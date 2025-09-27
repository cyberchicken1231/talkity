import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
connections: list[WebSocket] = []

# Mount the static directory so /static/style.css can be served
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get_index():
    # serve the index file from repository root
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
