"""Task baselines recorded at lock acquisition: the two measurement failures kept apart, atomicity with `set_owner`, epochs,
re-entry recovery, conflicting rows and the database CHECKs. MockProviders only: no real CLI."""
import asyncio
import dataclasses
import subprocess
import types
import uuid

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.infra import changes_manifest as CM
from app.infra.deadline import DeadlineExceeded
from app.infra.workspace_fingerprint import FingerprintError
from app.models.tables import Project, TaskBaseline
from app.repositories.baseline_repository import BaselineConflict, BaselineWriteResult, TaskBaselineRepository
from app.repositories.project_repository import ProjectRepository
from app.services import project_lock_service as PLS
from app.services.project_lock_service import (MISSING_AT_REENTRY, ProjectLockService, SupplementalFailure, SupplementalOk,
                                               baseline_values)
from app.services.project_service import ProjectService
from tests.harness import GOOD_CODE, Harness, structured, write_tests_ok
from tests.integration_pg.test_project_lock import (claim_and_process, fp, make_owner, make_project, new_task, project_row,
                                                    TESTER_GATE)

FP = "WORKSPACE_FINGERPRINT_V1:" + "a" * 64


def git(root, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *args], cwd=root,
                   check=True, capture_output=True)


async def baseline_of(h, task_id) -> TaskBaseline | None:
    async with h.db.transaction() as s:
        return (await s.execute(select(TaskBaseline).where(TaskBaseline.task_id == task_id))).scalar_one_or_none()


def entries_of(row: TaskBaseline) -> dict:
    return CM.decode(row.manifest, version=row.manifest_version, expected_mode=row.mode,
                     expected_sha256=row.manifest_sha256, expected_bytes=row.uncompressed_bytes)


def payload_is_empty(row: TaskBaseline) -> bool:
    return all(getattr(row, c) is None for c in ("mode", "head_kind", "head_sha", "manifest", "manifest_version",
                                                  "uncompressed_bytes", "manifest_sha256", "file_count", "dirty_paths"))


