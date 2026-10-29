"""Conversation metadata and Message content. Repositories never commit."""
import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Conversation, Message


class ConversationRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def create(self, expires_at: datetime | None = None, context_ref: str | None = None) -> Conversation:
        conv = Conversation(id=uuid.uuid4(), status="ACTIVE", expires_at=expires_at, context_ref=context_ref, version=0)
        self.s.add(conv)
        await self.s.flush()
        return conv

    async def get(self, conversation_id: uuid.UUID, *, for_update: bool = False) -> Conversation | None:
        stmt = select(Conversation).where(Conversation.id == conversation_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def update_if_version(self, conversation_id: uuid.UUID, expected_version: int, **fields) -> bool:
        """Compare-and-set on `version`; returns False when another writer got there first."""
        result = await self.s.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.version == expected_version)
            .values(**fields, version=Conversation.version + 1, updated_at=func.now()))
        return result.rowcount == 1

    async def add_message(self, conversation_id: uuid.UUID, role: str, content: str) -> Message:
        msg = Message(id=uuid.uuid4(), conversation_id=conversation_id, role=role, content=content)
        self.s.add(msg)
        await self.s.flush()
        return msg

    async def list_messages(self, conversation_id: uuid.UUID) -> list[Message]:
        rows = await self.s.execute(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id))
        return list(rows.scalars())

    async def expire_due(self, now: datetime) -> int:
        """ACTIVE conversations past expires_at become EXPIRED."""
        result = await self.s.execute(
            update(Conversation)
            .where(Conversation.status == "ACTIVE", Conversation.expires_at.is_not(None), Conversation.expires_at <= now)
            .values(status="EXPIRED", version=Conversation.version + 1, updated_at=func.now()))
        return result.rowcount

    async def delete_messages_of_ended_conversations(self, ended_before: datetime) -> int:
        """Retention: removes message rows only, of CLOSED/EXPIRED conversations ended before the cutoff.
        Nothing references messages, so task/step/execution/approval history is untouched."""
        ended = select(Conversation.id).where(
            Conversation.status.in_(("CLOSED", "EXPIRED")),
            func.coalesce(Conversation.closed_at, Conversation.expires_at, Conversation.updated_at) <= ended_before)
        result = await self.s.execute(delete(Message).where(Message.conversation_id.in_(ended)))
        return result.rowcount
