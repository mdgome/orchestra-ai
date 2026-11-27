"""E1: creating a Project Test Run (TaskService.create_test_run): the part that only touches the database. Running one is in
test_test_only_task.py."""
import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.models.tables import ExecutionRequest, Project, Task
from app.services.project_service import ProjectService
from app.validation import policies as P
from app.validation.exceptions import ActiveTestRunExists, Conflict, IdempotencyKeyReused, NotFound, ValidationFailed
from tests.harness import Harness

PASSING = {"test_ok.py": "def test_ok():\n    assert True\n", "user.txt": "the user's own file\n"}


async def make_project(h: Harness, tmp_path: Path, files: dict, name: str = "svc"):
    root = tmp_path / "projects" / name
    root.mkdir(parents=True)
    for rel, content in files.items():
        (root / rel).write_text(content, encoding="utf-8")
    service = ProjectService(h.db, None, clone_root=tmp_path / "clones", protected=(h.guard.root,))
    return await service.create_project(name=name, workspace_path=str(root)), root


async def request_of(h: Harness, task_id) -> list[ExecutionRequest]:
    async with h.db.transaction() as s:
        return list((await s.execute(select(ExecutionRequest).where(ExecutionRequest.task_id == task_id))).scalars())


async def test_creating_a_test_run_records_a_pending_test_only_task_and_its_start_request(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path, PASSING)
    task, replayed = await h.tasks.create_test_run(project.id)
    assert not replayed
    row = await h.task(task.id)
    assert row.mode == "TEST_ONLY" and row.status == "PENDING" and row.project_id == project.id
    assert row.requirements == P.TEST_ONLY_REQUIREMENTS and row.workspace_path == str(root.resolve())
    assert row.test_policy_hash and row.test_policy["commands"] and row.idempotency_key is None
    requests = await request_of(h, task.id)
    assert [(r.kind, r.status) for r in requests] == [("START", "PENDING")]
    assert (await h.tasks.get_task(task.id)).id == task.id
    assert [t.to_state for t in await h.transitions(task.id)] == ["PENDING"]


async def test_a_test_run_copies_the_profile_exactly_like_a_task(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, PASSING)
    run, _ = await h.tasks.create_test_run(project.id)
    ordinary = await h.tasks.create_task(requirements="implement add", project_id=project.id)
    a, b = await h.task(run.id), await h.task(ordinary.id)
    assert (a.profile_version, a.profile_hash, a.profile_snapshot) == (b.profile_version, b.profile_hash, b.profile_snapshot)
    assert a.test_policy_hash == b.test_policy_hash and a.workspace_path == b.workspace_path


async def test_a_test_run_needs_an_active_project(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    with pytest.raises(NotFound):
        await h.tasks.create_test_run(uuid.uuid4())
    project, _root = await make_project(h, tmp_path, PASSING)
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project.id).values(status="ARCHIVED"))
    with pytest.raises(Conflict, match="not ACTIVE"):
        await h.tasks.create_test_run(project.id)


async def test_the_idempotency_key_replays_the_same_run_and_refuses_another_meaning(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, PASSING)
    first, replayed = await h.tasks.create_test_run(project.id, idempotency_key="k1")
    again, replayed_again = await h.tasks.create_test_run(project.id, idempotency_key="k1")
    assert not replayed and replayed_again and again.id == first.id            # in progress: the same run
    assert len(await request_of(h, first.id)) == 1
    async with db.transaction() as s:                                          # the same key, but the policy changed
        await s.execute(update(Task).where(Task.id == first.id).values(request_fingerprint="0" * 64))
    with pytest.raises(IdempotencyKeyReused) as caught:
        await h.tasks.create_test_run(project.id, idempotency_key="k1")
    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED" and caught.value.status_code == 409


async def test_a_project_runs_one_test_run_at_a_time(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, PASSING)
    first, _ = await h.tasks.create_test_run(project.id)
    with pytest.raises(ActiveTestRunExists) as caught:
        await h.tasks.create_test_run(project.id)
    assert caught.value.code == "TEST_RUN_ACTIVE" and caught.value.details["test_run_id"] == str(first.id)
    other, _root2 = await make_project(h, tmp_path, PASSING, name="other")        # another project is not affected
    await h.tasks.create_test_run(other.id)


async def test_concurrent_creations_make_exactly_one_run(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, PASSING)
    results = await asyncio.gather(*[h.tasks.create_test_run(project.id, idempotency_key="same") for _ in range(4)],
                                   return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    assert len({task.id for task, _ in ok}) == 1 and sorted(replayed for _, replayed in ok) == [False, True, True, True]
    results = await asyncio.gather(*[h.tasks.create_test_run(project.id) for _ in range(3)], return_exceptions=True)
    assert all(isinstance(r, ActiveTestRunExists) for r in results)                 # the first run is still active


@pytest.mark.parametrize("key", ["", " x", "x ", "a" * 129, "tab\tkey", "kéy"])
async def test_a_bad_idempotency_key_is_rejected(db, tmp_path, key):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, PASSING)
    with pytest.raises(ValidationFailed):
        await h.tasks.create_test_run(project.id, idempotency_key=key)


async def test_a_test_run_cannot_be_edited_through_the_ordinary_task_update(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, PASSING)
    task, _ = await h.tasks.create_test_run(project.id)
    with pytest.raises(Conflict, match="cannot be modified"):
        await h.tasks.update_task(task.id, requirements="something else entirely")
    assert (await h.task(task.id)).requirements == P.TEST_ONLY_REQUIREMENTS
