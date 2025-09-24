from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import pathlib

app = FastAPI()

# Serve index.html
@app.get("/")
async def get():
    html = pathlib.Path("index.html").read_text()
    return HTMLResponse(html)

# Store active connections
connections = []

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connections.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            print(f"Received from client: {data}")
            for conn in connections:
                await conn.send_text(data)
    except WebSocketDisconnect:
        connections.remove(websocket)
