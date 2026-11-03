"""WORKSPACE_FINGERPRINT_V1: a verifiable fingerprint of a project workspace.

Two modes, chosen per workspace:
  GIT      the workspace has a plain in-tree `.git` directory and a usable `git`. The fingerprint covers HEAD (or
           UNBORN), every index entry (stage, mode, blob OID) and the ACTUAL CONTENT of every tracked file and of
           every untracked, non-ignored file. Ignored build output therefore never affects it. Content is always
           read from the working tree and hashed: git's stat cache is never trusted, so an edit that keeps the
           same size and mtime is still detected.
  CONTENT  everything else (no repo, external gitdir, git unavailable/untrusted): a content-hash walk.

Safety: no symlink or junction is ever followed (a link contributes its target *string*), git runs without a shell
with hooks/fsmonitor/user+system config disabled and with every inherited GIT_* variable removed, and any failure
raises FingerprintError so the caller can treat the workspace as DIRTY_UNRESOLVED instead of CLEAN.

Canonical format: each entry is length-prefixed (tag, path, mode, value) with explicit UTF-8 encoding and a
fixed byte order, so nothing depends on repr(), dict order or the OS default encoding. A change of VERSION means
stored baselines and checkpoints can no longer be compared and must not be reused.
"""
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.infra import workspace_digest as _digest
from app.infra.workspace_digest import WORKSPACE_FINGERPRINT_V1, Entry
from app.infra.workspace_policy import hash_file as _hash_file
from app.infra.workspace_policy import (
    describe_os_error, is_link_or_junction, is_volatile, read_link_target, require_directory, walk_workspace,
)

VERSION = WORKSPACE_FINGERPRINT_V1       # this module's purpose; the Step snapshot uses its own prefix
GIT_TIMEOUT_SECONDS = 120
MIN_GIT_VERSION = (2, 32)       # GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM
_GIT_ENV_KEEP = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR")

log = logging.getLogger("orchestrator.fingerprint")


class FingerprintError(Exception):
    """The workspace could not be measured. Never treat this as CLEAN."""


class _GitUnusable(Exception):
    pass


@dataclass(frozen=True)
class Fingerprint:
    value: str       # "WORKSPACE_FINGERPRINT_V1:<sha256 hex>"
    mode: str        # "GIT" | "CONTENT"


def is_current_version(value: str | None) -> bool:
    """True only for a WORKSPACE_FINGERPRINT value: a STEP_SNAPSHOT value is never accepted here."""
    return _digest.is_current_version(value, VERSION)


def canonical_digest(mode: str, entries: list[Entry]) -> str:
    return _digest.canonical_digest(VERSION, mode, entries)


def _worktree_entry(root: Path, rel: str, tag: str, *, missing_ok: bool = False) -> Entry:
    """The current on-disk state of one path. Links are described by their target string, never followed.

    `missing_ok` is only for a path git still tracks: its absence is a real DELETE. For anything the walk or
    `ls-files` just listed, a path that vanished before it could be read is a race, not a deletion: an error.
    Every OSError becomes `FingerprintError ... from exc`."""
    full = root / rel
    try:
        st = full.lstat()
    except FileNotFoundError as exc:
        if missing_ok:
            return Entry("DELETE", rel, "-", b"")
        raise FingerprintError(describe_os_error("read", root, exc) + f" ({rel} vanished while it was measured)") from exc
    except OSError as exc:
        raise FingerprintError(describe_os_error("read", root, exc)) from exc
    if is_link_or_junction(full):
        try:
            target = read_link_target(full)
        except OSError as exc:
            raise FingerprintError(describe_os_error("readlink", root, exc)) from exc
        return Entry("SYMLINK", rel, "-", target.encode("utf-8", "surrogateescape"))
    if full.is_dir():
        return Entry("DIRECTORY", rel, "-", b"")
    mode = "x" if os.name != "nt" and st.st_mode & 0o111 else "-"
    try:
        return Entry(tag, rel, mode, _hash_file(full))
    except OSError as exc:
        raise FingerprintError(describe_os_error("read", root, exc)) from exc


def _link_ancestor(root: Path, rel: str) -> str | None:
    """The top-most directory link/junction on the way to `rel`. git (on Windows) lists files behind a junction,
    which must never be read: the link itself becomes the single entry."""
    parts = rel.split("/")
    for depth in range(1, len(parts)):
        candidate = "/".join(parts[:depth])
        if is_link_or_junction(root / candidate):
            return candidate
    return None


def _git_path_entry(root: Path, rel: str, tag: str, seen_links: set[str], *, missing_ok: bool) -> Entry | None:
    link = _link_ancestor(root, rel)
    if link is None:
        return _worktree_entry(root, rel, tag, missing_ok=missing_ok)
    if link in seen_links:
        return None
    seen_links.add(link)
    return _worktree_entry(root, link, tag, missing_ok=missing_ok)


