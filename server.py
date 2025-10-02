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

    async def broadcast_user_list(r: str):
        # build a list of usernames currently active in room r
        conns = active_rooms.get(r, [])
        users = []
        for e in conns:
            name = (e.get("user") or "").strip()
            if not name:
                continue
            users.append({"name": name, "is_admin": bool(e.get("is_admin"))})
        payload = {"type": "users", "users": users}
        for e in list(conns):
            try:
                await e["ws"].send_text(json.dumps(payload))
            except Exception:
                pass

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

            # handle join payloads: { type: 'join', user: 'name' }
            if data.get("type") == "join":
                # register the username for this connection and broadcast join to the room
                username = (data.get("user") or "").strip()
                if username and not entry.get("user"):
                    # enforce one user per username globally (case-insensitive)
                    name_norm = username.strip().lower()
                    conflict = None
                    # scan every active connection across all rooms
                    for r_conns in active_rooms.values():
                        for e in r_conns:
                            u = (e.get("user") or "").strip().lower()
                            if u and u == name_norm:
                                conflict = e
                                break
                        if conflict:
                            break

                    if conflict:
                        # inform the joining client that the name is taken (globally) and close
                        try:
                            await ws.send_text(json.dumps({"user": "system", "text": f"Username '{username}' is already in use"}))
                        except Exception:
                            pass
                        # remove our placeholder entry and close
                        try:
                            if entry in active_rooms.get(room, []):
                                active_rooms[room].remove(entry)
                        except Exception:
                            pass
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        continue

                    entry["user"] = username
                    # private welcome for the joining client
                    try:
                        await ws.send_text(json.dumps({"user": "system", "text": f"Welcome {entry['user']} to {room}."}))
                    except Exception:
                        pass
                    # broadcast join announcement to everyone in the room
                    join_msg = {"user": "system", "text": f"{entry['user']} joined the room"}
                    for e in list(active_rooms.get(room, [])):
                        try:
                            await e["ws"].send_text(json.dumps(join_msg))
                        except Exception:
                            pass
                    # broadcast updated user list to the room
                    try:
                        await broadcast_user_list(room)
                    except Exception:
                        pass
                continue

            # client may send { "user": <name>, "text": <message> }
            username = (data.get("user") or "")
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
                    # announce to admin (private) and to room as admin message if ADMIN_USERNAME is set
                    await ws.send_text(json.dumps({"user": "system", "text": f"Room '{room_name}' {'created' if created else 'already exists'}"}))
                    # choose announcement username: prefer the admin's live username, then env var, then 'system'
                    env_admin = os.getenv("ADMIN_USERNAME")
                    ann_user = entry.get("user") if entry.get("is_admin") and entry.get("user") else (env_admin or "system")
                    ann = {"user": ann_user, "text": f"Room '{room_name}' {'created' if created else 'already exists'}"}
                    for e in list(active_rooms.get(room, [])):
                        try:
                            await e["ws"].send_text(json.dumps(ann))
                        except Exception:
                            pass
                    continue

                # >warn <username> <message>
                if cmd == "warn":
                        warn_parts = arg.split(" ", 1)
                        if len(warn_parts) < 1 or not warn_parts[0].strip():
                            await ws.send_text(json.dumps({"user": "system", "text": "Usage: >warn <username> <message>"}))
                            continue
                        target = warn_parts[0].strip()
                        warn_msg = warn_parts[1].strip() if len(warn_parts) > 1 and warn_parts[1].strip() else "You have been warned by an admin"
                        conns = active_rooms.get(room, [])
                        found = 0
                        target_norm = target.lower()
                        # iterate over a copy to avoid mutation issues
                        for e in list(conns):
                            u = (e.get("user") or "").strip()
                            if u.lower() == target_norm:
                                try:
                                    await e["ws"].send_text(json.dumps({"user": "system", "text": f"WARNING: {warn_msg}"}))
                                    found += 1
                                except Exception:
                                    # If sending fails, try to close and remove the connection
                                    try:
                                        await e["ws"].close()
                                    except Exception:
                                        pass
                                    try:
                                        conns.remove(e)
                                    except ValueError:
                                        pass
                        # after potential removals, broadcast updated user list
                        try:
                            await broadcast_user_list(room)
                        except Exception:
                            pass
                        # confirm to the admin who issued the warn
                        confirm_user = entry.get("user") if entry.get("is_admin") and entry.get("user") else "system"
                        await ws.send_text(json.dumps({"user": "system", "text": f"Warned {found} connection(s) for {target}."}))
                        # also announce to room as admin message if applicable
                        if found:
                            env_admin = os.getenv("ADMIN_USERNAME")
                            ann_user = entry.get("user") if entry.get("is_admin") and entry.get("user") else (env_admin or "system")
                            ann = {"user": ann_user, "text": f"Admin warned {target}: {warn_msg}"}
                            for e in list(active_rooms.get(room, [])):
                                try:
                                    await e["ws"].send_text(json.dumps(ann))
                                except Exception:
                                    pass
                        continue

                # >kick <username> [reason]
                if cmd == "kick":
                        kick_parts = arg.split(" ", 1)
                        if len(kick_parts) < 1 or not kick_parts[0].strip():
                            await ws.send_text(json.dumps({"user": "system", "text": "Usage: >kick <username> <reason?>"}))
                            continue
                        target = kick_parts[0].strip()
                        reason = kick_parts[1].strip() if len(kick_parts) > 1 and kick_parts[1].strip() else "kicked by admin"
                        conns = active_rooms.get(room, [])
                        removed = 0
                        target_norm = target.lower()
                        # iterate over a copy so we can modify the original list
                        for e in list(conns):
                            u = (e.get("user") or "").strip()
                            if u.lower() == target_norm:
                                try:
                                    # notify the target then close
                                    await e["ws"].send_text(json.dumps({"user": "system", "text": f"KICK: {reason}"}))
                                    await e["ws"].close()
                                except Exception:
                                    try:
                                        await e["ws"].close()
                                    except Exception:
                                        pass
                                # remove from the live list if present
                                try:
                                    conns.remove(e)
                                except ValueError:
                                    pass
                                removed += 1
                        # confirm to admin
                        await ws.send_text(json.dumps({"user": "system", "text": f"Kicked {removed} connection(s) for {target}."}))
                        # broadcast a room-wide announcement about the kick
                        if removed:
                            env_admin = os.getenv("ADMIN_USERNAME")
                            kick_user = entry.get("user") if entry.get("is_admin") and entry.get("user") else (env_admin or "system")
                            kick_announce = {"user": kick_user, "text": f"{target} was kicked by admin ({reason})"}
                            for e in list(active_rooms.get(room, [])):
                                try:
                                    await e["ws"].send_text(json.dumps(kick_announce))
                                except Exception:
                                    pass
                            # broadcast updated user list now that targets were removed
                            try:
                                await broadcast_user_list(room)
                            except Exception:
                                pass
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
