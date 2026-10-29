"""PostgreSQL implementation of SessionStore. Works inside the caller's transaction; never commits."""
import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import AgentSession, Conversation
from app.repositories.session_store import AgentSessionRecord, ConversationRecord


def _conv(row: Conversation) -> ConversationRecord:
    return ConversationRecord(row.id, row.status, row.expires_at, row.version, row.context_ref)


def _sess(row: AgentSession) -> AgentSessionRecord:
    return AgentSessionRecord(row.id, row.conversation_id, row.task_id, row.agent_role, row.provider,
                              row.native_session_id, row.status, row.expires_at, row.version)


class PostgresSessionStore:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def get_conversation(self, conversation_id: uuid.UUID) -> ConversationRecord | None:
        row = (await self.s.execute(select(Conversation).where(Conversation.id == conversation_id))).scalar_one_or_none()
        return _conv(row) if row else None

    async def update_conversation_if_version(self, conversation_id: uuid.UUID, expected_version: int,
                                             **fields) -> bool:
        result = await self.s.execute(
            update(Conversation).where(Conversation.id == conversation_id, Conversation.version == expected_version)
            .values(**fields, version=Conversation.version + 1, updated_at=func.now()))
        return result.rowcount == 1

    async def get_agent_session(self, task_id: uuid.UUID, role: str) -> AgentSessionRecord | None:
        row = (await self.s.execute(
            select(AgentSession).where(AgentSession.task_id == task_id, AgentSession.agent_role == role)
        )).scalar_one_or_none()
        return _sess(row) if row else None

    async def create_agent_session(self, *, conversation_id: uuid.UUID, task_id: uuid.UUID, role: str,
                                   provider: str, expires_at: datetime | None) -> AgentSessionRecord:
        row = AgentSession(id=uuid.uuid4(), conversation_id=conversation_id, task_id=task_id, agent_role=role,
                           provider=provider, native_session_id=None, status="ACTIVE", expires_at=expires_at,
                           version=0)
        self.s.add(row)
        await self.s.flush()
        return _sess(row)

    async def upsert_native_session_if_version(self, session_id: uuid.UUID, expected_version: int,
                                               native_session_id: str | None, status: str) -> bool:
        result = await self.s.execute(
            update(AgentSession).where(AgentSession.id == session_id, AgentSession.version == expected_version)
            .values(native_session_id=native_session_id, status=status, version=AgentSession.version + 1,
                    updated_at=func.now(), last_used_at=func.now()))
        return result.rowcount == 1

    async def set_status_if_version(self, session_id: uuid.UUID, expected_version: int, status: str) -> bool:
        result = await self.s.execute(
            update(AgentSession).where(AgentSession.id == session_id, AgentSession.version == expected_version)
            .values(status=status, version=AgentSession.version + 1, updated_at=func.now()))
        return result.rowcount == 1
