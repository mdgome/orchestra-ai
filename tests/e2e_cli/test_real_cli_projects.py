"""Real Codex + Claude CLIs on REGISTERED PROJECTS (slow, costs tokens): `pytest -m e2e_cli -k projects`.

1. one external project: the workflow runs in the registered workspace, user files stay, no per-task directory;
2. three tasks over two projects through the real Runner loop: different projects run in parallel, tasks of the
   same project never overlap (project cap + write lock), everything completes.
"""
import asyncio
import shutil
import subprocess
import time

import pytest
from sqlalchemy import select

from app.config import Settings
from app.container import build_container
from app.models.tables import Execution, Project, Task

pytestmark = [
    pytest.mark.e2e_cli,
    pytest.mark.skipif(not (shutil.which("claude") and shutil.which("codex")), reason="CLIs not installed"),
]

LIMITS = {"max_cli_invocations": 25, "max_workflow_iterations": 1, "max_replans": 1}


def make_settings(tmp_path, **kw) -> Settings:
    return Settings(workspace_root=tmp_path / "ws", project_clone_root=tmp_path / "clones",
                    claude_bin="claude", codex_bin="codex", **kw)


def make_project_dir(tmp_path, name: str, *, git: bool):
    root = tmp_path / "projects" / name
    root.mkdir(parents=True)
    (root / "README.md").write_text(f"# {name}\nThe user's own file; it must survive the workflow.\n", encoding="utf-8")
    if git:
        env = {"GIT_CONFIG_GLOBAL": "NUL" if shutil.os.name == "nt" else "/dev/null"}
        run = lambda *a: subprocess.run(["git", "-C", str(root), "-c", "user.name=e2e", "-c", "user.email=e2e@x.io",
                                         "-c", "commit.gpgsign=false", *a], check=True, capture_output=True,
                                        env={**shutil.os.environ, **env})
        run("init", "-q")
        run("add", "-A")
        run("commit", "-q", "-m", "initial")
    return root


async def approve_pending(c, task_ids) -> int:
    """The 'human': approve the first offered option of every pending DESIGN gate."""
    approved = 0
    for task_id in task_ids:
        for a in await c.tasks.approvals(task_id):
            if a.status == "PENDING" and a.type == "DESIGN":
                option = a.request_payload["options"][0]["id"]
                await c.gates.decide(a.id, decision="APPROVED", selected_option=option, rationale="e2e auto-approval",
                                     idempotency_key=f"e2e-{a.id}")
                print(" human approves", a.request_payload.get("problem", "")[:120], "->", option)
                approved += 1
    return approved


async def describe(c, db, task_ids) -> None:
    """Progress line for the log: task status, steps, open approvals and request state."""
    from app.models.tables import ExecutionRequest
    from app.repositories.task_repository import TaskRepository
    async with db.transaction() as s:
        for task_id in task_ids:
            task = await TaskRepository(s).get(task_id)
            steps = [f"{x.step_key}#{x.step_attempt}:{x.status}" for x in await TaskRepository(s).steps_for_task(task_id)]
            reqs = [f"{r.kind}:{r.status}:defer{r.defer_count}:{r.note}" for r in (await s.execute(
                select(ExecutionRequest).where(ExecutionRequest.task_id == task_id))).scalars()]
            print(f"[{time.strftime('%H:%M:%S')}] task {str(task_id)[:8]} {task.status} {task.failure_reason} "
                  f"steps={steps} reqs={reqs} inflight={c.runner.in_flight}", flush=True)


async def wait_until_done(c, db, task_ids, timeout=900) -> None:
    deadline = time.monotonic() + timeout
    last = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() - last > 60:
            last = time.monotonic()
            await describe(c, db, task_ids)
        await approve_pending(c, task_ids)
        async with db.transaction() as s:
            statuses = [t.status for t in (await s.execute(select(Task).where(Task.id.in_(task_ids)))).scalars()]
        if all(x in ("COMPLETED", "FAILED", "CANCELLED") for x in statuses):
            return
        await asyncio.sleep(3)
    raise AssertionError("timed out waiting for the tasks")


