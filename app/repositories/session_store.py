"""SessionStore: persistence operations for logical conversation sessions and per-role native sessions.

Contract
- Only reads and compare-and-set writes. It NEVER commits or rolls back: the calling service owns the
  PostgreSQL transaction, so Session, Step and Lease changes stay atomic together.
- The interface exposes plain records only (no ORM / SQLAlchemy types), so a Phase 3 Redis *read cache*
  can sit in front of it. Atomic multi-table changes must not be routed through a cache.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ConversationRecord:
    id: uuid.UUID
    status: str
    expires_at: datetime | None
    version: int
    context_ref: str | None


@dataclass(frozen=True)
class AgentSessionRecord:
    id: uuid.UUID
    conversation_id: uuid.UUID
    task_id: uuid.UUID
    agent_role: str
    provider: str
    native_session_id: str | None   # internal: never put this in an API response or a log line
    status: str
    expires_at: datetime | None
    version: int


class SessionStore(Protocol):
    async def get_conversation(self, conversation_id: uuid.UUID) -> ConversationRecord | None: ...

    async def update_conversation_if_version(self, conversation_id: uuid.UUID, expected_version: int,
                                             **fields) -> bool: ...

    async def get_agent_session(self, task_id: uuid.UUID, role: str) -> AgentSessionRecord | None: ...

    async def create_agent_session(self, *, conversation_id: uuid.UUID, task_id: uuid.UUID, role: str,
                                   provider: str, expires_at: datetime | None) -> AgentSessionRecord: ...

    async def upsert_native_session_if_version(self, session_id: uuid.UUID, expected_version: int,
                                               native_session_id: str | None, status: str) -> bool: ...

    async def set_status_if_version(self, session_id: uuid.UUID, expected_version: int, status: str) -> bool: ...
