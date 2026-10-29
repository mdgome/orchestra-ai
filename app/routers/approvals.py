import uuid

from fastapi import APIRouter, Depends, Header, Response, status

from app.container import Container
from app.routers.deps import get_container
from app.schemas.api import ApprovalOut, DecisionCreate, DecisionOut

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("/{approval_id}", response_model=ApprovalOut)
async def read_approval(approval_id: uuid.UUID, c: Container = Depends(get_container)):
    return await c.tasks.get_approval(approval_id)


@router.post("/{approval_id}/decisions", response_model=DecisionOut, status_code=status.HTTP_201_CREATED,
             responses={200: {"model": DecisionOut, "description": "identical decision already recorded"}})
async def create_decision(approval_id: uuid.UUID, body: DecisionCreate, response: Response,
                          idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                          c: Container = Depends(get_container)):
    outcome = await c.gates.decide(approval_id, decision=body.decision, selected_option=body.selected_option,
                                   rationale=body.rationale, idempotency_key=idempotency_key)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    else:
        c.runner.notify()               # the RESUME request was stored in the same transaction as the decision
    return outcome.decision
