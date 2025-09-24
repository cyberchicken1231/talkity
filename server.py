# server.py
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

app = FastAPI()

# keep track of connected websockets
clients: list[WebSocket] = []

@app.get("/")
async def get_index():
    path = os.path.join(os.path.dirname(__file__), "index.html")
    return FileResponse(path)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # simple server-side logging for debugging
            print("Received from client:", data)
            # broadcast to all connected clients
            for client in clients.copy():
                try:
                    await client.send_text(data)
                except Exception as e:
                    # if sending fails for a client, drop it
                    print("Error sending to client:", e)
                    try:
                        clients.remove(client)
                    except ValueError:
                        pass
    except WebSocketDisconnect:
        # client disconnected gracefully
        if websocket in clients:
            clients.remove(websocket)
        print("Client disconnected")
    except Exception as e:
        # catch-all to avoid crashing the server loop
        print("WebSocket error:", e)
        if websocket in clients:
            clients.remove(websocket)
