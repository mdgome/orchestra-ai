"""GET /tasks/{id}/changes over HTTP: real PostgreSQL, real git, MockProviders (no CLI). The workflow produces the baseline; the
developer mock edits the workspace; the view then compares the live workspace with that baseline."""
import asyncio
import os
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

from app.config import Settings
from app.container import Container
from app.infra import changes_manifest as CM
from app.infra import workspace_fingerprint as WF
from app.infra.response_sanitizer import TRUNCATION_MARK
from app.infra.workspace_fingerprint import compute_fingerprint
from app.main import create_app
from app.models.tables import TaskBaseline
from app.services.change_service import READ_ONLY_GIT_SUBCOMMANDS, ChangeService, MeasurementGate
from app.services.project_service import ProjectService
from app.validation import policies as P
from tests.api.test_api import assert_envelope
from tests.harness import GOOD_CODE, Harness, structured, write_tests_ok
from tests.integration_pg.test_project_lock import TESTER_GATE, claim_and_process, make_project, new_task
from tests.integration_pg.test_task_baseline import git, git_project

UNKNOWN = "00000000-0000-0000-0000-000000000000"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def build(db, h, clock=None):
    service = ChangeService(db, clock=clock or Clock()) if clock is None else ChangeService(db, clock=clock)
    container = Container(settings=Settings(), db=db, guard=h.guard, executor=None, events=h.events, tasks=h.tasks,
                          gates=h.gates, runner=h.runner, providers={"CLAUDE": h.claude, "CODEX": h.codex}, changes=service)
    app = create_app(container=container, run_preflight=False)
    app.state.container = container
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), service


def developer(*edits):
    """Writes the code the tests need and then applies the given edits to the project workspace."""
    def handler(call):
        root = Path(call.cwd)
        (root / "calc.py").write_text(GOOD_CODE, encoding="utf-8")
        for edit in edits:
            edit(root)
        return structured(summary="changed things", files_changed=["calc.py"], human_gate_request=None)
    return handler


async def completed(db, tmp_path, *edits, project_factory=git_project, handlers=None):
    h = Harness(db, tmp_path / "ws", claude_handlers={"DEVELOPER": developer(*edits), **(handlers or {})})
    project, root = await project_factory(h, tmp_path)
    task = await new_task(h, project)
    return h, project, root, task


