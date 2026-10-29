"""WorkspaceSnapshot: change *detection* only. A manifest of path -> content hash cannot restore files.

`files` holds code (tracked, modified-uncommitted and new untracked files alike: a plain filesystem walk
sees all of them). `volatile` holds regenerated artifacts (test reports, caches) and is excluded from
`code_fingerprint`, so a changed test report never invalidates the step that produced the code.
"""
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.validation import policies as P

SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", ".tox"})


def _is_volatile(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    for pattern in P.VOLATILE_PATH_PATTERNS:
        if pattern.endswith("/"):
            if rel.startswith(pattern) or f"/{pattern}" in f"/{rel}":
                return True
        elif rel == pattern or rel.endswith("/" + pattern):
            return True
    return False


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class SnapshotDiff:
    added: frozenset[str] = field(default_factory=frozenset)
    removed: frozenset[str] = field(default_factory=frozenset)
    modified: frozenset[str] = field(default_factory=frozenset)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.modified)

    def paths(self) -> list[str]:
        return sorted(self.added | self.removed | self.modified)


def take_snapshot(root: Path) -> dict:
    """Manifest of every file under root (skipping SKIP_DIRS). Includes the executable bit on POSIX."""
    files: dict[str, str] = {}
    volatile: dict[str, str] = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            try:
                digest = _hash_file(full)
                mode = "x" if os.access(full, os.X_OK) and os.name != "nt" else "-"
            except OSError:
                digest, mode = "UNREADABLE", "-"
            (volatile if _is_volatile(rel) else files)[rel] = f"{digest}:{mode}"
    return {"files": files, "volatile": volatile, "code_fingerprint": fingerprint(files)}


def fingerprint(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def diff_snapshots(before: dict | None, after: dict | None) -> SnapshotDiff:
    """Code-file differences only (volatile artifacts are ignored)."""
    if before is None or after is None:
        raise ValueError("cannot diff a missing snapshot")
    b, a = before["files"], after["files"]
    return SnapshotDiff(
        added=frozenset(a.keys() - b.keys()),
        removed=frozenset(b.keys() - a.keys()),
        modified=frozenset(k for k in a.keys() & b.keys() if a[k] != b[k]),
    )
