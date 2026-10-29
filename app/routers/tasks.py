import uuid

from fastapi import APIRouter, Depends, Query, status

from app.container import Container
from app.routers.deps import get_container
from app.schemas.api import (
    ApprovalOut, ExecutionOut, ExecutionRequestOut, TaskCreate, TaskDetailOut, TaskOut, TaskPatch, TransitionOut,
    build_task_detail,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskOut])
async def list_tasks(status_filter: str | None = Query(None, alias="status"), limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0), c: Container = Depends(get_container)):
    return await c.tasks.list_tasks(status_filter, limit, offset)


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskCreate, c: Container = Depends(get_container)):
    """Creating a task only records it (PENDING). Execution is a separate resource: /execution-requests."""
    return await c.tasks.create_task(requirements=body.requirements, conversation_id=body.conversation_id,
                                     limits=body.limits, test_policy=body.test_policy)


@router.get("/{task_id}", response_model=TaskDetailOut)
async def read_task(task_id: uuid.UUID, c: Container = Depends(get_container)):
    return build_task_detail(await c.tasks.detail(task_id))


@router.patch("/{task_id}", response_model=TaskOut)
async def patch_task(task_id: uuid.UUID, body: TaskPatch, c: Container = Depends(get_container)):
    return await c.tasks.update_task(task_id, requirements=body.requirements, limits=body.limits)


@router.post("/{task_id}/execution-requests", response_model=ExecutionRequestOut,
             status_code=status.HTTP_202_ACCEPTED)
async def create_execution_request(task_id: uuid.UUID, c: Container = Depends(get_container)):
    request, _created = await c.tasks.request_execution(task_id)
    c.runner.notify()
    return request


@router.post("/{task_id}/cancellation-requests", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def create_cancellation_request(task_id: uuid.UUID, c: Container = Depends(get_container)):
    task = await c.tasks.request_cancellation(task_id)
    c.runner.notify()
    return task


@router.get("/{task_id}/executions", response_model=list[ExecutionOut])
async def list_executions(task_id: uuid.UUID, c: Container = Depends(get_container)):
    return await c.tasks.executions(task_id)


@router.get("/{task_id}/state-transitions", response_model=list[TransitionOut])
async def list_state_transitions(task_id: uuid.UUID, c: Container = Depends(get_container)):
    return await c.tasks.transitions(task_id)


@router.get("/{task_id}/approvals", response_model=list[ApprovalOut])
async def list_task_approvals(task_id: uuid.UUID, c: Container = Depends(get_container)):
    return await c.tasks.approvals(task_id)
