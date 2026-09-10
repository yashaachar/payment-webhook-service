import json
from typing import List
from fastapi import WebSocket


class ConnectionManager:
    """
    Tracks connected dashboard clients and broadcasts ledger updates to
    all of them. Kept in-process and in-memory on purpose — this is a
    portfolio project, so a single-instance broadcaster is the right
    amount of complexity. At real scale you'd back this with Redis
    pub/sub so it works across multiple app instances.
    """

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        dead_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message, default=str))
            except Exception:
                dead_connections.append(connection)
        for dead in dead_connections:
            self.disconnect(dead)


manager = ConnectionManager()