async def get_changes(client, task_id, **params):
    response = await client.get(f"/tasks/{task_id}/changes", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def by_path(body):
    return {c["path"]: c for c in body["changes"]}


# --- what is reported, per kind of change ---------------------------------------------------------------------------------------------
async def test_the_cumulative_changes_since_the_lock_with_diff_bodies_where_git_can_supply_the_old_content(db, tmp_path):
    h, project, root, task = await completed(db, tmp_path, lambda r: (r / "tracked.txt").write_text("changed by the task\n"))
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    calls = (len(h.claude.calls), len(h.codex.calls))
    async with client:
        body = await get_changes(client, task.id)
    assert (body["state"], body["precision"], body["supported_scope"]) == ("AVAILABLE", "HASH_COMPARISON", "GIT_TRACKED_AND_UNTRACKED_NON_IGNORED")
    assert "LIVE" in body["note"] and body["workspace_relation"] == "UNOWNED_AFTER_TASK"
    changes = by_path(body)
    assert changes["calc.py"]["change_type"] == "ADDED" and "+def add(a, b):" in changes["calc.py"]["diff"]
    tracked = changes["tracked.txt"]
    assert tracked["change_type"] == "MODIFIED" and tracked["diff_status"] == "AVAILABLE" and tracked["pre_existing_dirty"] is False
    assert "-committed" in tracked["diff"] and "+changed by the task" in tracked["diff"]
    assert "tests/test_calc.py" in changes                                                    # the tester's file
    assert "edited.txt" not in changes and "untracked.txt" not in changes                     # dirty before, untouched: not the task's
    assert not any(p.startswith(("build/", "reports/")) for p in changes)                     # ignored / volatile
    assert body["diff_basis"]["mode"] == "GIT" and body["diff_basis"]["head_kind"] == "COMMIT"
    assert (len(h.claude.calls), len(h.codex.calls)) == calls                                 # reading runs no agent


async def test_a_file_that_was_already_dirty_is_flagged_and_never_given_a_body(db, tmp_path):
    h, project, root, task = await completed(db, tmp_path, lambda r: (r / "edited.txt").write_text("the task edits it again\n"))
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        item = by_path(await get_changes(client, task.id))["edited.txt"]
    assert item["change_type"] == "MODIFIED" and item["pre_existing_dirty"] is True
    assert (item["diff_status"], item["diff_reason"], item["diff"]) == ("UNAVAILABLE", "BASELINE_CONTENT_NOT_STORED", None)


async def test_removed_renamed_binary_lfs_and_symlink_changes(db, tmp_path):
    def edits(root):
        (root / "tracked.txt").rename(root / "moved.txt")
        (root / "data.bin").write_bytes(b"\x00\x01\x02 binary")
        (root / "model.bin").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10\n")
        try:
            os.symlink("tracked-elsewhere.txt", root / "alias")
        except (OSError, NotImplementedError):
            pass
    h, project, root, task = await completed(db, tmp_path, edits)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        changes = by_path(await get_changes(client, task.id))
    assert (changes["moved.txt"]["change_type"], changes["moved.txt"]["previous_path"]) == ("RENAMED", "tracked.txt")
    assert "tracked.txt" not in changes
    assert changes["data.bin"]["binary"] is True and changes["data.bin"]["diff_reason"] == "BINARY" and changes["data.bin"]["diff"] is None
    assert changes["model.bin"]["diff_reason"] == "LFS_POINTER" and changes["model.bin"]["diff"] is None
    if "alias" in changes:
        assert changes["alias"]["kind"] == "SYMLINK" and changes["alias"]["diff_reason"] == "SYMLINK_TARGET_ONLY"
        assert changes["alias"]["target"] == "tracked-elsewhere.txt" and changes["alias"]["diff"] is None


async def test_a_deleted_clean_file_is_removed_with_its_old_content_as_the_body(db, tmp_path):
    h, project, root, task = await completed(db, tmp_path, lambda r: (r / "tracked.txt").unlink())
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        item = by_path(await get_changes(client, task.id))["tracked.txt"]
    assert item["change_type"] == "REMOVED" and item["diff_status"] == "AVAILABLE" and "-committed" in item["diff"]


async def test_sensitive_files_are_listed_but_their_content_is_never_returned(db, tmp_path):
    def edits(root):
        (root / ".env").write_text("DB_PASSWORD=hunter2hunter2\n")
        (root / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\nMIIsecretkeymaterial\n")
    h, project, root, task = await completed(db, tmp_path, edits)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        response = await client.get(f"/tasks/{task.id}/changes")
    changes = by_path(response.json())
    for name in (".env", "server.pem"):
        assert (changes[name]["diff_status"], changes[name]["diff_reason"], changes[name]["diff"]) == ("WITHHELD", "SENSITIVE_PATH", None)
    assert "hunter2" not in response.text and "secretkeymaterial" not in response.text


async def test_credentials_inside_an_ordinary_diff_body_are_masked(db, tmp_path):
    h, project, root, task = await completed(
        db, tmp_path, lambda r: (r / "notes.txt").write_text("push to https://user:s3cretT0ken@host/x.git with ghp_abcdefghijklmnop1234\n"))
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        response = await client.get(f"/tasks/{task.id}/changes")
    assert "s3cretT0ken" not in response.text and "ghp_abcdefghijklmnop1234" not in response.text
    assert "***" in by_path(response.json())["notes.txt"]["diff"]


async def test_large_files_and_the_body_budget_are_reported_not_hidden(db, tmp_path, monkeypatch):
    monkeypatch.setattr(P, "CHANGES_FILE_DIFF_MAX_BYTES", 200)
    monkeypatch.setattr(P, "CHANGES_TOTAL_DIFF_MAX_BYTES", 300)
    monkeypatch.setattr(P, "CHANGES_MAX_DIFF_FILES", 3)

    def edits(root):
        (root / "big.txt").write_text("x" * 1000)
        for i in range(4):
            (root / f"small{i}.txt").write_text(f"line {i}\n" * 3)
    h, project, root, task = await completed(db, tmp_path, edits)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        changes = by_path(await get_changes(client, task.id))
    assert (changes["big.txt"]["diff_status"], changes["big.txt"]["diff_reason"]) == ("TRUNCATED", "FILE_TOO_LARGE")
    statuses = [changes[f"small{i}.txt"] for i in range(4)]
    assert any(s["diff_status"] == "TRUNCATED" and s["diff_reason"] == "BODY_BUDGET" for s in statuses)
    assert any(s["diff_status"] == "AVAILABLE" for s in statuses)


async def test_without_git_only_paths_and_hashes_are_compared_and_there_are_no_bodies(db, tmp_path):
    h, project, root, task = await completed(db, tmp_path, lambda r: (r / "user.txt").write_text("changed\n"),
                                             project_factory=make_project)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        body = await get_changes(client, task.id)
    assert body["supported_scope"] == "CONTENT_WALK_POLICY_FILTERED" and body["diff_basis"]["mode"] == "CONTENT"
    assert any("not a git workspace" in text for text in body["limitations"])
    changes = by_path(body)
    assert changes["user.txt"]["change_type"] == "MODIFIED" and changes["calc.py"]["change_type"] == "ADDED"
    assert all(c["diff"] is None and c["diff_reason"] == "NO_BASELINE_CONTENT_WITHOUT_GIT" for c in changes.values())


async def test_an_unborn_head_lists_changes_but_has_no_bodies_for_edits_of_existing_files(db, tmp_path):
    h = Harness(db, tmp_path / "ws", claude_handlers={"DEVELOPER": developer(lambda r: (r / "x.txt").write_text("edited\n"))})
    root = tmp_path / "projects" / "fresh"
    root.mkdir(parents=True)
    git(root, "init", "-q")
    (root / "x.txt").write_text("x\n")
    project = await ProjectService(h.db, None, clone_root=tmp_path / "clones", protected=(h.guard.root,)).create_project(
        name="fresh", workspace_path=str(root))
    task = await new_task(h, project)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        body = await get_changes(client, task.id)
    changes = by_path(body)
    assert body["diff_basis"]["head_kind"] == "UNBORN" and body["diff_basis"]["head_sha"] is None
    assert changes["x.txt"]["diff_reason"] == "UNBORN_HEAD" and changes["x.txt"]["diff"] is None
    assert changes["calc.py"]["change_type"] == "ADDED" and changes["calc.py"]["diff_status"] == "AVAILABLE"


# --- the states of a baseline -----------------------------------------------------------------------------------------------------
async def test_a_task_without_a_baseline_or_without_a_project_says_so_and_an_unknown_task_is_a_404(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    pending = await new_task(h, project)
    plain = await h.new_task()
    client, _ = build(db, h)
    async with client:
        first, second = await get_changes(client, pending.id), await get_changes(client, plain.id)
        assert_envelope(await client.get(f"/tasks/{UNKNOWN}/changes"), 404, "NOT_FOUND")
    assert (first["state"], first["unavailable_reason"], first["precision"]) == ("UNAVAILABLE", "NO_BASELINE", "NONE")
    assert (second["state"], second["unavailable_reason"]) == ("UNAVAILABLE", "NO_PROJECT") and first["changes"] == []


async def test_a_baseline_that_could_not_be_collected_is_reported_with_its_reason(db, tmp_path, monkeypatch):
    h, project, root, task = await completed(db, tmp_path)
    client, _ = build(db, h)

    def failing(*_a, **_k):
        raise CM.ManifestFailure("TOO_MANY_FILES")

    monkeypatch.setattr(CM, "measure", failing)
    assert await h.start(task.id) == ["COMPLETED"]
    monkeypatch.undo()
    async with client:
        body = await get_changes(client, task.id)
    assert (body["state"], body["unavailable_reason"], body["changes"]) == ("UNAVAILABLE", "TOO_MANY_FILES", [])


@pytest.mark.parametrize("damage,reason", [
    ({"manifest": b"this is not gzip"}, "MANIFEST_CORRUPT"), ({"manifest_sha256": "0" * 64}, "MANIFEST_CORRUPT"),
    ({"manifest_version": "CHANGES_MANIFEST_V9"}, "UNSUPPORTED_MANIFEST_VERSION"),
    ({"uncompressed_bytes": 10}, "MANIFEST_TOO_LARGE")])
async def test_a_damaged_stored_manifest_is_an_unavailable_answer_not_a_server_error(db, tmp_path, damage, reason):
    h, project, root, task = await completed(db, tmp_path)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with h.db.transaction() as s:
        await s.execute(update(TaskBaseline).where(TaskBaseline.task_id == task.id).values(**damage))
    async with client:
        body = await get_changes(client, task.id)
    assert (body["state"], body["unavailable_reason"]) == ("UNAVAILABLE", reason)


async def test_a_workspace_that_stopped_being_git_cannot_be_compared_like_for_like(db, tmp_path):
    h, project, root, task = await completed(db, tmp_path)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    import shutil
    shutil.rmtree(root / ".git", onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
    async with client:
        body = await get_changes(client, task.id)
    assert (body["state"], body["unavailable_reason"]) == ("UNAVAILABLE", "MODE_UNAVAILABLE")


# --- who owns the workspace now -------------------------------------------------------------------------------------------------------
async def test_the_owner_sees_bodies_and_another_task_sees_none_while_it_owns_the_workspace(db, tmp_path):
    gated = lambda call: TESTER_GATE(call) if call.n == 1 else write_tests_ok(call)  # noqa: E731
    h = Harness(db, tmp_path / "ws", claude_handlers={"DEVELOPER": developer(lambda r: (r / "tracked.txt").write_text("changed\n")),
                                                      "TESTER": gated})
    project, root = await git_project(h, tmp_path)
    finished = await new_task(h, project, "implement the first thing")
    first_run = Harness  # noqa: F841 - documentation: the finished task ran earlier, before the owner below
    # run `finished` to completion first (tester gate answered), then start another task that stops at a gate holding the lock
    assert await claim_and_process(h, finished.id) == "WAITING_APPROVAL"
    gate = next(a for a in await h.approvals(finished.id) if a.status == "PENDING")
    await h.decide(gate.id)
    assert await h.run_until_idle() == ["COMPLETED"]
    owner = await new_task(h, project, "implement the second thing")
    h.claude.handlers["TESTER"] = lambda call: TESTER_GATE(call) if call.n == 1 else write_tests_ok(call)
    h.claude._counts["TESTER"] = 0
    assert await claim_and_process(h, owner.id) == "WAITING_APPROVAL"
    client, _ = build(db, h)
    async with client:
        mine = await get_changes(client, owner.id)
        theirs = await get_changes(client, finished.id)
    assert mine["workspace_relation"] == "CURRENT_OWNER"
    assert theirs["workspace_relation"] == "OTHER_TASK_OWNS" and theirs["changes"]
    assert {c["diff_reason"] for c in theirs["changes"] if c["diff_status"] == "WITHHELD"} == {"PROJECT_OWNED_BY_OTHER_TASK"}
    assert all(c["diff"] is None for c in theirs["changes"])


# --- consistency: the database and the workspace are separate questions ----------------------------------------------------------------------
async def test_the_workspace_is_unverified_by_default_and_stable_only_after_two_equal_measurements(db, tmp_path):
    h, project, root, task = await completed(db, tmp_path)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        default = await get_changes(client, task.id)
        verified = await get_changes(client, task.id, verify_workspace="true")
    assert (default["lock_consistency"], default["workspace_consistency"], default["workspace_consistency_reason"]) == (
        "STABLE", "UNVERIFIED", "NOT_REQUESTED")
    assert (verified["lock_consistency"], verified["workspace_consistency"]) == ("STABLE", "STABLE")


async def test_an_outside_process_changing_files_without_touching_the_database_makes_the_workspace_unstable_not_the_lock(
        db, tmp_path, monkeypatch):
    h, project, root, task = await completed(db, tmp_path)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    real, calls = CM.measure, {"n": 0}

    def measure_then_someone_edits(r, **kw):
        result = real(r, **kw)
        calls["n"] += 1
        if calls["n"] == 1:
            (Path(r) / "written_by_an_outside_process.txt").write_text("mid-query")
        return result

    monkeypatch.setattr(CM, "measure", measure_then_someone_edits)
    async with client:
        body = await get_changes(client, task.id, verify_workspace="true")
    assert calls["n"] == 2 and body["lock_consistency"] == "STABLE" and body["workspace_consistency"] == "UNSTABLE"


async def test_a_file_changing_during_the_query_is_not_called_stable_even_without_any_database_change(db, tmp_path, monkeypatch):
    h, project, root, task = await completed(db, tmp_path)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    real, calls = CM.measure, {"n": 0}

    def edit_tracked_in_between(r, **kw):
        result = real(r, **kw)
        calls["n"] += 1
        if calls["n"] == 1:
            (Path(r) / "tracked.txt").write_text("edited while the query was running\n")
        return result

    monkeypatch.setattr(CM, "measure", edit_tracked_in_between)
    async with client:
        assert (await get_changes(client, task.id, verify_workspace="true"))["workspace_consistency"] == "UNSTABLE"


@pytest.mark.parametrize("what", ["owner", "epoch", "state"])
async def test_a_change_of_owner_epoch_or_workspace_state_during_the_query_makes_the_lock_unstable(db, tmp_path, what):
    h, project, root, task = await completed(db, tmp_path)
    client, service = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    real_view, seen = service._lock_view, {"n": 0}

    async def drifting(project_id):
        owner, epoch, state = await real_view(project_id)
        seen["n"] += 1
        if seen["n"] % 2 == 0:                                  # every "after" differs from its "before"
            return {"owner": (task.id, epoch, state), "epoch": (owner, (epoch or 0) + 1, state),
                    "state": (owner, epoch, "DIRTY_UNRESOLVED")}[what]
        return owner, epoch, state

    service._lock_view = drifting
    async with client:
        body = await get_changes(client, task.id)
    assert seen["n"] == 4                                         # one discarded attempt and one retry, then the flag
    assert body["lock_consistency"] == "UNSTABLE" and body["state"] == "AVAILABLE"


async def test_one_unstable_attempt_is_retried_and_a_stable_second_attempt_is_reported_stable(db, tmp_path):
    h, project, root, task = await completed(db, tmp_path)
    client, service = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    real_view, seen = service._lock_view, {"n": 0}

    async def once(project_id):
        owner, epoch, state = await real_view(project_id)
        seen["n"] += 1
        return (owner, (epoch or 0) + 1, state) if seen["n"] == 2 else (owner, epoch, state)

    service._lock_view = once
    async with client:
        body = await get_changes(client, task.id)
    assert seen["n"] == 4 and body["lock_consistency"] == "STABLE"


# --- the deadline reaches the measuring code, slots follow the worker --------------------------------------------------------------------------
async def test_an_expired_deadline_is_reported_and_never_as_stable(db, tmp_path, monkeypatch):
    h, project, root, task = await completed(db, tmp_path)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    monkeypatch.setattr(P, "CHANGES_DEADLINE_SECONDS", 0.0)
    async with client:
        body = await get_changes(client, task.id)
    assert (body["state"], body["unavailable_reason"], body["truncated"]) == ("UNAVAILABLE", "DEADLINE", True)


async def test_the_verification_measurement_counts_against_the_same_deadline(db, tmp_path, monkeypatch):
    h, project, root, task = await completed(db, tmp_path)
    clock = Clock()
    client, _ = build(db, h, clock)
    assert await h.start(task.id) == ["COMPLETED"]
    real, calls = CM.measure, {"n": 0}

    def slow_second(r, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            clock.now += 1000                                   # the re-measure finds the deadline already gone
        return real(r, **kw)

    monkeypatch.setattr(CM, "measure", slow_second)
    async with client:
        body = await get_changes(client, task.id, verify_workspace="true")
    assert body["state"] == "AVAILABLE" and body["workspace_consistency"] == "UNVERIFIED"
    assert body["workspace_consistency_reason"] == "DEADLINE"


async def test_a_stalled_worker_keeps_its_slot_blocks_new_workers_and_starts_a_cooldown(db, tmp_path, monkeypatch):
    h, project, root, task = await completed(db, tmp_path)
    clock = Clock()
    client, service = build(db, h, clock)
    assert await h.start(task.id) == ["COMPLETED"]
    release, started, real = threading.Event(), [], CM.measure

    def stalled(r, **kw):                                       # ignores the deadline, like a blocked OS read would
        started.append(1)
        release.wait(20)
        return real(r, **kw)

    monkeypatch.setattr(CM, "measure", stalled)
    monkeypatch.setattr(P, "CHANGES_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(P, "CHANGES_DEADLINE_GRACE_SECONDS", 0.05)
    async with client:
        try:
            first = await get_changes(client, task.id)
            assert (first["state"], first["unavailable_reason"]) == ("UNAVAILABLE", "DEADLINE")
            assert service.gate._total == 1                      # the abandoned worker still owns the slot
            for _ in range(3):                                   # every later request is refused: no new worker starts
                assert_envelope(await client.get(f"/tasks/{task.id}/changes"), 503, "CHANGES_MEASUREMENT_BUSY")
            assert len(started) == 1 and service.gate._total == 1
        finally:
            release.set()
        for _ in range(200):                                     # the slot returns only when the worker really ended
            if service.gate._total == 0:
                break
            await asyncio.sleep(0.02)
        assert service.gate._total == 0 and service.gate._per_project == {}
        monkeypatch.setattr(CM, "measure", real)
        monkeypatch.setattr(P, "CHANGES_DEADLINE_SECONDS", 10.0)
        cooling = await get_changes(client, task.id, verify_workspace="true")      # breaker open: no second measurement
        assert (cooling["workspace_consistency"], cooling["workspace_consistency_reason"]) == ("UNVERIFIED", "COOLDOWN")
        clock.now += P.CHANGES_VERIFY_COOLDOWN_SECONDS + 1
        assert (await get_changes(client, task.id, verify_workspace="true"))["workspace_consistency"] == "STABLE"


def test_the_gate_limits_per_project_and_overall_and_releases_on_demand(monkeypatch):
    import uuid
    gate = MeasurementGate(Clock())
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(P, "CHANGES_MAX_CONCURRENT_GLOBAL", 2)
    assert gate.try_acquire(a) and not gate.try_acquire(a)       # one per project
    assert gate.try_acquire(b) and not gate.try_acquire(c)       # two overall
    gate.release(a)
    assert gate.try_acquire(c)
    assert gate._total == 2


# --- read-only, filters, paging, size -------------------------------------------------------------------------------------------------------
async def test_only_read_only_git_commands_run_and_the_workspace_is_left_exactly_as_it_was(db, tmp_path, monkeypatch):
    h, project, root, task = await completed(db, tmp_path, lambda r: (r / "tracked.txt").write_text("changed\n"))
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    before = compute_fingerprint(root).value
    seen = []
    real = WF._git

    def recording(r, subcommand, **kw):
        seen.append(subcommand[0])
        return real(r, subcommand, **kw)

    monkeypatch.setattr(WF, "_git", recording)
    async with client:
        await get_changes(client, task.id, verify_workspace="true")
    assert seen and set(seen) <= set(READ_ONLY_GIT_SUBCOMMANDS) | {"config"}               # `config --get` is the usability probe
    forbidden = {"reset", "clean", "checkout", "commit", "push", "add", "stash", "rm", "mv", "merge", "fetch", "pull",
                 "restore", "switch", "rebase", "apply", "update-index", "gc"}
    assert not forbidden & set(seen)
    assert compute_fingerprint(root).value == before                                         # nothing was touched


async def test_the_path_filter_selects_and_unsafe_filters_are_rejected(db, tmp_path):
    h, project, root, task = await completed(db, tmp_path, lambda r: (r / "tracked.txt").write_text("changed\n"))
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        assert set(by_path(await get_changes(client, task.id, path="tests"))) == {"tests/test_calc.py"}
        assert set(by_path(await get_changes(client, task.id, path="tests/"))) == {"tests/test_calc.py"}
        assert set(by_path(await get_changes(client, task.id, path="tracked.txt"))) == {"tracked.txt"}
        assert (await get_changes(client, task.id, path="tra"))["total_changes"] == 0           # a prefix means a directory
        for bad in ("../x", "/etc/passwd", "a/../b", "C:/x", "a\\b", "a//b", "./a", "a\x00b"):
            assert_envelope(await client.get(f"/tasks/{task.id}/changes", params={"path": bad}), 422, "VALIDATION_FAILED")


async def test_changes_are_paged_in_a_stable_order(db, tmp_path):
    def edits(root):
        for i in range(5):
            (root / f"f{i}.txt").write_text(f"{i}\n")
    h, project, root, task = await completed(db, tmp_path, edits)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        everything = [c["path"] for c in (await get_changes(client, task.id))["changes"]]
        seen, offset = [], 0
        while offset is not None:
            page = await get_changes(client, task.id, limit=3, offset=offset)
            assert page["total_changes"] == len(everything) and page["returned"] == len(page["changes"])
            seen += [c["path"] for c in page["changes"]]
            offset = page["next_offset"]
        assert seen == everything == sorted(everything)
        assert (await client.get(f"/tasks/{task.id}/changes", params={"limit": 101})).status_code == 422


async def test_the_real_response_body_respects_its_size_limit_by_dropping_bodies_first(db, tmp_path, monkeypatch):
    h, project, root, task = await completed(db, tmp_path, lambda r: (r / "notes.txt").write_text("line\n" * 400))
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        full = await client.get(f"/tasks/{task.id}/changes")
        cap = len(full.content) - 600
        monkeypatch.setattr(P, "CHANGES_RESPONSE_MAX_BYTES", cap)
        capped = await client.get(f"/tasks/{task.id}/changes")
    body = capped.json()
    assert len(capped.content) <= cap and body["truncated"] is True and "SIZE_LIMIT" in body["sanitization"]["reasons"]
    assert any(c["diff_status"] == "TRUNCATED" and c["diff_reason"] == "SIZE_LIMIT" for c in body["changes"])


# --- the top-level `sanitization` reports what the sanitizer really did --------------------------------------------------------------------
SECRET = "s3cretT0ken"
LEAKY = f"push to https://user:{SECRET}@host/x.git\n"
NO_SANITIZATION = {"truncated": False, "reasons": [], "omitted_items": 0, "omitted_sections": []}


def _cut_file(root: Path, name: str) -> None:
    (root / name).write_bytes(b"a\n" * 50)               # 100 bytes (no CRLF): at the file limit, but its diff text is over twice it


async def _sanitization_of(db, tmp_path, *edits, monkeypatch=None, file_limit=None):
    if file_limit is not None:
        monkeypatch.setattr(P, "CHANGES_FILE_DIFF_MAX_BYTES", file_limit)
    h, project, root, task = await completed(db, tmp_path, *edits)
    client, _ = build(db, h)
    assert await h.start(task.id) == ["COMPLETED"]
    async with client:
        response = await client.get(f"/tasks/{task.id}/changes")
    assert response.status_code == 200, response.text
    return response, response.json()


async def test_nothing_sanitised_gives_the_empty_sanitization(db, tmp_path):
    response, body = await _sanitization_of(db, tmp_path, lambda r: (r / "plain.txt").write_text("nothing secret here\n"))
    assert by_path(body)["plain.txt"]["diff_status"] == "AVAILABLE"
    assert body["sanitization"] == NO_SANITIZATION


async def test_masking_alone_names_its_reason_once_and_is_not_a_truncation(db, tmp_path):
    response, body = await _sanitization_of(db, tmp_path, lambda r: (r / "a_one.txt").write_text(LEAKY),
                                            lambda r: (r / "b_two.txt").write_text(LEAKY))
    assert "***" in by_path(body)["a_one.txt"]["diff"] and "***" in by_path(body)["b_two.txt"]["diff"]
    assert body["sanitization"] == {**NO_SANITIZATION, "reasons": ["SECRET_REDACTED"]}      # two files, one reason, truncated=false
    assert SECRET not in response.text


async def test_a_real_cut_names_its_reason_and_is_a_truncation(db, tmp_path, monkeypatch):
    response, body = await _sanitization_of(db, tmp_path, lambda r: _cut_file(r, "b_truncated_file.txt"),
                                            monkeypatch=monkeypatch, file_limit=100)
    cut = by_path(body)["b_truncated_file.txt"]
    assert cut["diff_status"] == "AVAILABLE" and cut["diff"].endswith(TRUNCATION_MARK)
    assert body["sanitization"] == {**NO_SANITIZATION, "truncated": True, "reasons": ["STRING_TRUNCATED"]}
    assert body["truncated"] is False                                                        # the answer's own flag is another thing


@pytest.mark.parametrize("first, expected", [("mask", ["SECRET_REDACTED", "STRING_TRUNCATED"]),
                                             ("cut", ["STRING_TRUNCATED", "SECRET_REDACTED"])])
async def test_masking_and_cutting_together_list_both_once_in_the_order_first_met(db, tmp_path, monkeypatch, first, expected):
    mask = lambda r, n: (r / n).write_text(LEAKY)                                           # noqa: E731
    names = {"mask": ["a_mask.txt", "c_mask.txt"], "cut": ["b_mask.txt", "d_mask.txt"]}[first]
    cut_name = "b_truncated_file.txt" if first == "mask" else "a_truncated_file.txt"
    response, body = await _sanitization_of(
        db, tmp_path, lambda r: mask(r, names[0]), lambda r: _cut_file(r, cut_name), lambda r: mask(r, names[1]),
        monkeypatch=monkeypatch, file_limit=100)
    assert body["sanitization"] == {**NO_SANITIZATION, "truncated": True, "reasons": expected}
    assert SECRET not in response.text


async def test_a_withheld_body_adds_no_reason_and_is_not_read_for_one(db, tmp_path):
    response, body = await _sanitization_of(db, tmp_path, lambda r: (r / ".env").write_text(f"TOKEN=ghp_abcdefghijklmnop1234 {SECRET}\n"))
    assert (by_path(body)[".env"]["diff_status"], by_path(body)[".env"]["diff_reason"]) == ("WITHHELD", "SENSITIVE_PATH")
    assert body["sanitization"] == NO_SANITIZATION
    assert SECRET not in response.text and "ghp_abcdefghijklmnop1234" not in response.text
