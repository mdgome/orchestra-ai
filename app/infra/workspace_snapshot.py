"""WorkspaceSnapshot: the Step-level record of a workspace.

Two things, two purposes (do not mix them up with `workspace_fingerprint`):
  * the manifest (`files`, `volatile`): path -> "digest:mode". It exists to EXPLAIN a change (which paths were added,
    removed or modified, `diff_snapshots`). It cannot restore content.
  * `code_fingerprint`: `STEP_SNAPSHOT_V1:<sha256>` over the non-volatile entries of the SAME walk. It is the value
    Step reuse, code freshness and Completion compare. It ignores `.gitignore` (a plain filesystem walk).
The Project baseline / FingerprintProof use `workspace_fingerprint.compute_fingerprint` (git-aware, a different
prefix); the two values are never compared with each other.

Fail-closed: any enumeration, file-read or link-target-read error raises SnapshotError. There is no placeholder for an
unreadable file and no partial manifest is ever returned, so an unreadable file cannot hide a content change.
This module and `workspace_fingerprint` do not import each other; both import only the common modules below them.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.infra.workspace_digest import STEP_SNAPSHOT_V1, Entry, canonical_digest, is_current_version
from app.infra.workspace_policy import (  # noqa: F401 - re-exported for existing importers
    SKIP_DIRS, describe_os_error, is_volatile, read_link_target, require_directory, skipped_dir, walk_workspace,
)
from app.infra.workspace_policy import hash_file as _hash_file
from app.validation.exceptions import FailClosed


class SnapshotError(FailClosed):
    """The workspace could not be snapshotted completely. The step result / checkpoint must not be reused."""
    code = "SNAPSHOT_FAILED"


def is_current_snapshot_version(value: str | None) -> bool:
    return is_current_version(value, STEP_SNAPSHOT_V1)


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
    """Manifest of every file under root (skipping SKIP_DIRS) plus the versioned `code_fingerprint`.

    The whole walk — root check, directory enumeration, every file hash and every link target — is one conversion
    boundary: an OSError anywhere becomes `SnapshotError(...) from exc` and nothing partial escapes."""
    root = Path(root)
    files: dict[str, str] = {}
    volatile: dict[str, str] = {}
    entries: list[Entry] = []
    try:
        require_directory(root)
    except OSError as exc:
        raise SnapshotError(describe_os_error("enumerate", root, exc), {"root": str(root)}) from exc
    walker = walk_workspace(root)
    while True:
        try:
            item = next(walker)
        except StopIteration:
            break
        except OSError as exc:                       # raised while the generator was being consumed
            raise SnapshotError(describe_os_error("enumerate", root, exc), {"root": str(root)}) from exc
        if item.kind == "emptydir":
            continue                                 # the manifest has always been about files
        into = volatile if is_volatile(item.rel) else files
        if item.kind == "link":
            try:
                target = read_link_target(item.full)
            except OSError as exc:
                raise SnapshotError(describe_os_error("readlink", root, exc), {"root": str(root)}) from exc
            into[item.rel] = f"link:{target}:-"
            if into is files:
                entries.append(Entry("SYMLINK", item.rel, "-", target.encode("utf-8", "surrogateescape")))
            continue
        try:
            digest = _hash_file(item.full)
        except OSError as exc:
            raise SnapshotError(describe_os_error("read", root, exc), {"root": str(root)}) from exc
        mode = "x" if os.name != "nt" and os.access(item.full, os.X_OK) else "-"
        into[item.rel] = f"{digest.hex()}:{mode}"
        if into is files:
            entries.append(Entry("FILE", item.rel, mode, digest))
    return {"files": files, "volatile": volatile,
            "code_fingerprint": canonical_digest(STEP_SNAPSHOT_V1, "SNAPSHOT", entries)}


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
