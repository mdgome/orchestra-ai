"""Full-stack test harness: real PostgreSQL + real services + MockProviders (no external CLI)."""
import asyncio
from pathlib import Path

from sqlalchemy import select

from app.infra.command_executor import CommandExecutor
from app.infra.event_bus import EventBus
from app.infra.workspace_guard import WorkspaceGuard
from app.models.states import CliOutcome
from app.models.tables import Approval, Execution, StateTransition, Task, TaskRun, TaskStep
from app.providers.base import ProviderResult
from app.providers.mock import MockCall, MockProvider, ok
from app.repositories.approval_repository import ExecutionRequestRepository
from app.services.human_gate_service import HumanGateService
from app.services.lease_service import LeaseService
from app.services.runner import Runner
from app.services.session_service import SessionService
from app.services.step_runner import StepRunner
from app.services.task_service import TaskService
from app.services.test_policy_service import TestPolicyService
from app.services.workflow_service import WorkflowService

PLAN = {
    "requirements": "add(a, b) returns the sum", "design_direction": "single module with one pure function",
    "changed_files": ["calc.py", "tests/test_calc.py"], "implementation_steps": ["write calc.add"],
    "test_plan": ["test add"], "acceptance_criteria": ["add(2, 3) == 5"], "tradeoffs": ["none"],
    "human_gate_request": None,
}
GATE = {"problem": "sync or async?", "options": [
    {"id": "A", "title": "sync", "pros": "simple", "cons": "blocking"},
    {"id": "B", "title": "async", "pros": "scalable", "cons": "complex"}],
    "complexity": "A is simpler", "maintainability": "A is easier", "recommendation": "A", "impact": "one module"}
GOOD_CODE = "def add(a, b):\n    return a + b\n"
BAD_CODE = "def add(a, b):\n    return a - b\n"
TEST_CODE = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def structured(**data) -> ProviderResult:
    return ok(text="done", structured=data)


def planner_ok(call: MockCall) -> ProviderResult:
    return structured(**PLAN)


def design_pass(call: MockCall) -> ProviderResult:
    return structured(verdict="PASS", issues=[], summary="fine", human_gate_request=None)


def developer_good(call: MockCall) -> ProviderResult:
    (Path(call.cwd) / "calc.py").write_text(GOOD_CODE, encoding="utf-8")
    return structured(summary="implemented add", files_changed=["calc.py"], human_gate_request=None)


def write_tests_ok(call: MockCall) -> ProviderResult:
    tests = Path(call.cwd) / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_calc.py").write_text(TEST_CODE, encoding="utf-8")
    return structured(summary="added test", tests_added=["tests/test_calc.py"], human_gate_request=None)


def review_pass(call: MockCall) -> ProviderResult:
    return structured(verdict="PASS", issues=[], summary="good", plan_defect=False, test_policy_concern=False,
                      human_gate_request=None)


def happy_handlers() -> tuple[dict, dict]:
    claude = {"PLANNER": planner_ok, "DEVELOPER": developer_good, "TESTER": write_tests_ok}
    codex = {"DESIGN_REVIEWER": design_pass, "CODE_REVIEWER": review_pass}
    return claude, codex


class Harness:
    def __init__(self, db, workspace_root: Path, claude_handlers=None, codex_handlers=None, *, ttl_seconds=30,
                 terminator=None, cancel_poll=0.1):
        ch, xh = happy_handlers()
        ch.update(claude_handlers or {})
        xh.update(codex_handlers or {})
        self.db = db
        self.guard = WorkspaceGuard(workspace_root)
        self.events = EventBus()
        self.claude = MockProvider("CLAUDE", ch)
        self.codex = MockProvider("CODEX", xh)
        self.log = self.claude.log = self.codex.log = []     # chronological role log across providers
        kw = {"terminator": terminator} if terminator else {}
        self.leases = LeaseService(db, "runner-test", ttl_seconds=ttl_seconds, **kw)
        self.sessions = SessionService()
        self.steps = StepRunner(db, self.leases, self.sessions, {"CLAUDE": self.claude, "CODEX": self.codex},
                                self.events, cancel_poll_seconds=cancel_poll)
        self.gates = HumanGateService(db)
        self.tests = TestPolicyService(CommandExecutor(self.guard))
        self.workflow = WorkflowService(db, self.leases, self.steps, self.gates, self.tests, self.guard,
                                        self.sessions, self.events)
        self.runner = Runner(db, self.leases, self.workflow, self.gates, [self.claude, self.codex],
                             advisory_key=987_654_321, claim_timeout_seconds=300, poll_seconds=0.05)
        self.tasks = TaskService(db, self.guard)

    # --- driving ---------------------------------------------------------------------------------------------
    async def new_task(self, requirements="implement add(a, b)", **kw):
        return await self.tasks.create_task(requirements=requirements, **kw)

    async def run_until_idle(self, limit=20) -> list[str]:
        """Consume every PENDING execution request (START and RESUME) the way the Runner loop would."""
        outcomes = []
        for _ in range(limit):
            async with self.db.transaction() as s:
                request = await ExecutionRequestRepository(s).claim_next(self.runner.runner_id)
            if request is None:
                break
            outcomes.append(await self.runner.process(request))
        return outcomes

    async def start(self, task_id):
        await self.tasks.request_execution(task_id)
        return await self.run_until_idle()

    async def decide(self, approval_id, decision="APPROVED", option="A", key="k"):
        return await self.gates.decide(approval_id, decision=decision, selected_option=option, rationale=None,
                                       idempotency_key=key)

    # --- inspection ------------------------------------------------------------------------------------------
    async def task(self, task_id) -> Task:
        async with self.db.transaction() as s:
            return (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()

    async def steps_of(self, task_id) -> list[TaskStep]:
        async with self.db.transaction() as s:
            rows = await s.execute(select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.created_at))
            return list(rows.scalars())

    async def runs(self, task_id) -> list[TaskRun]:
        async with self.db.transaction() as s:
            return list((await s.execute(select(TaskRun).where(TaskRun.task_id == task_id))).scalars())

    async def approvals(self, task_id) -> list[Approval]:
        async with self.db.transaction() as s:
            return list((await s.execute(select(Approval).where(Approval.task_id == task_id))).scalars())

    async def executions(self, task_id) -> list[Execution]:
        async with self.db.transaction() as s:
            rows = await s.execute(select(Execution).where(Execution.task_id == task_id).order_by(Execution.started_at))
            return list(rows.scalars())

    async def transitions(self, task_id) -> list[StateTransition]:
        async with self.db.transaction() as s:
            rows = await s.execute(select(StateTransition).where(StateTransition.task_id == task_id)
                                   .order_by(StateTransition.id))
            return list(rows.scalars())

    async def state_path(self, task_id) -> list[str]:
        return [t.to_state for t in await self.transitions(task_id) if t.kind == "TASK_STATE"]

    @staticmethod
    def roles_called(provider: MockProvider) -> list[str]:
        return [c.role for c in provider.calls]


async def wait_for(predicate, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if await predicate():
            return True
        await asyncio.sleep(0.05)
    return False


__all__ = ["Harness", "PLAN", "GATE", "GOOD_CODE", "BAD_CODE", "TEST_CODE", "structured", "wait_for", "ok",
           "CliOutcome", "planner_ok", "design_pass", "developer_good", "write_tests_ok", "review_pass"]
