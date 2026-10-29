"""Application operations behind the REST resources. Routers only call these methods."""
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, update

from app.db import Database
from app.infra.workspace_guard import WorkspaceGuard
from app.models.states import TERMINAL_TASK_STATES, TaskState
from app.models.tables import Approval, Conversation, Execution, ExecutionRequest, Message, StateTransition, Task
from app.repositories.approval_repository import ApprovalRepository, ExecutionRequestRepository
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.task_repository import TaskRepository
from app.services.hashing import stable_hash
from app.services.test_policy_service import hash_test_policy
from app.validation import policies as P
from app.validation import validators as V
from app.validation.exceptions import Conflict, NotFound


@dataclass(frozen=True)
class TaskDetail:
    task: Task
    lease: object | None
    checkpoint: object | None
    pending_approvals: list


class TaskService:
    def __init__(self, db: Database, guard: WorkspaceGuard, clock=lambda: datetime.now(UTC)):
        self.db, self.guard, self._now = db, guard, clock

    # --- conversations / messages ---------------------------------------------------------------------
    async def create_conversation(self, ttl_seconds: int | None = None) -> Conversation:
        async with self.db.transaction() as s:
            expires = self._now() + timedelta(seconds=ttl_seconds) if ttl_seconds else None
            return await ConversationRepository(s).create(expires_at=expires)

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation:
        async with self.db.transaction() as s:
            conv = await ConversationRepository(s).get(conversation_id)
        if conv is None:
            raise NotFound("conversation not found")
        return conv

    async def add_message(self, conversation_id: uuid.UUID, role: str, content: str) -> Message:
        role, content = V.validate_message(role, content)
        async with self.db.transaction() as s:
            repo = ConversationRepository(s)
            conv = await repo.get(conversation_id, for_update=True)
            if conv is None:
                raise NotFound("conversation not found")
            if conv.status != "ACTIVE" or (conv.expires_at is not None and conv.expires_at <= self._now()):
                raise Conflict("conversation is not ACTIVE; messages cannot be created", {"status": conv.status})
            return await repo.add_message(conversation_id, role, content)   # failure => exception => no 201

    async def list_messages(self, conversation_id: uuid.UUID) -> list[Message]:
        async with self.db.transaction() as s:
            if await ConversationRepository(s).get(conversation_id) is None:
                raise NotFound("conversation not found")
            return await ConversationRepository(s).list_messages(conversation_id)

    async def close_conversation(self, conversation_id: uuid.UUID) -> Conversation:
        async with self.db.transaction() as s:
            repo = ConversationRepository(s)
            conv = await repo.get(conversation_id, for_update=True)
            if conv is None:
                raise NotFound("conversation not found")
            if conv.status == "ACTIVE":
                await repo.update_if_version(conversation_id, conv.version, status="CLOSED", closed_at=func.now())
            return await repo.get(conversation_id)

    # --- tasks ---------------------------------------------------------------------------------------
    async def create_task(self, *, requirements: str, conversation_id: uuid.UUID | None = None,
                          limits: dict | None = None, test_policy: dict | None = None) -> Task:
        requirements = V.validate_requirements(requirements)
        limit_values = V.validate_limits(limits)
        policy = V.validate_test_policy(test_policy)
        async with self.db.transaction() as s:
            convs = ConversationRepository(s)
            if conversation_id is None:
                conversation_id = (await convs.create()).id
            else:
                conv = await convs.get(conversation_id)
                if conv is None:
                    raise NotFound("conversation not found")
                if conv.status != "ACTIVE":
                    raise Conflict("conversation is not ACTIVE", {"status": conv.status})
            task_id = uuid.uuid4()
            workspace = self.guard.task_workspace(task_id)
            task = await TaskRepository(s).add(Task(
                id=task_id, conversation_id=conversation_id, requirements=requirements,
                requirements_hash=stable_hash("requirements", requirements), status=str(TaskState.PENDING),
                limits={k: getattr(limit_values, k) for k in P.LIMIT_BOUNDS}, test_policy=policy,
                test_policy_hash=hash_test_policy(policy), workspace_path=str(workspace), plan_revision=0,
                workflow_iteration=0, replan_count=0, cli_invocations=0, run_seconds=0.0, decisions_hash="",
                row_version=0))
            await TaskRepository(s).add_transition(task.id, str(TaskState.PENDING), None, "created")
            return task

    async def get_task(self, task_id: uuid.UUID) -> Task:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).get(task_id)
        if task is None:
            raise NotFound("task not found")
        return task

    async def list_tasks(self, status: str | None, limit: int, offset: int) -> list[Task]:
        async with self.db.transaction() as s:
            return await TaskRepository(s).list(status=status, limit=limit, offset=offset)

    async def update_task(self, task_id: uuid.UUID, *, requirements: str | None = None,
                          limits: dict | None = None) -> Task:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            if task is None:
                raise NotFound("task not found")
            if task.status != str(TaskState.PENDING):
                raise Conflict("only a PENDING task can be modified", {"status": task.status})
            values: dict = {"updated_at": func.now(), "row_version": Task.row_version + 1}
            if requirements is not None:
                req = V.validate_requirements(requirements)
                values.update(requirements=req, requirements_hash=stable_hash("requirements", req))
            if limits is not None:
                lv = V.validate_limits(limits)
                values["limits"] = {k: getattr(lv, k) for k in P.LIMIT_BOUNDS}
            await s.execute(update(Task).where(Task.id == task_id).values(**values))
            await s.refresh(task)
            return task

    # --- execution / cancellation requests (resources, not verbs) -------------------------------------------
    async def request_execution(self, task_id: uuid.UUID) -> tuple[ExecutionRequest, bool]:
        """Returns (request, created). Never runs the CLI: the single Runner consumes the persisted request."""
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            if task is None:
                raise NotFound("task not found")
            state = TaskState(task.status)
            if state in TERMINAL_TASK_STATES:
                raise Conflict("task is already finished", {"status": task.status})
            if state is TaskState.WAITING_APPROVAL:
                raise Conflict("task is waiting for a Human Gate decision; resume happens automatically after it",
                               {"status": task.status})
            repo = ExecutionRequestRepository(s)
            existing = await repo.open_for_task(task_id)
            if existing is not None:
                return existing, False
            req = await repo.add(ExecutionRequest(id=uuid.uuid4(), task_id=task_id, kind="START", status="PENDING"))
            return req, True

    async def request_cancellation(self, task_id: uuid.UUID) -> Task:
        """Only records the request. The Runner stops the process, verifies termination, then marks CANCELLED."""
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            if task is None:
                raise NotFound("task not found")
            if TaskState(task.status) in TERMINAL_TASK_STATES:
                raise Conflict("task is already finished", {"status": task.status})
            if task.cancel_requested_at is None:
                await s.execute(update(Task).where(Task.id == task_id)
                                .values(cancel_requested_at=func.now(), updated_at=func.now()))
                await s.refresh(task)
            return task

    # --- read models -------------------------------------------------------------------------------------
    async def executions(self, task_id: uuid.UUID) -> list[Execution]:
        await self.get_task(task_id)
        async with self.db.transaction() as s:
            return await TaskRepository(s).executions_for_task(task_id)

    async def transitions(self, task_id: uuid.UUID) -> list[StateTransition]:
        await self.get_task(task_id)
        async with self.db.transaction() as s:
            return await TaskRepository(s).list_transitions(task_id)

    async def approvals(self, task_id: uuid.UUID) -> list[Approval]:
        await self.get_task(task_id)
        async with self.db.transaction() as s:
            return await ApprovalRepository(s).for_task(task_id)

    async def get_approval(self, approval_id: uuid.UUID) -> Approval:
        async with self.db.transaction() as s:
            approval = await ApprovalRepository(s).get(approval_id)
        if approval is None:
            raise NotFound("approval not found")
        return approval

    async def detail(self, task_id: uuid.UUID) -> "TaskDetail":
        """Task + live lease + latest checkpoint + open approvals (read model for GET /tasks/{id})."""
        async with self.db.transaction() as s:
            repo = TaskRepository(s)
            task = await repo.get(task_id)
            if task is None:
                raise NotFound("task not found")
            steps = await repo.steps_for_task(task_id)
            pending = await ApprovalRepository(s).pending_for_task(task_id)
            return TaskDetail(task, await repo.running_run(task_id), steps[-1] if steps else None,
                              [a.id for a in pending])
