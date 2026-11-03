"""Workspace path validation: every cwd / --add-dir / artifact path must resolve under the root."""
import os
from pathlib import Path

from app.validation.exceptions import FailClosed


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def resolve_project_root(path: str | os.PathLike) -> Path:
    """Re-validates a registered project workspace before every run (it lives outside WORKSPACE_ROOT).

    The path was link-free and canonical at registration; anything that changed since then fails closed."""
    candidate = Path(path)
    if not candidate.is_absolute():
        raise FailClosed("project workspace must be an absolute path", {"path": str(path)})
    probe = candidate
    while probe != probe.parent:
        if probe.exists() and _is_link_or_junction(probe):
            raise FailClosed("project workspace traverses a symlink or junction", {"path": str(path)})
        probe = probe.parent
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FailClosed("project workspace is missing or inaccessible", {"path": str(path)}) from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate)):
        raise FailClosed("project workspace no longer resolves to its registered location", {"path": str(path)})
    if not resolved.is_dir() or not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
        raise FailClosed("project workspace is not an accessible directory", {"path": str(path)})
    return resolved


class WorkspaceGuard:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def resolve_inside(self, path: str | os.PathLike, *, must_exist: bool = False) -> Path:
        """Return the real path, rejecting `..`, symlink/junction escapes and absolute paths elsewhere."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        # Reject any link/junction component below the root, even if it currently points inside.
        probe = candidate
        while probe != probe.parent:
            if probe == self.root:
                break
            if probe.exists() and _is_link_or_junction(probe):
                raise FailClosed("path traverses a symlink or junction", {"path": str(path)})
            probe = probe.parent
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise FailClosed("path escapes the workspace root", {"path": str(path)})
        if must_exist and not resolved.exists():
            raise FailClosed("path does not exist", {"path": str(path)})
        return resolved

    def task_workspace(self, task_id) -> Path:
        path = self.resolve_inside(self.root / str(task_id))
        path.mkdir(parents=True, exist_ok=True)
        return path
