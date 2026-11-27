"""Application operations behind the REST resources. Routers only call these methods."""
import copy
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, update

from app.db import Database
from app.infra.workspace_guard import WorkspaceGuard, resolve_project_root
from app.models.states import TERMINAL_TASK_STATES, TaskMode, TaskState
from app.models.tables import Approval, Conversation, Execution, ExecutionRequest, Message, StateTransition, Task
from app.repositories.approval_repository import ApprovalRepository, ExecutionRequestRepository
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.process_risk_repository import ProcessRiskRequestRepository
from app.repositories.profile_repository import ProfileRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.services.hashing import stable_hash
from app.services.test_policy_resolver import resolve_test_policy
from app.services.test_policy_service import hash_test_policy
from app.validation import policies as P
from app.validation import validators as V
from app.validation.exceptions import ActiveTestRunExists, Conflict, IdempotencyKeyReused, NotFound


@dataclass(frozen=True)
class ApprovalView:
    """An Approval as the read paths return it: the ORM fields plus the three additive, computed ones (design 7.5 item 2).
    `options` and `options_hidden` are None for a static gate or when no options provider is wired; `operator_request` is None
    when the approval has no risk request."""
    id: uuid.UUID
    task_id: uuid.UUID
    type: str
    status: str
    scope: str
    target: dict
    request_payload: dict
    checkpoint_step_key: str
    valid_until: datetime
    used_at: datetime | None
    created_at: datetime
    options: list | None = None
    options_hidden: list | None = None
    operator_request: dict | None = None


def operator_request_of(request) -> dict | None:
    if request is None:
        return None
    return {"request_id": request.approval_id, "kind": request.kind, "status": request.status,
            "target_count": len(request.target_execution_ids), "requested_at": request.created_at,
            "finished_at": request.finished_at, "reason": request.reason,
            "retryable": request.status in ("REQUESTED", "VERIFYING", "RETRYABLE"), "attempts": request.attempts,
            "next_attempt_at": request.next_attempt_at if request.status == "RETRYABLE" else None,
            "last_error": request.last_error}


@dataclass(frozen=True)
class TaskDetail:
    task: Task
    lease: object | None
    checkpoint: object | None
    pending_approvals: list