def _content_entries(root: Path) -> list[Entry]:
    """Content walk. The whole iteration is one conversion boundary: an OSError raised while the generator is being
    consumed (unreadable / vanished directory) is a FingerprintError too, and no partial list escapes."""
    entries: list[Entry] = []
    walker = walk_workspace(root)
    while True:
        try:
            item = next(walker)
        except StopIteration:
            break
        except OSError as exc:
            raise FingerprintError(describe_os_error("enumerate", root, exc)) from exc
        if item.kind == "emptydir":
            entries.append(Entry("DIRECTORY", item.rel, "-", b""))
        elif not is_volatile(item.rel):
            entries.append(_worktree_entry(root, item.rel, "FILE"))
    return entries


# --- git ---------------------------------------------------------------------------------------------------------
def git_env() -> dict[str, str]:
    """A fixed environment: nothing inherited from GIT_* , user or system config disabled."""
    env = {k: v for k, v in os.environ.items() if k.upper() in _GIT_ENV_KEEP}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    return env


def git_global_args(root: Path) -> list[str]:
    """Global git options only (before the sub-command)."""
    return ["git", f"--git-dir={root / '.git'}", f"--work-tree={root}", "--no-optional-locks",
            "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "-c", "core.untrackedCache=false",
            "-c", "core.quotepath=off"]


@lru_cache(maxsize=1)
def _git_version() -> tuple[int, ...] | None:
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30, env=git_env(),
                             check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _git(root: Path, subcommand: list[str], *, ok_codes=(0,)) -> bytes:
    try:
        proc = subprocess.run([*git_global_args(root), *subcommand], capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
                              env=git_env(), cwd=root, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _GitUnusable(f"git {subcommand[0]} failed to run: {exc}") from exc
    if proc.returncode not in ok_codes:
        raise _GitUnusable(f"git {subcommand[0]} exited {proc.returncode}")
    return proc.stdout


def _git_usable(root: Path) -> None:
    version = _git_version()
    if version is None or version < MIN_GIT_VERSION:
        raise _GitUnusable("git is unavailable or too old")
    gitdir = root / ".git"
    if not gitdir.is_dir() or is_link_or_junction(gitdir):
        raise _GitUnusable(".git is not a plain in-tree directory")           # gitfile / worktree / external gitdir
    if (gitdir / "commondir").exists() or (gitdir / "objects" / "info" / "alternates").exists():
        raise _GitUnusable("repository borrows objects or state from outside the workspace")
    if _git(root, ["config", "--local", "--get", "core.worktree"], ok_codes=(0, 1)).strip():
        raise _GitUnusable("core.worktree is set")


def _git_entries(root: Path) -> list[Entry]:
    _git_usable(root)
    entries: list[Entry] = []
    head = _git(root, ["rev-parse", "--verify", "-q", "HEAD"], ok_codes=(0, 1)).strip()
    entries.append(Entry("HEAD", "", "-", head or b"UNBORN"))
    staged = _git(root, ["ls-files", "--stage", "-z"])
    tracked: set[str] = set()
    seen_links: set[str] = set()
    for raw in staged.split(b"\0"):
        if not raw:
            continue
        meta, _, path_b = raw.partition(b"\t")
        mode, oid, stage = meta.decode("ascii").split(" ")
        rel = path_b.decode("utf-8", "surrogateescape")
        if is_volatile(rel):
            continue
        if mode == "160000":                                    # submodule: gitlink OID + inner content
            inner = root / rel
            inner_digest = canonical_digest("CONTENT", _content_entries(inner)) if inner.is_dir() else "MISSING"
            entries.append(Entry("SUBMODULE", rel, mode, f"{oid}:{inner_digest}".encode("ascii")))
            tracked.add(rel)
            continue
        entries.append(Entry("GIT_INDEX", rel, mode, f"{stage}:{oid}".encode("ascii")))
        if rel not in tracked:
            tracked.add(rel)
            entry = _git_path_entry(root, rel, "FILE", seen_links, missing_ok=True)   # tracked: absence = DELETE; read from disk
            if entry is not None:
                entries.append(entry)
    for raw in _git(root, ["ls-files", "--others", "--exclude-standard", "-z"]).split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "surrogateescape")
        if not is_volatile(rel):
            entry = _git_path_entry(root, rel, "UNTRACKED", seen_links, missing_ok=False)
            if entry is not None:
                entries.append(entry)
    return entries


def compute_fingerprint(root: Path) -> Fingerprint:
    """Measures the workspace. Any filesystem failure is a FingerprintError (`raise ... from exc`); a git failure
    falls back to the content walk, whose own failures are FingerprintErrors too. Never returns a partial value."""
    root = Path(root)
    try:
        require_directory(root)
    except OSError as exc:
        raise FingerprintError(describe_os_error("enumerate", root, exc)) from exc
    try:
        entries = _git_entries(root)
        return Fingerprint(canonical_digest("GIT", entries), "GIT")
    except _GitUnusable as exc:
        log.info("git-aware fingerprint unavailable for %s (%s); using content hashing", root, exc)
    except FingerprintError:
        raise                                   # already the domain error: not wrapped a second time
    except OSError as exc:
        raise FingerprintError(describe_os_error("read", root, exc)) from exc
    try:
        return Fingerprint(canonical_digest("CONTENT", _content_entries(root)), "CONTENT")
    except FingerprintError:
        raise
    except OSError as exc:
        raise FingerprintError(describe_os_error("read", root, exc)) from exc
