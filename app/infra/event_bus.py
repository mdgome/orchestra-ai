"""In-memory best-effort pub/sub for live WebSocket updates.

PostgreSQL (executions, state_transitions, approvals) is the source of truth: a client that misses events
re-reads the REST history. Phase 1+2 does not guarantee delivery or replay.
"""
import asyncio
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable


class EventBus:
    def __init__(self, queue_size: int = 256):
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._size = queue_size

    def subscribe(self, task_id: uuid.UUID | str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(self._size)
        self._subs[str(task_id)].add(queue)
        return queue

    def unsubscribe(self, task_id: uuid.UUID | str, queue: asyncio.Queue) -> None:
        self._subs[str(task_id)].discard(queue)

    def publish(self, task_id: uuid.UUID | str, event: dict) -> None:
        for queue in list(self._subs.get(str(task_id), ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer: it can recover from the REST history

    async def pump(self, task_id: uuid.UUID | str, send: Callable[[dict], Awaitable[None]],
                   heartbeat_seconds: float = 15) -> None:
        """Forwards live events to `send` until the caller's transport fails/cancels; always unsubscribes."""
        queue = self.subscribe(task_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                except TimeoutError:
                    event = {"event": "ping"}
                await send(event)
        finally:
            self.unsubscribe(task_id, queue)