async def execution_span(db, task_id) -> tuple:
    async with db.transaction() as s:
        rows = list((await s.execute(select(Execution).where(Execution.task_id == task_id))).scalars())
    assert rows, "no CLI execution was recorded"
    return min(r.started_at for r in rows), max(r.finished_at for r in rows if r.finished_at)


async def test_projects_one_external_workspace_with_the_real_clis(db, tmp_path):
    c = build_container(make_settings(tmp_path), db=db)
    root = make_project_dir(tmp_path, "service-a", git=True)
    project = await c.projects.create_project(name="Service A", workspace_path=str(root))
    task = await c.tasks.create_task(
        requirements=("Create calc.py with two pure functions: add(a, b) returning a + b and subtract(a, b) returning "
                      "a - b. Do not modify README.md. No other files except tests."),
        limits=LIMITS, project_id=project.id)
    await c.tasks.request_execution(task.id)
    await c.runner.start()
    try:
        await wait_until_done(c, db, [task.id])
    finally:
        await c.runner.stop()

    final = await c.tasks.get_task(task.id)
    print("final:", final.status, final.failure_reason)
    assert final.status == "COMPLETED", (final.status, final.failure_reason)
    assert (root / "calc.py").exists() and (root / "README.md").read_text(encoding="utf-8").startswith("# service-a")
    assert not (tmp_path / "ws" / str(task.id)).exists()                        # no per-task workspace was created
    async with db.transaction() as s:
        row = (await s.execute(select(Project).where(Project.id == project.id))).scalar_one()
    assert row.write_owner_task_id is None and row.write_baseline_fingerprint is None
    assert row.workspace_state == "CLEAN" and row.baseline_fingerprint.startswith("WORKSPACE_FINGERPRINT_V1:")


async def test_projects_parallel_across_projects_and_serial_within_one(db, tmp_path):
    c = build_container(make_settings(tmp_path, max_concurrent_tasks=2, max_tasks_per_project=1), db=db)
    a_root = make_project_dir(tmp_path, "service-a", git=True)
    b_root = make_project_dir(tmp_path, "service-b", git=False)
    proj_a = await c.projects.create_project(name="Service A", workspace_path=str(a_root))
    proj_b = await c.projects.create_project(name="Service B", workspace_path=str(b_root))
    req = "Create {name}.py with one pure function {name}(x) returning x * 2. No other files except tests."
    a1 = await c.tasks.create_task(requirements=req.format(name="doubler"), limits=LIMITS, project_id=proj_a.id)
    a2 = await c.tasks.create_task(requirements=req.format(name="twice"), limits=LIMITS, project_id=proj_a.id)
    b1 = await c.tasks.create_task(requirements=req.format(name="timestwo"), limits=LIMITS, project_id=proj_b.id)
    ids = [a1.id, a2.id, b1.id]
    for task in (a1, a2, b1):
        await c.tasks.request_execution(task.id)
    await c.runner.start()
    try:
        await wait_until_done(c, db, ids, timeout=3600)
    finally:
        await c.runner.stop()

    for task in (a1, a2, b1):
        final = await c.tasks.get_task(task.id)
        print(task.id, final.status, final.failure_reason)
        assert final.status == "COMPLETED", (final.status, final.failure_reason)
    assert (a_root / "doubler.py").exists() and (a_root / "twice.py").exists() and (b_root / "timestwo.py").exists()

    (a1_start, a1_end), (a2_start, a2_end), (b_start, b_end) = [await execution_span(db, t.id) for t in (a1, a2, b1)]
    print("A1", a1_start, a1_end, "A2", a2_start, a2_end, "B", b_start, b_end)
    assert not (a1_start < a2_end and a2_start < a1_end), "two tasks of the same project overlapped"
    assert any(a_start < b_end and b_start < a_end for a_start, a_end in ((a1_start, a1_end), (a2_start, a2_end))), \
        "no task of project A overlapped the task of project B"
    async with db.transaction() as s:
        for project in (proj_a, proj_b):
            row = (await s.execute(select(Project).where(Project.id == project.id))).scalar_one()
            assert row.write_owner_task_id is None and row.workspace_state == "CLEAN"
