import uuid

from fastapi import APIRouter, Depends, status

from app.container import Container
from app.routers.deps import get_container
from app.schemas.api import ConversationCreate, ConversationOut, MessageCreate, MessageOut

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(body: ConversationCreate | None = None, c: Container = Depends(get_container)):
    return await c.tasks.create_conversation(body.ttl_seconds if body else None)


@router.get("/{conversation_id}", response_model=ConversationOut)
async def read_conversation(conversation_id: uuid.UUID, c: Container = Depends(get_container)):
    return await c.tasks.get_conversation(conversation_id)


@router.post("/{conversation_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
async def create_message(conversation_id: uuid.UUID, body: MessageCreate, c: Container = Depends(get_container)):
    return await c.tasks.add_message(conversation_id, body.role, body.content)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(conversation_id: uuid.UUID, c: Container = Depends(get_container)):
    return await c.tasks.list_messages(conversation_id)
