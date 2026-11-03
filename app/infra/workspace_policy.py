"""Domain-neutral workspace rules shared by the Step snapshot and the Project fingerprint.

Both measurements must agree on WHAT is part of a workspace (skipped directories, volatile artifacts, build output,
links that are never followed), so that policy lives here, below both of them. This module (like `workspace_digest`)
imports neither `workspace_snapshot` nor `workspace_fingerprint` and knows no domain exception: every filesystem
failure surfaces as the original `OSError`, and each caller converts it at its own boundary
(`SnapshotError` / `FingerprintError`, always `raise ... from exc`).
"""
import errno
import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from app.validation import policies as P

SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", ".tox", ".gradle", ".idea"})
_GRADLE_FILES = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")


def skipped_dir(parent: Path, name: str) -> bool:
    """Build output is only recognised next to its build file: a Java package may legitimately be called `build`."""
    if name == "target":
        return (parent / "pom.xml").exists()
    if name == "build":
        return any((parent / f).exists() for f in _GRADLE_FILES)
    return False


def is_volatile(rel: str) -> bool:
    """Regenerated artifacts (reports, caches, orchestrator notes): never part of a code measurement."""
    rel = rel.replace("\\", "/")
    for pattern in P.VOLATILE_PATH_PATTERNS:
        if pattern.endswith("/"):
            if rel.startswith(pattern) or f"/{pattern}" in f"/{rel}":
                return True
        elif rel == pattern or rel.endswith("/" + pattern):
            return True
    return False


def is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def read_link_target(path: Path) -> str:
    """A link is described by its target string; its target is never opened (it may lie outside the workspace).
    A failure is the original OSError: there is no 'UNREADABLE' placeholder."""
    return os.readlink(path)


def hash_file(path: Path) -> bytes:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.digest()


def require_directory(root: Path) -> None:
    """The workspace root must exist and be a directory BEFORE anything is walked."""
    if not root.exists():
        raise FileNotFoundError(errno.ENOENT, "workspace root does not exist", str(root))
    if not root.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "workspace root is not a directory", str(root))


def describe_os_error(operation: str, root: Path, exc: OSError) -> str:
    """Message for a domain error: the failing path when the OSError carries one, otherwise only the root and the
    operation (`enumerate` / `read` / `readlink`): a path is never guessed."""
    reason = exc.strerror or str(exc)
    filename = getattr(exc, "filename", None)
    if filename:
        return f"{operation} failed at {filename}: {reason}"
    return f"{operation} failed under workspace root {root} (failing path unknown): {reason}"


class WalkItem(NamedTuple):
    kind: str        # "file" | "link" | "emptydir"
    rel: str         # workspace-relative, '/' separated, case preserved
    full: Path


def walk_workspace(root: Path) -> Iterator[WalkItem]:
    """Every file, link and empty directory below root (skipped directories excluded, links never descended into).

    An enumeration error (unreadable or vanished directory) is raised as the original OSError while the generator
    is being consumed — callers must wrap the WHOLE iteration, not just this call."""

    def onerror(exc: OSError) -> None:
        raise exc

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        here = Path(dirpath)
        kept = []
        for d in sorted(dirnames):
            full = here / d
            if d in SKIP_DIRS or skipped_dir(here, d):
                continue
            if is_link_or_junction(full):
                yield WalkItem("link", full.relative_to(root).as_posix(), full)
                continue
            kept.append(d)
        dirnames[:] = kept
        if not kept and not filenames and here != root:
            yield WalkItem("emptydir", here.relative_to(root).as_posix(), here)
        for name in sorted(filenames):
            full = here / name
            kind = "link" if is_link_or_junction(full) else "file"
            yield WalkItem(kind, full.relative_to(root).as_posix(), full)
