"""Project registration (existing local workspace or a git clone) and lifecycle.

Clone ordering and compensation: validate -> create a NEW directory under the clone root (owned by this request)
-> `git clone` -> insert the Project row. The Project row only exists after the clone succeeded, so an incomplete
directory is never exposed as a project. Only the directory created by this request is ever removed.
"""
import asyncio
import logging
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from sqlalchemy import null

from app.db import Database
from app.infra.command_executor import CommandExecutor, CommandSpec
from app.infra.process_control import CONFIRMED_GONE
from app.infra.redaction import redact_credentials
from app.infra.workspace_fingerprint import FingerprintError, compute_fingerprint
from app.models.states import CliOutcome
from app.models.tables import Project
from app.repositories.project_repository import ProjectRepository
from app.validation import policies as P
from app.validation import validators as V
from app.validation.exceptions import Conflict, NotFound, ValidationFailed

log = logging.getLogger("orchestrator.projects")


class CloneFailed(Conflict):
    code = "CLONE_FAILED"


class CloneTerminationUnconfirmed(Conflict):
    code = "CLONE_TERMINATION_UNCONFIRMED"


def _make_writable(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)      # git object files are read-only on Windows
    func(path)


class ProjectService:
    def __init__(self, db: Database, executor: CommandExecutor, *, clone_root: Path,
                 protected: tuple[Path, ...] = ()):
        self.db, self._executor = db, executor
        self.clone_root = Path(clone_root).resolve()
        self.protected = tuple(Path(p).resolve() for p in (*protected, self.clone_root))

    # --- registration --------------------------------------------------------------------------------
    async def create_project(self, *, name: str, workspace_path: str | None = None, repository_url: str | None = None,
                             test_policy: dict | None = None) -> Project:
        name = V.validate_project_name(name)
        policy = V.validate_test_policy(test_policy) if test_policy is not None else None
        if (workspace_path is None) == (repository_url is None):
            raise ValidationFailed("exactly one of workspace_path or repository_url is required")
        if workspace_path is not None:
            resolved = V.validate_project_path(workspace_path, protected=self.protected)
            return await self._insert(name, resolved, None, policy)
        return await self._clone_and_register(name, V.validate_repository_url(repository_url), policy)

    async def _insert(self, name: str, resolved: Path, repository_url: str | None, policy: dict | None) -> Project:
        key = V.workspace_key(resolved)
        try:
            # the workspace as registered is the first allowed baseline (an already-dirty git tree is not an error)
            baseline = await asyncio.to_thread(compute_fingerprint, resolved)
        except FingerprintError as exc:
            raise ValidationFailed("workspace could not be fingerprinted", {"reason": str(exc)}) from exc
        async with self.db.transaction() as s:
            repo = ProjectRepository(s)
            await repo.lock_registration()
            V.validate_no_project_overlap(key, await repo.workspace_keys())
            return await repo.add(Project(
                id=uuid.uuid4(), name=name, workspace_path=str(resolved), workspace_key=key,
                repository_url=repository_url, status="ACTIVE", test_policy=policy, write_epoch=0,
                baseline_fingerprint=baseline.value, workspace_state="CLEAN", row_version=0))

    async def _clone_and_register(self, name: str, clean_url: str, policy: dict | None) -> Project:
        self.clone_root.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:40] or "project"
        target = self.clone_root / f"{slug}-{uuid.uuid4().hex[:8]}"
        target.mkdir()                       # non-recursive: fails if it exists, so we own what we created
        try:
            await self._run_clone(clean_url, target)
            resolved = V.validate_project_path(
                str(target), protected=tuple(p for p in self.protected if p != self.clone_root))
            return await self._insert(name, resolved, clean_url, policy)
        except CloneTerminationUnconfirmed:
            raise                            # the process may still be writing: never delete under it
        except BaseException:
            await asyncio.shield(asyncio.to_thread(self._remove_created, target))
            raise

    async def _run_clone(self, clean_url: str, target: Path) -> None:
        env = {"GIT_TERMINAL_PROMPT": "0"}
        if os.environ.get("SSH_AUTH_SOCK"):
            env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        result = await self._executor.run(CommandSpec(
            argv=["git", "-c", "protocol.ext.allow=never", "clone", "--", clean_url, str(target)],
            cwd=self.clone_root, timeout_seconds=P.PROJECT_CLONE_TIMEOUT_SECONDS, idle_seconds=None,
            extra_env=env, workspace_root=self.clone_root))
        if result.termination is not None and result.termination not in {str(t) for t in CONFIRMED_GONE} \
                and result.termination != "KILLED_BY_HANDLE":
            log.error("git clone could not be proven stopped; leaving %s in place (not registered)", target)
            raise CloneTerminationUnconfirmed("git clone could not be proven stopped; the directory was left in place")
        if result.outcome is not CliOutcome.SUCCESS:
            tail = redact_credentials((result.stderr or result.stdout)[-500:])
            raise CloneFailed("git clone failed", {"outcome": str(result.outcome), "detail": tail})

    def _remove_created(self, target: Path) -> None:
        """Removes only a directory this request created inside the clone root."""
        try:
            resolved = target.resolve()
            if resolved.parent != self.clone_root or target.is_symlink() or not resolved.is_dir():
                log.error("refusing to remove unexpected path %s", target)
                return
            shutil.rmtree(resolved, onexc=_make_writable)
        except OSError:
            log.exception("could not remove incomplete clone directory %s", target)

    # --- read / update ----------------------------------------------------------------------------------
    async def get_project(self, project_id: uuid.UUID) -> Project:
        async with self.db.transaction() as s:
            project = await ProjectRepository(s).get(project_id)
        if project is None:
            raise NotFound("project not found")
        return project

    async def list_projects(self, status: str | None, limit: int, offset: int) -> list[Project]:
        async with self.db.transaction() as s:
            return await ProjectRepository(s).list(status=status, limit=limit, offset=offset)

    async def update_project(self, project_id: uuid.UUID, changes: dict) -> Project:
        """`changes` holds only the fields the client sent. Omitted: unchanged. `test_policy`: an object is validated
        and replaces the policy, null removes it (SQL NULL). `name` / `status` cannot be null. Tasks that already
        copied the policy keep their copy and hash."""
        for key in ("name", "status"):
            if key in changes and changes[key] is None:
                raise ValidationFailed(f"project {key} cannot be null", {"field": key})
        async with self.db.transaction() as s:
            repo = ProjectRepository(s)
            project = await repo.lock(project_id)
            if project is None:
                raise NotFound("project not found")
            values: dict = {}
            if "name" in changes:
                values["name"] = V.validate_project_name(changes["name"])
            if "test_policy" in changes:
                policy = changes["test_policy"]
                values["test_policy"] = null() if policy is None else V.validate_test_policy(policy)
            status = changes.get("status")
            if status is not None and V.validate_project_status(status) != project.status:
                if status == "ARCHIVED":
                    open_tasks = await repo.count_open_tasks(project_id)
                    if open_tasks:
                        raise Conflict("project has unfinished tasks; archive it after they finish",
                                       {"open_tasks": open_tasks})
                values["status"] = status
            if values:
                await repo.update_fields(project_id, **values)
                await s.refresh(project)
            return project