async def git_project(h, tmp_path, name="gitsvc"):
    root = tmp_path / "projects" / name
    root.mkdir(parents=True)
    git(root, "init", "-q")
    (root / "tracked.txt").write_text("committed\n")
    (root / "edited.txt").write_text("committed too\n")
    (root / ".gitignore").write_text("build/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    (root / "edited.txt").write_text("edited before the task\n")          # already dirty when the task starts
    (root / "untracked.txt").write_text("not committed\n")
    (root / "build").mkdir()
    (root / "build" / "out.o").write_text("ignored build output")
    service = ProjectService(h.db, None, clone_root=tmp_path / "clones", protected=(h.guard.root,))
    return await service.create_project(name=name, workspace_path=str(root)), root


# --- acquisition ------------------------------------------------------------------------------------------------------------
async def test_taking_the_lock_records_an_available_baseline_with_the_three_epochs(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    before = fp(root)
    task = await new_task(h, project)
    assert await h.start(task.id) == ["COMPLETED"]
    row = await baseline_of(h, task.id)
    assert (row.status, row.origin, row.mode, row.head_kind, row.head_sha) == ("AVAILABLE", "ACQUISITION", "CONTENT", None, None)
    assert (row.proof_epoch, row.acquired_epoch, row.recorded_epoch) == (0, 1, 1)         # E0, E0 + 1, written at E0 + 1
    assert row.proof_fingerprint == before and row.project_id == project.id and row.unavailable_reason is None
    assert set(entries_of(row)) == {"user.txt"} and row.dirty_paths == [] and row.file_count == 1
    assert row.manifest_version == "CHANGES_MANIFEST_V1" and len(row.manifest_sha256) == 64


async def test_the_baseline_describes_the_start_state_including_what_was_already_dirty(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await git_project(h, tmp_path)
    task = await new_task(h, project)
    assert await h.start(task.id) == ["COMPLETED"]
    row = await baseline_of(h, task.id)
    assert (row.status, row.mode, row.head_kind) == ("AVAILABLE", "GIT", "COMMIT") and len(row.head_sha) in (40, 64)
    entries = entries_of(row)
    assert set(entries) == {".gitignore", "edited.txt", "tracked.txt", "untracked.txt"}      # ignored build/ is not covered
    assert "calc.py" not in entries                                                          # created later, by the task
    assert sorted(row.dirty_paths) == ["edited.txt", "untracked.txt"]                        # the task's changes start AFTER these


async def test_a_repository_without_a_first_commit_is_a_valid_baseline(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    root = tmp_path / "projects" / "fresh"
    root.mkdir(parents=True)
    git(root, "init", "-q")
    (root / "x.txt").write_text("x\n")
    service = ProjectService(h.db, None, clone_root=tmp_path / "clones", protected=(h.guard.root,))
    project = await service.create_project(name="fresh", workspace_path=str(root))
    task = await new_task(h, project)
    assert await h.start(task.id) == ["COMPLETED"]
    row = await baseline_of(h, task.id)
    assert (row.status, row.mode, row.head_kind, row.head_sha) == ("AVAILABLE", "GIT", "UNBORN", None)


async def test_a_plain_task_without_a_project_has_no_baseline(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    task = await h.new_task()
    assert await h.start(task.id) == ["COMPLETED"]
    assert await baseline_of(h, task.id) is None


# --- the two measurement failures, kept apart ------------------------------------------------------------------------------
async def test_a_fingerprint_failure_keeps_the_fail_closed_path_no_owner_no_baseline_a_dirty_gate(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)

    def broken(_root):
        raise FingerprintError("cannot read the workspace")

    monkeypatch.setattr(PLS, "compute_fingerprint", broken)
    assert await claim_and_process(h, task.id) == "WAITING_APPROVAL"
    row = await project_row(h, project.id)
    assert row.workspace_state == "DIRTY_UNRESOLVED" and row.write_owner_task_id is None
    assert await baseline_of(h, task.id) is None                                     # nothing was recorded, nothing ran
    gates = [a for a in await h.approvals(task.id) if (a.target or {}).get("reason_code") == "PROJECT_DIRTY"]
    assert len(gates) == 1 and gates[0].status == "PENDING"
    assert "DEVELOPER" not in h.roles_called(h.claude)


@pytest.mark.parametrize("failure,reason", [
    (CM.ManifestFailure("TOO_MANY_FILES"), "TOO_MANY_FILES"), (CM.ManifestFailure("DIRTY_PATHS_LIMIT"), "DIRTY_PATHS_LIMIT"),
    (CM.ManifestFailure("GIT_ENUMERATION_FAILED"), "GIT_ENUMERATION_FAILED"), (DeadlineExceeded("late"), "DEADLINE"),
    (OSError("disk"), "FILESYSTEM_ERROR"), (RuntimeError("bug"), "UNEXPECTED_ERROR")])
async def test_a_manifest_failure_with_a_valid_fingerprint_continues_with_an_unavailable_row_in_the_same_transaction(
        db, tmp_path, monkeypatch, failure, reason):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    before = fp(root)
    task = await new_task(h, project)

    def failing(*_a, **_k):
        raise failure

    monkeypatch.setattr(CM, "measure", failing)
    assert await h.start(task.id) == ["COMPLETED"]                                   # the task is NOT stopped
    row = await baseline_of(h, task.id)
    assert (row.status, row.origin, row.unavailable_reason) == ("UNAVAILABLE", "ACQUISITION", reason)
    assert payload_is_empty(row)                                                      # partial data is never authoritative
    assert (row.proof_epoch, row.acquired_epoch, row.recorded_epoch) == (0, 1, 1)     # the valid proof and its epochs
    assert row.proof_fingerprint == before


async def test_an_encoding_failure_is_also_only_an_unavailable_row(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    task = await new_task(h, project)

    def too_large(_measured):
        raise CM.ManifestFailure("MANIFEST_TOO_LARGE")

    monkeypatch.setattr(CM, "encode", too_large)
    assert await h.start(task.id) == ["COMPLETED"]
    assert (await baseline_of(h, task.id)).unavailable_reason == "MANIFEST_TOO_LARGE"


async def test_a_workspace_that_moves_while_the_manifest_is_measured_is_re_measured(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    real = PLS.compute_fingerprint(root)
    other = dataclasses.replace(real, value="WORKSPACE_FINGERPRINT_V1:" + "0" * 64)
    calls = []

    def flapping(_root):
        calls.append(1)
        return other if len(calls) == 2 else real             # before = real, AFTER = different, then stable

    monkeypatch.setattr(PLS, "compute_fingerprint", flapping)
    task = await new_task(h, project)
    assert await h.start(task.id) == ["COMPLETED"]
    row = await baseline_of(h, task.id)
    assert row.proof_fingerprint == real.value and len(calls) >= 4                    # the first cycle was discarded


async def test_when_the_workspace_never_holds_still_nothing_is_recorded_and_nothing_is_owned(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    real = PLS.compute_fingerprint(root)
    counter = {"n": 0}

    def never_stable(_root):
        counter["n"] += 1
        return dataclasses.replace(real, value="WORKSPACE_FINGERPRINT_V1:" + f"{counter['n']:064x}")

    monkeypatch.setattr(PLS, "compute_fingerprint", never_stable)
    task = await new_task(h, project)
    await claim_and_process(h, task.id)
    row = await project_row(h, project.id)
    assert row.write_owner_task_id is None and row.write_epoch == 0 and row.workspace_state == "CLEAN"
    assert await baseline_of(h, task.id) is None and "DEVELOPER" not in h.roles_called(h.claude)


# --- atomicity and conflicts ------------------------------------------------------------------------------------------------
async def test_owner_and_baseline_commit_together_a_failing_insert_rolls_the_owner_back(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    task = await new_task(h, project)

    async def boom(self, values):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(TaskBaselineRepository, "insert_once", boom)
    try:
        await claim_and_process(h, task.id)
    except Exception:  # noqa: BLE001 - how the runner reports it is not what is under test
        pass
    row = await project_row(h, project.id)
    assert row.write_owner_task_id is None and row.write_epoch == 0 and row.write_baseline_fingerprint is None
    assert await baseline_of(h, task.id) is None


async def test_a_baseline_of_a_different_acquisition_fails_closed_and_rolls_the_owner_back(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    async with h.db.transaction() as s:                                               # a row of some OTHER acquisition
        await TaskBaselineRepository(s).insert_once(baseline_values(
            task.id, project.id, origin="ACQUISITION", proof_epoch=5, acquired_epoch=6, recorded_epoch=6, fingerprint=FP,
            supplemental=SupplementalFailure("OLD")))
    try:
        await claim_and_process(h, task.id)
    except Exception:  # noqa: BLE001
        pass
    row = await project_row(h, project.id)
    assert row.write_owner_task_id is None and row.write_epoch == 0                    # set_owner rolled back with it
    kept = await baseline_of(h, task.id)
    assert (kept.proof_epoch, kept.acquired_epoch, kept.unavailable_reason) == (5, 6, "OLD")     # never overwritten
    assert "DEVELOPER" not in h.roles_called(h.claude)


async def test_two_acquisitions_at_the_same_epoch_exactly_one_wins(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    a, b = await new_task(h, project), await new_task(h, project, "implement sub(a, b)")

    async def take(task):
        async with h.db.transaction() as s:
            repo = ProjectRepository(s)
            await repo.lock(project.id)
            return await repo.set_owner(project.id, task.id, FP, expected_epoch=0)

    results = await asyncio.gather(take(a), take(b))
    assert sorted(r for r in results if r is not None) == [1] and results.count(None) == 1
    row = await project_row(h, project.id)
    assert row.write_epoch == 1 and row.write_owner_task_id in (a.id, b.id)


async def test_set_owner_with_a_stale_epoch_changes_nothing(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    async with h.db.transaction() as s:
        assert await ProjectRepository(s).set_owner(project.id, task.id, FP, expected_epoch=3) is None
    row = await project_row(h, project.id)
    assert row.write_owner_task_id is None and row.write_epoch == 0


def _row(**kw):
    return types.SimpleNamespace(**{"task_id": uuid.uuid4(), "project_id": None, "acquired_epoch": 1, "recorded_epoch": 1, **kw})


def test_the_epoch_check_is_exact_before_any_mutation_and_monotonic_after_it():
    project = types.SimpleNamespace(id=uuid.uuid4(), write_epoch=4)
    check = ProjectLockService._check_epoch
    check(_row(project_id=project.id, acquired_epoch=4), project, mutating=False)
    check(_row(project_id=project.id, acquired_epoch=4), project, mutating=True)
    check(_row(project_id=project.id, acquired_epoch=2), project, mutating=True)           # mutating steps bumped it
    for row, mutating in ((_row(project_id=project.id, acquired_epoch=3), False),          # grew with no mutation: wrong
                          (_row(project_id=project.id, acquired_epoch=5), True),            # below the acquired epoch
                          (_row(project_id=uuid.uuid4(), acquired_epoch=4), False)):        # another project's row
        with pytest.raises(PLS.FailClosed):
            check(row, project, mutating)
    check(_row(project_id=project.id, acquired_epoch=None, recorded_epoch=4), project, True)    # recovery row: lower bound only
    with pytest.raises(PLS.FailClosed):
        check(_row(project_id=project.id, acquired_epoch=None, recorded_epoch=5), project, True)


# --- identity of a conflicting row -------------------------------------------------------------------------------------------
def available(task_id, project_id, tmp_path, **kw):
    (tmp_path / "f.txt").write_text("f")
    measured = CM.measure(tmp_path)
    base = dict(origin="ACQUISITION", proof_epoch=2, acquired_epoch=3, recorded_epoch=3, fingerprint=FP,
                supplemental=SupplementalOk(measured, CM.encode(measured)))
    base.update(kw)
    return baseline_values(task_id, project_id, **base)


async def test_insert_once_compares_identity_not_status_and_never_overwrites(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    other_project, _ = await make_project(h, tmp_path, "other")
    task = await new_task(h, project)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    async def write(values):
        async with h.db.transaction() as s:
            return await TaskBaselineRepository(s).insert_once(values)

    first = available(task.id, project.id, scratch)
    assert await write(first) is BaselineWriteResult.INSERTED
    assert await write(first) is BaselineWriteResult.IDEMPOTENT
    unavailable = available(task.id, project.id, scratch, supplemental=SupplementalFailure("LATER"))
    assert await write(unavailable) is BaselineWriteResult.STATUS_CONFLICT_PRESERVED         # AVAILABLE beats a new UNAVAILABLE
    assert (await baseline_of(h, task.id)).status == "AVAILABLE"
    for different in (available(task.id, project.id, scratch, proof_epoch=3, acquired_epoch=4, recorded_epoch=4),
                      available(task.id, project.id, scratch, fingerprint="WORKSPACE_FINGERPRINT_V1:" + "b" * 64),
                      available(task.id, other_project.id, scratch)):
        with pytest.raises(BaselineConflict):
            await write(different)
    assert (await baseline_of(h, task.id)).acquired_epoch == 3                                # still the original


async def test_an_unavailable_row_is_never_promoted_to_available(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    async with h.db.transaction() as s:
        await TaskBaselineRepository(s).insert_once(available(task.id, project.id, scratch, supplemental=SupplementalFailure("X")))
    async with h.db.transaction() as s:
        assert (await TaskBaselineRepository(s).insert_once(available(task.id, project.id, scratch))
                ) is BaselineWriteResult.STATUS_CONFLICT_PRESERVED
    assert (await baseline_of(h, task.id)).status == "UNAVAILABLE"


async def test_rows_without_an_acquisition_epoch_have_their_own_identity(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    def missing(recorded):
        return baseline_values(task.id, project.id, origin="REENTRY_RECOVERY", proof_epoch=None, acquired_epoch=None,
                               recorded_epoch=recorded, fingerprint=FP, supplemental=None, reason=MISSING_AT_REENTRY)

    async def write(values):
        async with h.db.transaction() as s:
            return await TaskBaselineRepository(s).insert_once(values)

    assert await write(missing(7)) is BaselineWriteResult.INSERTED
    assert await write(missing(9)) is BaselineWriteResult.IDEMPOTENT                 # recorded_epoch is not part of the identity
    with pytest.raises(BaselineConflict):                                            # an epoch-bearing row is another identity
        await write(available(task.id, project.id, scratch))
    with pytest.raises(BaselineConflict):                                            # another fingerprint is another identity
        await write(baseline_values(task.id, project.id, origin="REENTRY_RECOVERY", proof_epoch=None, acquired_epoch=None,
                                    recorded_epoch=7, fingerprint="WORKSPACE_FINGERPRINT_V1:" + "c" * 64, supplemental=None,
                                    reason=MISSING_AT_REENTRY))


# --- crash gap / re-entry ------------------------------------------------------------------------------------------------------
async def test_an_owner_without_a_baseline_that_changed_nothing_is_recovered_when_the_workspace_is_provably_unchanged(
        db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    baseline = await make_owner(h, project, task, root)                    # owner set, NO baseline row: the crash gap
    assert await baseline_of(h, task.id) is None
    assert await claim_and_process(h, task.id) == "COMPLETED"
    row = await baseline_of(h, task.id)
    assert (row.status, row.origin, row.mode) == ("AVAILABLE", "REENTRY_RECOVERY", "CONTENT")
    assert (row.proof_epoch, row.acquired_epoch, row.recorded_epoch) == (1, 1, 1)         # the owner epoch, proven by the fingerprint
    assert row.proof_fingerprint == baseline and set(entries_of(row)) == {"user.txt"}


async def test_an_owner_whose_workspace_moved_since_acquisition_gets_an_explicit_unavailable_row(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    baseline = await make_owner(h, project, task, root)
    (root / "changed_by_someone.txt").write_text("not the task's own start state\n")
    assert await claim_and_process(h, task.id) == "COMPLETED"
    row = await baseline_of(h, task.id)
    assert (row.status, row.origin, row.unavailable_reason) == ("UNAVAILABLE", "REENTRY_RECOVERY", MISSING_AT_REENTRY)
    assert (row.proof_epoch, row.acquired_epoch, row.recorded_epoch) == (None, None, 1)   # the epoch is not invented
    assert row.proof_fingerprint == baseline and payload_is_empty(row)


async def test_an_owner_is_not_stuck_behind_an_unmeasurable_workspace_when_its_baseline_is_missing(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    baseline = await make_owner(h, project, task, root)
    async def unmeasured(self, *a, **k):
        return PLS.LockMeasurement(PLS.UNMEASURED, SupplementalFailure("FINGERPRINT_UNMEASURED"))
    monkeypatch.setattr(ProjectLockService, "_measure_acquisition", unmeasured)
    assert await claim_and_process(h, task.id) == "COMPLETED"          # used to be BLOCKED -> ProjectBusy, lock held forever
    row = await baseline_of(h, task.id)
    assert (row.status, row.unavailable_reason, row.proof_fingerprint) == ("UNAVAILABLE", MISSING_AT_REENTRY, baseline)


async def test_after_a_mutation_a_missing_baseline_is_unavailable_and_the_current_epoch_is_not_passed_off_as_acquired(
        db, tmp_path):
    gated = lambda call: TESTER_GATE(call) if call.n == 1 else write_tests_ok(call)  # noqa: E731
    h = Harness(db, tmp_path / "ws", claude_handlers={"TESTER": gated})
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    assert await claim_and_process(h, task.id) == "WAITING_APPROVAL"       # DEVELOP ran (a mutating step): the lock is held
    assert (await baseline_of(h, task.id)).status == "AVAILABLE"
    async with h.db.transaction() as s:                                    # simulate an owner from before this feature
        await s.execute(delete(TaskBaseline).where(TaskBaseline.task_id == task.id))
    held = await project_row(h, project.id)
    approval = next(a for a in await h.approvals(task.id) if a.status == "PENDING")
    await h.decide(approval.id)
    assert await h.run_until_idle() == ["COMPLETED"]
    row = await baseline_of(h, task.id)
    assert (row.status, row.origin, row.unavailable_reason) == ("UNAVAILABLE", "REENTRY_RECOVERY", MISSING_AT_REENTRY)
    assert (row.proof_epoch, row.acquired_epoch) == (None, None) and row.recorded_epoch >= held.write_epoch
    assert row.proof_fingerprint == held.write_baseline_fingerprint


async def test_a_recorded_baseline_is_never_touched_on_re_entry(db, tmp_path):
    gated = lambda call: TESTER_GATE(call) if call.n == 1 else write_tests_ok(call)  # noqa: E731
    h = Harness(db, tmp_path / "ws", claude_handlers={"TESTER": gated})
    project, _root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    assert await claim_and_process(h, task.id) == "WAITING_APPROVAL"
    first = await baseline_of(h, task.id)
    approval = next(a for a in await h.approvals(task.id) if a.status == "PENDING")
    await h.decide(approval.id)
    assert await h.run_until_idle() == ["COMPLETED"]
    again = await baseline_of(h, task.id)
    assert (again.manifest, again.manifest_sha256, again.created_at, again.acquired_epoch) == (
        first.manifest, first.manifest_sha256, first.created_at, first.acquired_epoch)


# --- the database CHECKs -----------------------------------------------------------------------------------------------------
async def _insert_raw(db, values):
    async with db.transaction() as s:
        await s.execute(insert(TaskBaseline).values(**values))


@pytest.fixture
async def shapes(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    task = await new_task(h, project)
    good = available(task.id, project.id, scratch)
    good.update(mode="GIT", head_kind="COMMIT", head_sha="a" * 40)
    return h, project, task, good, scratch


@pytest.mark.parametrize("change", [
    {"unavailable_reason": "X"},                                                          # AVAILABLE + a reason
    {"manifest_sha256": "short"}, {"manifest_sha256": None}, {"manifest": None}, {"manifest_version": None},
    {"uncompressed_bytes": None}, {"uncompressed_bytes": -1}, {"file_count": None}, {"file_count": -1},
    {"dirty_paths": None}, {"mode": None}, {"mode": "SVN"},
    {"head_kind": "COMMIT", "head_sha": None}, {"head_sha": "A" * 40}, {"head_sha": "a" * 39}, {"head_sha": "g" * 40},
    {"head_kind": "UNBORN"},                                                              # UNBORN with a head_sha
    {"head_kind": None}, {"head_kind": "BRANCH"},
    {"mode": "CONTENT"},                                                                  # CONTENT with head_kind/head_sha
    {"proof_epoch": None}, {"acquired_epoch": None}, {"acquired_epoch": 9},               # ACQUISITION: acquired != proof + 1
    {"recorded_epoch": 9}, {"acquired_epoch": 0, "proof_epoch": -1}, {"recorded_epoch": 0},
    {"proof_fingerprint": "nope"}, {"proof_fingerprint": "WORKSPACE_FINGERPRINT_V1:" + "A" * 64},
    {"proof_fingerprint": "STEP_SNAPSHOT_V1:" + "a" * 64}, {"origin": "OTHER"}, {"status": "MAYBE"},
])
async def test_the_database_rejects_a_malformed_available_row(shapes, change):
    h, _p, _t, good, _s = shapes
    with pytest.raises(IntegrityError):
        await _insert_raw(h.db, {**good, **change})


@pytest.mark.parametrize("change", [
    {"unavailable_reason": None}, {"manifest": b"x"}, {"manifest_version": "CHANGES_MANIFEST_V1"}, {"manifest_sha256": "a" * 64},
    {"uncompressed_bytes": 1}, {"file_count": 1}, {"mode": "GIT"}, {"head_kind": "COMMIT"}, {"head_sha": "a" * 40},
    {"dirty_paths": []},                                                                   # any payload next to UNAVAILABLE
])
async def test_the_database_rejects_a_mixed_unavailable_row(shapes, change):
    h, project, task, _good, _s = shapes
    row = baseline_values(task.id, project.id, origin="ACQUISITION", proof_epoch=2, acquired_epoch=3, recorded_epoch=3,
                          fingerprint=FP, supplemental=SupplementalFailure("X"))
    with pytest.raises(IntegrityError):
        await _insert_raw(h.db, {**row, **change})


@pytest.mark.parametrize("change", [
    {"origin": "ACQUISITION"}, {"acquired_epoch": 4}, {"proof_epoch": 1},
])
async def test_the_missing_at_reentry_reason_is_only_valid_with_null_epochs_and_the_recovery_origin(shapes, change):
    h, project, task, _good, _s = shapes
    row = baseline_values(task.id, project.id, origin="REENTRY_RECOVERY", proof_epoch=None, acquired_epoch=None,
                          recorded_epoch=5, fingerprint=FP, supplemental=None, reason=MISSING_AT_REENTRY)
    with pytest.raises(IntegrityError):
        await _insert_raw(h.db, {**row, **change})


async def test_every_legitimate_shape_is_accepted(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    shapes_ = []
    for kind, mode, head_kind, head_sha in (("git", "GIT", "COMMIT", "a" * 64), ("unborn", "GIT", "UNBORN", None),
                                            ("content", "CONTENT", None, None)):
        shapes_.append(dict(mode=mode, head_kind=head_kind, head_sha=head_sha))
    for extra in shapes_:
        task = await new_task(h, project, f"implement feature {extra['mode']} {extra['head_kind']}")
        await _insert_raw(h.db, {**available(task.id, project.id, scratch), **extra})
    for build in (lambda t: baseline_values(t, project.id, origin="ACQUISITION", proof_epoch=0, acquired_epoch=1,
                                            recorded_epoch=1, fingerprint=FP, supplemental=SupplementalFailure("DEADLINE")),
                  lambda t: baseline_values(t, project.id, origin="REENTRY_RECOVERY", proof_epoch=None, acquired_epoch=None,
                                            recorded_epoch=4, fingerprint=FP, supplemental=None, reason=MISSING_AT_REENTRY),
                  lambda t: baseline_values(t, project.id, origin="REENTRY_RECOVERY", proof_epoch=4, acquired_epoch=4,
                                            recorded_epoch=4, fingerprint=FP, supplemental=SupplementalFailure("GIT_ENUMERATION_FAILED"))):
        task = await new_task(h, project, f"implement feature {uuid.uuid4().hex[:6]}")
        await _insert_raw(h.db, build(task.id))
    async with h.db.transaction() as s:
        assert len((await s.execute(select(TaskBaseline))).scalars().all()) == 6
