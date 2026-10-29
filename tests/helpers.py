import uuid
from datetime import UTC, datetime, timedelta

from app.models.tables import Conversation, Execution, Task
from app.services.hashing import stable_hash
from app.validation import policies as P


class FakeClock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def make_task(db, *, status="PENDING", requirements="build a small thing", conversation_id=None,
                    limits=None, test_policy=None) -> Task:
    async with db.transaction() as s:
        if conversation_id is None:
            conv = Conversation(id=uuid.uuid4(), status="ACTIVE", version=0)
            s.add(conv)
            await s.flush()
            conversation_id = conv.id
        policy = test_policy or P.DEFAULT_TEST_POLICY
        task = Task(
            id=uuid.uuid4(), conversation_id=conversation_id, requirements=requirements,
            requirements_hash=stable_hash(requirements), status=status,
            limits=limits or {"task_max_wall_seconds": 3600, "max_cli_invocations": 40,
                              "max_workflow_iterations": 3, "max_step_attempts": 2, "max_replans": 2},
            test_policy=policy, test_policy_hash=stable_hash(policy), plan_revision=0, workflow_iteration=0,
            replan_count=0, cli_invocations=0, run_seconds=0.0, decisions_hash="", row_version=0)
        s.add(task)
        await s.flush()
        return task


async def make_step(db, task, *, step_key="DEVELOP", status="RUNNING", attempt=1, revision=0, iteration=0,
                    run_id=None, input_hash="in", output=None):
    from app.models.tables import TaskStep
    async with db.transaction() as s:
        step = TaskStep(id=uuid.uuid4(), task_id=task.id, run_id=run_id, plan_revision=revision,
                        workflow_iteration=iteration, step_key=step_key, step_attempt=attempt, status=status,
                        input_hash=input_hash, output=output)
        s.add(step)
        await s.flush()
        return step


async def add_execution(db, task, run_id, *, pid=None, create_time=None, finished=False, outcome="STARTING"):
    async with db.transaction() as s:
        ex = Execution(id=uuid.uuid4(), task_id=task.id, run_id=run_id, role="DEVELOPER", provider="CLAUDE",
                       outcome=outcome, pid=pid, process_create_time=create_time,
                       finished_at=datetime.now(UTC) if finished else None)
        s.add(ex)
        await s.flush()
        return ex
