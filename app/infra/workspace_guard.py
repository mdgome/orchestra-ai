"""Workspace path validation: every cwd / --add-dir / artifact path must resolve under the root."""
import os
from pathlib import Path

from app.validation.exceptions import FailClosed


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


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
