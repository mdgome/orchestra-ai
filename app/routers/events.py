"""Live stream. PostgreSQL stays the source of truth: after a disconnect, re-read the REST history
(/tasks/{id}, /executions, /state-transitions). Replay of missed events is not guaranteed."""
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["events"])


@router.websocket("/tasks/{task_id}/event-stream")
async def event_stream(websocket: WebSocket, task_id: uuid.UUID):
    container = websocket.app.state.container
    await websocket.accept()
    try:
        detail = await container.tasks.detail(task_id)
        await websocket.send_json({"event": "snapshot", "status": detail.task.status})
        await container.events.pump(task_id, websocket.send_json)
    except WebSocketDisconnect:
        pass
