import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

app = FastAPI()
connections: list[WebSocket] = []

@app.get("/")
async def get_index():
    return FileResponse("index.html")

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connections.append(ws)
    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            # data looks like: {"user": "moses", "text": "hello"}
            for conn in connections:
                await conn.send_text(json.dumps(data))
    except WebSocketDisconnect:
        connections.remove(ws)
