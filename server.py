import json
import os
import sqlite3
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# in-memory active websocket connections per room
# each entry is a dict: {"ws": WebSocket, "user": str|None, "is_admin": bool}
active_rooms: dict[str, list[dict]] = {}

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
    entry = {"ws": ws, "user": None, "is_admin": False}
    active_rooms[room].append(entry)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                # ignore malformed JSON
                continue

            if not isinstance(data, dict):
                continue

            # client may send { "user": <name>, "text": <message> }
            username = (data.get("user") or "")
            if username and not entry.get("user"):
                # set username on first appearance for this connection
                entry["user"] = str(username)

            text = (data.get("text") or "").strip()
            if not text:
                continue

            # Commands start with '>'
            if text.startswith(">"):
                cmd_body = text[1:].strip()
                if not cmd_body:
                    await ws.send_text(json.dumps({"user": "system", "text": "Empty command"}))
                    continue
                parts = cmd_body.split(" ", 1)
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else ""

                # >login <username> <password>
                if cmd == "login":
                    creds = arg.split(" ", 1)
                    if len(creds) < 2:
                        await ws.send_text(json.dumps({"user": "system", "text": "Usage: >login <username> <password>"}))
                        continue
                    uname, pwd = creds[0].strip(), creds[1].strip()
                    ADMIN_USER = os.getenv("ADMIN_USERNAME")
                    ADMIN_PWD = os.getenv("ADMIN_PASSWORD")
                    if ADMIN_USER and ADMIN_PWD and uname == ADMIN_USER and pwd == ADMIN_PWD:
                        entry["is_admin"] = True
                        await ws.send_text(json.dumps({"user": "system", "text": "Admin privileges granted"}))
                    else:
                        await ws.send_text(json.dumps({"user": "system", "text": "Invalid admin credentials"}))
                    continue

                # other commands require admin
                if not entry.get("is_admin"):
                    await ws.send_text(json.dumps({"user": "system", "text": "Unauthorized: admin only command"}))
                    continue

                # >create <room-name>
                if cmd == "create":
                    room_name = arg.strip()
                    if not room_name:
                        await ws.send_text(json.dumps({"user": "system", "text": "Usage: >create <room-name>"}))
                        continue
                    created = create_room(room_name)
                    await ws.send_text(json.dumps({"user": "system", "text": f"Room '{room_name}' {'created' if created else 'already exists'}"}))
                    continue

                # >warn <username> <message>
                if cmd == "warn":
                    warn_parts = arg.split(" ", 1)
                    if len(warn_parts) < 1 or not warn_parts[0]:
                        await ws.send_text(json.dumps({"user": "system", "text": "Usage: >warn <username> <message>"}))
                        continue
                    target = warn_parts[0]
                    warn_msg = warn_parts[1] if len(warn_parts) > 1 else "You have been warned by an admin"
                    conns = active_rooms.get(room, [])
                    found = False
                    for e in conns:
                        if e.get("user") == target:
                            try:
                                await e["ws"].send_text(json.dumps({"user": "system", "text": f"WARNING: {warn_msg}"}))
                                found = True
                            except Exception:
                                pass
                    await ws.send_text(json.dumps({"user": "system", "text": f"Warned {target}: {found}"}))
                    continue

                # >kick <username> [reason]
                if cmd == "kick":
                    kick_parts = arg.split(" ", 1)
                    if len(kick_parts) < 1 or not kick_parts[0]:
                        await ws.send_text(json.dumps({"user": "system", "text": "Usage: >kick <username> <reason?>"}))
                        continue
                    target = kick_parts[0]
                    reason = kick_parts[1] if len(kick_parts) > 1 else "kicked by admin"
                    conns = active_rooms.get(room, [])
                    removed = 0
                    for e in list(conns):
                        if e.get("user") == target:
                            try:
                                await e["ws"].send_text(json.dumps({"user": "system", "text": f"KICK: {reason}"}))
                                await e["ws"].close()
                            except Exception:
                                pass
                            try:
                                conns.remove(e)
                            except ValueError:
                                pass
                            removed += 1
                    await ws.send_text(json.dumps({"user": "system", "text": f"Kicked {removed} connections for {target}"}))
                    continue

                await ws.send_text(json.dumps({"user": "system", "text": f"Unknown command: {cmd}"}))
                continue

            # regular message broadcast
            payload = {"user": entry.get("user") or username or "anon", "text": text}
            conns = list(active_rooms.get(room, []))
            for e in conns:
                try:
                    await e["ws"].send_text(json.dumps(payload))
                except Exception:
                    pass
    except WebSocketDisconnect:
        # remove the websocket entry from the room list
        if room in active_rooms:
            for e in list(active_rooms[room]):
                if e.get("ws") is ws:
                    try:
                        active_rooms[room].remove(e)
                    except ValueError:
                        pass
            if not active_rooms[room]:
                del active_rooms[room]
