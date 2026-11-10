"""A task's cumulative changes: the workspace measured NOW against the manifest taken when the task took the write lock.

Honest about what this is:
* it compares a LIVE workspace; it is not a point-in-time snapshot, and a finished task's answer is not a settled diff once
  another task has changed the workspace. Two separate consistency fields say what could be checked: `lock_consistency`
  (owner / write epoch / workspace state in the database, before and after) and `workspace_consistency` (the files, measured
  twice; only with `verify_workspace=true`, otherwise UNVERIFIED, never claimed STABLE without two real measurements);
* only hashes were stored, so a text diff exists where git can supply the old content (GIT mode, a COMMIT head, a file that was
  clean at the start); everything else is reported as UNAVAILABLE / WITHHELD / TRUNCATED with a reason;
* read-only: only `rev-parse`, `ls-files`, `status` and `cat-file` are ever run; nothing is written, reset, cleaned, checked out,
  committed or pushed;
* bounded: one deadline reaches the measuring code itself (git is killed by its timeout, the walk and hashing check it between
  entries and chunks), at most one measurement per project and a few overall; a slot is returned when the worker thread REALLY
  ends. The deadline is cooperative for the filesystem: a stalled read cannot be interrupted, so after a timeout the worker may
  linger and `verify_workspace` is refused for a cooldown instead of piling up workers.
"""
import asyncio
import difflib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from app.db import Database
from app.infra import changes_manifest as CM
from app.infra.deadline import Deadline, DeadlineExceeded
from app.infra.response_sanitizer import Sanitizer
from app.infra.workspace_fingerprint import GitInventoryError, git_read_only
from app.infra.workspace_guard import resolve_project_root
from app.models.states import TERMINAL_TASK_STATES
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.baseline_repository import TaskBaselineRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.validation import policies as P
from app.validation.exceptions import FailClosed, MeasurementBusy, NotFound, ValidationFailed
from app.validation.validators import validate_changes_path_filter

log = logging.getLogger("orchestrator.changes")

READ_ONLY_GIT_SUBCOMMANDS = ("rev-parse", "ls-files", "status", "cat-file")     # the ONLY git commands this feature runs
LFS_MARKER = b"version https://git-lfs.github.com/spec/v1"
NOTE = ("This compares the LIVE workspace with the baseline taken when the task took the project write lock. It is not a "
        "point-in-time snapshot.")
SUPPORTED_SCOPE = {"GIT": "GIT_TRACKED_AND_UNTRACKED_NON_IGNORED", "CONTENT": "CONTENT_WALK_POLICY_FILTERED"}
LIMITATIONS_ALL = ("Only paths and content hashes were stored; no file content. A text diff exists only where git can supply "
                   "the old content.",
                   "Changes made before the task took the write lock are not the task's: files that were already dirty are "
                   "flagged pre_existing_dirty and get no diff body.")
LIMITATIONS = {
    "GIT": ("Ignored files (for example build output) are not covered.",
            "A submodule is described by its commit id only: a change inside it is not a changed path and has no body "
            "(SUBMODULE_CONTENT_NOT_STORED); the project fingerprint may still differ.",
            "A mode change that is only staged (git update-index --chmod) is not detected: the manifest describes the "
            "working tree."),
    "CONTENT": ("This is not a git workspace (or git was unusable): paths and hashes only, no diff bodies.",),
}


