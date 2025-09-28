import json
import os
import sqlite3
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# in-memory active websocket connections per room
active_rooms: dict[str, list[WebSocket]] = {}

# SQLite DB file for persistent rooms
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

# Mount the static directory so /static/style.css can be served
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_db_connection():
    # open a new connection per call; keep check_same_thread False for uvicorn threads
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def list_rooms() -> List[str]:
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT name FROM rooms ORDER BY name")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def create_room(name: str) -> bool:
    """Create a room in the DB. Returns True if created, False if it already exists."""
    # normalize room name
    name = (name or "").strip().lower()
    conn = get_db_connection()
    try:
        try:
            conn.execute("INSERT INTO rooms (name) VALUES (?)", (name,))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
    finally:
        conn.close()


def room_exists(name: str) -> bool:
    # normalize lookup
    name = (name or "").strip().lower()
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT 1 FROM rooms WHERE name = ? LIMIT 1", (name,))
        return cur.fetchone() is not None
    finally:
        conn.close()


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
async def get_index():
    # serve the index file from repository root
    return FileResponse("index.html")


@app.get("/rooms")
async def api_list_rooms():
    return JSONResponse(content={"rooms": list_rooms()})


@app.post("/rooms")
async def api_create_room(request: Request):
    # Admin token required (set ADMIN_TOKEN env var on the server)
    admin_token = os.getenv("ADMIN_TOKEN")
    if not admin_token:
        # creation disabled unless ADMIN_TOKEN configured
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Room creation disabled: ADMIN_TOKEN not configured")

    header_token = request.headers.get("x-admin-token")
    if header_token != admin_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid admin token")

    body = await request.json()
    name = (body.get("name") or "").strip().lower()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing room name")

    created = create_room(name)
    if created:
        return JSONResponse(status_code=status.HTTP_201_CREATED, content={"room": name})
    else:
        return JSONResponse(status_code=status.HTTP_200_OK, content={"room": name, "note": "already exists"})


@app.websocket("/ws/{room}")
async def websocket_endpoint(ws: WebSocket, room: str):
    # normalize requested room name and ensure it exists in persistent storage
    room = (room or "").strip().lower()
    if not room_exists(room):
        # accept then send error and close
        await ws.accept()
        await ws.send_text(json.dumps({"user": "system", "text": f"Room '{room}' does not exist"}))
        await ws.close()
        return

    # accept connection and add to in-memory active room list
    await ws.accept()
    if room not in active_rooms:
        active_rooms[room] = []
    active_rooms[room].append(ws)
    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            # broadcast to everyone in the same room
            conns = list(active_rooms.get(room, []))
            for conn in conns:
                try:
                    await conn.send_text(json.dumps(data))
                except Exception:
                    # ignore send errors here; cleanup will happen on disconnect
                    pass
    except WebSocketDisconnect:
        # remove the websocket from the room list
        if room in active_rooms and ws in active_rooms[room]:
            active_rooms[room].remove(ws)
            if not active_rooms[room]:
                del active_rooms[room]