class TaskService:
    def __init__(self, db: Database, guard: WorkspaceGuard, clock=lambda: datetime.now(UTC), options_provider=None):
        """`options_provider(session, approvals) -> {approval_id: ProviderResult | None}` (HumanGateService.current_options_for_approvals);
        None keeps the static behaviour and omits the new approval fields."""
        self.db, self.guard, self._now = db, guard, clock
        self._options_provider = options_provider

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
                          limits: dict | None = None, test_policy: dict | None = None,
                          project_id: uuid.UUID | None = None) -> Task:
        requirements = V.validate_requirements(requirements)
        limit_values = V.validate_limits(limits)
        async with self.db.transaction() as s:
            project = await self._lock_active_project(s, project_id)
            policy, project_root = self._resolve_policy(project, test_policy)
            return await self._insert_task(s, project, project_root, requirements=requirements,
                                           conversation_id=conversation_id, limit_values=limit_values, policy=policy)

    async def create_test_run(self, project_id: uuid.UUID, *, idempotency_key: str | None = None) -> tuple[Task, bool]:
        """A Project Test Run: an ordinary Task with mode TEST_ONLY that only runs the project's test policy (D4).
        Returns (task, replayed). One short, DB-only transaction under the project row lock (the lock create_task and
        archive use), so two concurrent calls cannot both create a run. The Runner consumes the START request it records.

        The key is looked up FIRST: the same key and the same meaning replays that run (in progress or finished); the same
        key with another meaning is IDEMPOTENCY_KEY_REUSED. Otherwise a project with a non-terminal run answers
        TEST_RUN_ACTIVE. No command, path or policy comes from the caller: the policy is the project's own."""
        key = V.validate_idempotency_key(idempotency_key)
        async with self.db.transaction() as s:
            project = await self._lock_active_project(s, project_id)
            if project is None:
                raise NotFound("project not found")
            policy, project_root = self._resolve_policy(project, None)
            fingerprint = stable_hash("test_run_request", str(project.id), hash_test_policy(policy))
            repo = TaskRepository(s)
            if key is not None:
                existing = await repo.test_run_by_key(project.id, key)
                if existing is not None:
                    if existing.request_fingerprint != fingerprint:
                        raise IdempotencyKeyReused("the idempotency key was used for a different request",
                                                   {"test_run_id": str(existing.id)})
                    return existing, True
            active = await repo.active_test_run(project.id, tuple(str(x) for x in TERMINAL_TASK_STATES))
            if active is not None:
                raise ActiveTestRunExists("the project already has an active test run", {"test_run_id": str(active.id)})
            task = await self._insert_task(s, project, project_root, requirements=P.TEST_ONLY_REQUIREMENTS,
                                           conversation_id=None, limit_values=V.validate_limits(None), policy=policy,
                                           mode=TaskMode.TEST_ONLY, idempotency_key=key, request_fingerprint=fingerprint)
            await ExecutionRequestRepository(s).add(
                ExecutionRequest(id=uuid.uuid4(), task_id=task.id, kind="START", status="PENDING"))
            return task, False

    async def _lock_active_project(self, s, project_id: uuid.UUID | None):
        if project_id is None:
            return None
        # FOR UPDATE: serialises with ARCHIVE, so a task can never be added to a project being archived
        project = await ProjectRepository(s).lock(project_id)
        if project is None:
            raise NotFound("project not found")
        if project.status != "ACTIVE":
            raise Conflict("project is not ACTIVE", {"status": project.status})
        return project

    @staticmethod
    def _resolve_policy(project, test_policy: dict | None) -> tuple[dict, object]:
        """precedence: task policy > project policy > default; a preset is resolved to ONE runnable command now and copied
        into the task, so test_policy_hash describes exactly what will run. Returns (policy, project root or None)."""
        project_root = resolve_project_root(project.workspace_path) if project else None
        policy = resolve_test_policy(test_policy if test_policy is not None else
                                     (project.test_policy if project else None), project_root)
        return policy, project_root

    async def _insert_task(self, s, project, project_root, *, requirements: str, conversation_id: uuid.UUID | None,
                           limit_values, policy: dict, mode: TaskMode = TaskMode.WORKFLOW,
                           idempotency_key: str | None = None, request_fingerprint: str | None = None) -> Task:
        """The part create_task and create_test_run share: conversation, profile copy, the Task row and its first transition."""
        project_id = project.id if project is not None else None
        convs = ConversationRepository(s)
        if conversation_id is None:
            conversation_id = (await convs.create()).id
        else:
            conv = await convs.get(conversation_id)
            if conv is None:
                raise NotFound("conversation not found")
            if conv.status != "ACTIVE":
                raise Conflict("conversation is not ACTIVE", {"status": conv.status})
        # the active profile version is copied (number, hash, content) under the same project row lock that
        # serialises profile changes, so a task sees exactly one version and a later change cannot alter it
        profile = (await ProfileRepository(s).get(project.id, project.profile_version)
                   if project is not None and project.profile_version else None)
        task_id = uuid.uuid4()
        # project tasks share the registered workspace (re-validated here and before every run)
        workspace = project_root if project else self.guard.task_workspace(task_id)
        task = await TaskRepository(s).add(Task(
            id=task_id, conversation_id=conversation_id, project_id=project_id, requirements=requirements,
            requirements_hash=stable_hash("requirements", requirements), status=str(TaskState.PENDING),
            limits={k: getattr(limit_values, k) for k in P.LIMIT_BOUNDS}, test_policy=policy,
            test_policy_hash=hash_test_policy(policy), workspace_path=str(workspace), plan_revision=0,
            workflow_iteration=0, replan_count=0, cli_invocations=0, run_seconds=0.0, decisions_hash="",
            profile_version=profile.version if profile else None,
            profile_hash=profile.content_hash if profile else None,
            profile_snapshot=copy.deepcopy(profile.content) if profile else None, row_version=0,
            mode=str(mode), idempotency_key=idempotency_key, request_fingerprint=request_fingerprint))
        await TaskRepository(s).add_transition(task.id, str(TaskState.PENDING), None, "created")
        return task

    async def get_task(self, task_id: uuid.UUID) -> Task:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).get(task_id)
        if task is None:
            raise NotFound("task not found")
        return task

    async def list_tasks(self, status: str | None, limit: int, offset: int,
                         project_id: uuid.UUID | None = None) -> list[Task]:
        async with self.db.transaction() as s:
            if project_id is not None and await ProjectRepository(s).get(project_id) is None:
                raise NotFound("project not found")
            return await TaskRepository(s).list(status=status, limit=limit, offset=offset, project_id=project_id)

    async def update_task(self, task_id: uuid.UUID, *, requirements: str | None = None,
                          limits: dict | None = None) -> Task:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            if task is None:
                raise NotFound("task not found")
            if task.mode == str(TaskMode.TEST_ONLY):
                raise Conflict("a test run cannot be modified", {"mode": task.mode})
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

    async def _views(self, s, approvals: list[Approval]) -> list[ApprovalView]:
        provided = {}
        if self._options_provider is not None and approvals:
            provided = await self._options_provider(s, approvals)
        requests = await ProcessRiskRequestRepository(s).by_approvals([a.id for a in approvals])
        views = []
        for a in approvals:
            result = provided.get(a.id)
            views.append(ApprovalView(
                id=a.id, task_id=a.task_id, type=a.type, status=a.status, scope=a.scope, target=a.target,
                request_payload=a.request_payload, checkpoint_step_key=a.checkpoint_step_key, valid_until=a.valid_until,
                used_at=a.used_at, created_at=a.created_at,
                options=None if result is None else result.options,
                options_hidden=None if result is None else list(result.options_hidden),
                operator_request=operator_request_of(requests.get(a.id))))
        return views

    async def approvals(self, task_id: uuid.UUID) -> list[ApprovalView]:
        await self.get_task(task_id)
        async with self.db.transaction() as s:
            return await self._views(s, await ApprovalRepository(s).for_task(task_id))

    async def get_approval(self, approval_id: uuid.UUID) -> ApprovalView:
        async with self.db.transaction() as s:
            approval = await ApprovalRepository(s).get(approval_id)
            if approval is None:
                raise NotFound("approval not found")
            return (await self._views(s, [approval]))[0]

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