class MeasurementGate:
    """Limits concurrent measurement workers per project and overall. A slot is released when the WORKER ends (a
    done-callback), never when a waiter times out, so abandoned workers keep counting. After a timeout the project is in a
    cooldown during which `verify_workspace` is refused."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._per_project: dict[uuid.UUID, int] = {}
        self._total = 0
        self._cooldown_until: dict[uuid.UUID, float] = {}

    def try_acquire(self, project_id: uuid.UUID) -> bool:
        if (self._per_project.get(project_id, 0) >= P.CHANGES_MAX_CONCURRENT_PER_PROJECT
                or self._total >= P.CHANGES_MAX_CONCURRENT_GLOBAL):
            return False
        self._per_project[project_id] = self._per_project.get(project_id, 0) + 1
        self._total += 1
        return True

    def release(self, project_id: uuid.UUID) -> None:
        self._per_project[project_id] -= 1
        if not self._per_project[project_id]:
            del self._per_project[project_id]
        self._total -= 1

    def trip(self, project_id: uuid.UUID) -> None:
        self._cooldown_until[project_id] = self._clock() + P.CHANGES_VERIFY_COOLDOWN_SECONDS

    def cooling_down(self, project_id: uuid.UUID) -> bool:
        return self._cooldown_until.get(project_id, 0.0) > self._clock()


@dataclass(frozen=True)
class _Ctx:
    task_id: uuid.UUID
    task_status: str
    project_id: uuid.UUID | None
    workspace_path: str | None
    baseline: object | None
    known_ids: tuple[str, ...]


def _matches(path: str, prefix: str | None) -> bool:
    return prefix is None or path == prefix or path.startswith(prefix + "/")


class ChangeService:
    def __init__(self, db: Database, *, gate: MeasurementGate | None = None, clock: Callable[[], float] = time.monotonic):
        self.db, self.gate, self._clock = db, gate or MeasurementGate(clock), clock

    # --- loading ---------------------------------------------------------------------------------------------
    async def _load(self, task_id: uuid.UUID) -> _Ctx:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).get(task_id)
            if task is None:
                raise NotFound("task not found")
            project = await ProjectRepository(s).get(task.project_id) if task.project_id else None
            baseline = await TaskBaselineRepository(s).get(task_id) if project else None
            known = await ArtifactRepository(s).known_session_ids(task_id)
        return _Ctx(task_id, task.status, project.id if project else None, project.workspace_path if project else None,
                    baseline, known)

    async def _lock_view(self, project_id: uuid.UUID) -> tuple:
        async with self.db.transaction() as s:
            project = await ProjectRepository(s).get(project_id)
        return (project.write_owner_task_id, project.write_epoch, project.workspace_state) if project else (None, None, None)

    # --- the answer --------------------------------------------------------------------------------------------
    async def changes(self, task_id: uuid.UUID, *, limit: int, offset: int, path: str | None = None,
                      verify_workspace: bool = False) -> dict:
        prefix = validate_changes_path_filter(path)
        ctx = await self._load(task_id)
        if ctx.project_id is None:
            return self._unavailable(ctx, "NO_PROJECT", limit, offset)
        baseline = ctx.baseline
        if baseline is None:
            return self._unavailable(ctx, "NO_BASELINE", limit, offset)
        if baseline.status != "AVAILABLE":
            return self._unavailable(ctx, baseline.unavailable_reason or "UNAVAILABLE", limit, offset, baseline=baseline)
        try:
            root = resolve_project_root(ctx.workspace_path)
        except FailClosed:
            return self._unavailable(ctx, "WORKSPACE_UNAVAILABLE", limit, offset, baseline=baseline)

        verify, downgrade = verify_workspace, None
        if verify and self.gate.cooling_down(ctx.project_id):
            verify, downgrade = False, "COOLDOWN"            # a recent timeout: no second measurement, say why
        outcome, lock_stable, view = None, True, None
        for attempt in (1, 2):
            before = await self._lock_view(ctx.project_id)
            outcome = await self._measure(ctx, root, prefix, offset, limit, verify, before[0] not in (None, ctx.task_id))
            after = await self._lock_view(ctx.project_id)
            view, lock_stable = before, before == after
            if lock_stable or outcome.get("failure"):
                break                                         # stable, or nothing worth re-running
        return self._payload(ctx, baseline, outcome, view, lock_stable, limit, offset, downgrade, verify_workspace)

    # --- running the measurement ---------------------------------------------------------------------------------
    async def _measure(self, ctx: _Ctx, root: Path, prefix: str | None, offset: int, limit: int, verify: bool,
                       other_owns: bool) -> dict:
        if not self.gate.try_acquire(ctx.project_id):
            raise MeasurementBusy("a workspace measurement of this project is still running (or too many are); retry later")
        deadline = Deadline(P.CHANGES_DEADLINE_SECONDS, self._clock)
        loop = asyncio.get_running_loop()
        job = partial(self._compute, ctx, root, deadline, prefix, offset, limit, verify, not other_owns)
        fut = loop.run_in_executor(None, job)
        fut.add_done_callback(partial(self._finished, ctx.project_id))        # the slot returns when the THREAD ends
        done, _ = await asyncio.wait({fut}, timeout=max(0.0, deadline.remaining()) + P.CHANGES_DEADLINE_GRACE_SECONDS)
        if fut not in done:
            self.gate.trip(ctx.project_id)                                      # the worker may linger: cooldown + busy
            return {"failure": "DEADLINE", "truncated": True}
        return fut.result()

    def _finished(self, project_id: uuid.UUID, fut: asyncio.Future) -> None:
        if not fut.cancelled():
            fut.exception()                                                     # consumed here; the waiter sees it via result()
        self.gate.release(project_id)

    def _compute(self, ctx: _Ctx, root: Path, deadline: Deadline, prefix: str | None, offset: int, limit: int, verify: bool,
                 allow_bodies: bool) -> dict:
        """Runs in a worker thread. Never raises for an expected failure: it returns {"failure": reason}."""
        b = ctx.baseline
        try:
            base = CM.decode(b.manifest, version=b.manifest_version, expected_mode=b.mode,
                             expected_sha256=b.manifest_sha256, expected_bytes=b.uncompressed_bytes)
            now = CM.measure(root, deadline=deadline, mode=b.mode)
        except CM.ManifestFailure as exc:
            return {"failure": exc.reason}
        except DeadlineExceeded:
            self.gate.trip(ctx.project_id)
            return {"failure": "DEADLINE", "truncated": True}
        dirty = frozenset(b.dirty_paths or [])
        found = [c for c in CM.compute_changes(base, now.entries, dirty) if _matches(c.path, prefix)]
        page = found[offset:offset + limit]
        sanitizer = Sanitizer(ctx.known_ids)
        out: dict = {"total": len(found), "items": [], "truncated": False, "workspace": "UNVERIFIED", "workspace_reason": None}
        budget = {"files": 0, "bytes": 0}
        expired = False
        for change in page:
            if expired:
                out["items"].append(self._item(change, {"diff_status": "TRUNCATED", "diff_reason": "DEADLINE"}))
                out["truncated"] = True
                continue
            try:
                out["items"].append(self._item(change, self._body(change, ctx, root, deadline, budget, sanitizer, allow_bodies, dirty)))
            except DeadlineExceeded:
                expired = True
                out["truncated"] = True
                out["items"].append(self._item(change, {"diff_status": "TRUNCATED", "diff_reason": "DEADLINE"}))
        if verify:
            try:
                second = CM.measure(root, deadline=deadline, mode=b.mode)
                out["workspace"] = "STABLE" if CM.entries_digest(second) == CM.entries_digest(now) else "UNSTABLE"
            except DeadlineExceeded:
                out["workspace_reason"] = "DEADLINE"            # the re-measure ran out of time: never claimed STABLE
            except CM.ManifestFailure as exc:
                out["workspace_reason"] = exc.reason
        else:
            out["workspace_reason"] = "NOT_REQUESTED"
        return out

    # --- one change: the body ---------------------------------------------------------------------------------------------
    @staticmethod
    def _item(change: CM.Change, body: dict) -> dict:
        return {"path": change.path, "change_type": change.change_type, "kind": change.kind,
                "previous_path": change.previous_path, "binary": body.get("binary", False),
                "pre_existing_dirty": change.pre_existing_dirty, "diff_status": body["diff_status"],
                "diff_reason": body.get("diff_reason"), "diff": body.get("diff"), "base_target": body.get("base_target"),
                "target": body.get("target")}

    def _body(self, c: CM.Change, ctx: _Ctx, root: Path, deadline: Deadline, budget: dict, sanitizer: Sanitizer,
              allow: bool, dirty: frozenset) -> dict:
        b = ctx.baseline
        out: dict = {"diff_status": "UNAVAILABLE", "diff_reason": None}
        base_e, now_e = c.base, c.now
        if (base_e and base_e["kind"] == "SYMLINK") or (now_e and now_e["kind"] == "SYMLINK"):
            out["base_target"] = sanitizer.text(base_e["target"], max_chars=512)[0] if base_e and base_e["kind"] == "SYMLINK" else None
            out["target"] = sanitizer.text(now_e["target"], max_chars=512)[0] if now_e and now_e["kind"] == "SYMLINK" else None
        if not allow:
            return {**out, "diff_status": "WITHHELD", "diff_reason": "PROJECT_OWNED_BY_OTHER_TASK"}
        if CM.is_sensitive_path(c.path) or (c.previous_path and CM.is_sensitive_path(c.previous_path)):
            return {**out, "diff_status": "WITHHELD", "diff_reason": "SENSITIVE_PATH"}
        kinds = {e["kind"] for e in (base_e, now_e) if e}
        if "GITLINK" in kinds:
            return {**out, "diff_reason": "SUBMODULE_CONTENT_NOT_STORED"}
        if "SYMLINK" in kinds:
            return {**out, "diff_reason": "SYMLINK_TARGET_ONLY"}
        if b.mode != "GIT":
            return {**out, "diff_reason": "NO_BASELINE_CONTENT_WITHOUT_GIT"}
        needs_base = c.change_type in ("MODIFIED", "REMOVED", "RENAMED")
        base_path = c.previous_path or c.path
        if needs_base:
            if b.head_kind != "COMMIT":
                return {**out, "diff_reason": "UNBORN_HEAD"}
            if c.pre_existing_dirty or base_path in dirty or c.path in dirty:
                return {**out, "diff_reason": "BASELINE_CONTENT_NOT_STORED"}
        if budget["files"] >= P.CHANGES_MAX_DIFF_FILES or budget["bytes"] >= P.CHANGES_TOTAL_DIFF_MAX_BYTES:
            return {**out, "diff_status": "TRUNCATED", "diff_reason": "BODY_BUDGET"}
        try:
            old = self._read_base(root, b.head_sha, base_path, deadline) if needs_base else b""
            new = self._read_now(root, c.path, now_e) if now_e else b""
        except _Skip as skip:
            return {**out, "diff_status": skip.status, "diff_reason": skip.reason}
        except GitInventoryError:
            return {**out, "diff_reason": "GIT_READ_FAILED"}
        if b"\x00" in old[:8192] or b"\x00" in new[:8192]:
            return {**out, "binary": True, "diff_reason": "BINARY"}
        if old.startswith(LFS_MARKER) or new.startswith(LFS_MARKER):
            return {**out, "diff_reason": "LFS_POINTER"}
        text = "\n".join(difflib.unified_diff(
            old.decode("utf-8", "replace").splitlines(), new.decode("utf-8", "replace").splitlines(),
            fromfile=f"a/{base_path}", tofile=f"b/{c.path}", lineterm=""))
        clean, _ = sanitizer.text(text, max_chars=2 * P.CHANGES_FILE_DIFF_MAX_BYTES)
        budget["files"] += 1
        budget["bytes"] += len(clean.encode("utf-8", "replace"))
        return {**out, "diff_status": "AVAILABLE", "diff": clean}

    @staticmethod
    def _read_base(root: Path, head_sha: str, rel: str, deadline: Deadline) -> bytes:
        spec = f"{head_sha}:{rel}"                       # starts with the commit id, so it can never be read as an option
        size = git_read_only(root, ["cat-file", "-s", spec], deadline=deadline, ok_codes=(0, 128)).strip()
        if not size:
            raise _Skip("UNAVAILABLE", "NOT_IN_HEAD")
        if int(size) > P.CHANGES_FILE_DIFF_MAX_BYTES:
            raise _Skip("TRUNCATED", "FILE_TOO_LARGE")
        return git_read_only(root, ["cat-file", "blob", spec], deadline=deadline)

    @staticmethod
    def _read_now(root: Path, rel: str, entry: dict) -> bytes:
        full = root / rel
        try:
            if full.is_symlink() or not full.is_file():
                raise _Skip("UNAVAILABLE", "CHANGED_DURING_READ")
            if full.stat().st_size > P.CHANGES_FILE_DIFF_MAX_BYTES:
                raise _Skip("TRUNCATED", "FILE_TOO_LARGE")
            return full.read_bytes()
        except OSError as exc:
            raise _Skip("UNAVAILABLE", "READ_FAILED") from exc

    # --- payloads ---------------------------------------------------------------------------------------------------------
    @staticmethod
    def _empty_sanitization() -> dict:
        return {"truncated": False, "reasons": [], "omitted_items": 0, "omitted_sections": []}

    def _relation(self, ctx: _Ctx, view: tuple) -> str:
        owner = view[0]
        if owner == ctx.task_id:
            return "CURRENT_OWNER"
        if owner is not None:
            return "OTHER_TASK_OWNS"
        return "UNOWNED_AFTER_TASK" if ctx.task_status in {str(s) for s in TERMINAL_TASK_STATES} else "UNVERIFIED"

    def _unavailable(self, ctx: _Ctx, reason: str, limit: int, offset: int, *, baseline=None) -> dict:
        return {"task_id": ctx.task_id, "state": "UNAVAILABLE", "unavailable_reason": reason, "precision": "NONE",
                "note": NOTE, "diff_basis": None, "workspace_relation": "UNVERIFIED", "lock_consistency": "STABLE",
                "workspace_consistency": "UNVERIFIED", "workspace_consistency_reason": "NOT_MEASURED",
                "supported_scope": None, "limitations": list(LIMITATIONS_ALL), "changes": [], "total_changes": 0,
                "returned": 0, "has_more": False, "next_offset": None, "limit": limit, "offset": offset,
                "truncated": False, "sanitization": self._empty_sanitization()}

    def _payload(self, ctx: _Ctx, b, outcome: dict, view: tuple, lock_stable: bool, limit: int, offset: int,
                 downgrade: str | None, verify_requested: bool) -> dict:
        relation = self._relation(ctx, view)
        if outcome.get("failure"):
            base = self._unavailable(ctx, outcome["failure"], limit, offset, baseline=b)
            base.update(truncated=bool(outcome.get("truncated")), workspace_relation=relation,
                        lock_consistency="STABLE" if lock_stable else "UNSTABLE")
            return base
        items, total = outcome["items"], outcome["total"]
        end = offset + len(items)
        reason = outcome["workspace_reason"] if outcome["workspace"] == "UNVERIFIED" else None
        if downgrade and verify_requested:
            reason = downgrade                               # the circuit breaker, not the caller, chose not to verify
        return {
            "task_id": ctx.task_id, "state": "AVAILABLE", "unavailable_reason": None, "precision": "HASH_COMPARISON",
            "note": NOTE,
            "diff_basis": {"origin": b.origin, "mode": b.mode, "head_kind": b.head_kind, "head_sha": b.head_sha,
                           "proof_epoch": b.proof_epoch, "acquired_epoch": b.acquired_epoch,
                           "recorded_epoch": b.recorded_epoch, "manifest_version": b.manifest_version,
                           "file_count": b.file_count},
            "workspace_relation": relation, "lock_consistency": "STABLE" if lock_stable else "UNSTABLE",
            "workspace_consistency": outcome["workspace"], "workspace_consistency_reason": reason,
            "supported_scope": SUPPORTED_SCOPE[b.mode], "limitations": [*LIMITATIONS_ALL, *LIMITATIONS[b.mode]],
            "changes": items, "total_changes": total, "returned": len(items), "has_more": end < total,
            "next_offset": end if end < total else None, "limit": limit, "offset": offset,
            "truncated": bool(outcome["truncated"]), "sanitization": self._empty_sanitization()}


class _Skip(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason
