"""CHANGES_MANIFEST_V1: what a task's workspace looked like when it took the write lock, in a form that can be diffed later.

This is NOT the Project fingerprint (WORKSPACE_FINGERPRINT_V1, one opaque digest that proves "unchanged") and NOT the Step
snapshot (STEP_SNAPSHOT_V1, `path -> "sha256:mode"`); both are untouched. Here every path has a tagged entry:

    FILE     {"kind": "FILE", "sha256": <64 hex>, "executable": bool}    executable = the working-tree lstat bit
    SYMLINK  {"kind": "SYMLINK", "target": <string>}                      the target is never opened
    GITLINK  {"kind": "GITLINK", "oid": <40|64 hex>}                      submodule: only its commit id, not its content

Included, by mode:
    GIT      tracked files and untracked NON-ignored files (ignored build output is excluded); kinds from the index/lstat.
    CONTENT  the workspace_policy walk (skipped directories and volatile artifacts excluded), for a non-git workspace.

No file CONTENT is stored, only hashes, so an exact text diff exists only where the old content can be read from git.
`measure` takes a `Deadline`: it is checked between entries and between hash chunks (cooperative), git is bounded by it.
"""
import fnmatch
import gzip
import hashlib
import json
import os
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

from app.infra.deadline import Deadline
from app.infra.workspace_fingerprint import GitInventoryError, git_inventory
from app.infra.workspace_policy import is_link_or_junction, is_volatile, read_link_target, walk_workspace
from app.validation import policies as P

MANIFEST_VERSION = "CHANGES_MANIFEST_V1"
SUPPORTED_VERSIONS = frozenset({MANIFEST_VERSION})
MODES = ("GIT", "CONTENT")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_CHUNK = 1 << 20


class ManifestFailure(Exception):
    """A manifest could not be measured, stored or read. `reason` is the stable code stored/returned as the cause."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class Measured:
    mode: str                              # GIT | CONTENT
    head_kind: str | None                  # COMMIT | UNBORN for GIT, None for CONTENT
    head_sha: str | None
    entries: dict
    dirty_paths: tuple[str, ...]


@dataclass(frozen=True)
class Encoded:
    data: bytes                            # gzip of the canonical JSON
    uncompressed_bytes: int
    sha256: str                            # of the UNcompressed canonical JSON bytes
    file_count: int


@dataclass(frozen=True)
class Change:
    path: str
    change_type: str                       # ADDED | REMOVED | MODIFIED | RENAMED
    kind: str                              # the kind the path has now (or had, when removed)
    previous_path: str | None
    base: dict | None
    now: dict | None
    pre_existing_dirty: bool


# --- paths ----------------------------------------------------------------------------------------------------------------
def _has_control(text: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def safe_rel_path(path: str, *, max_len: int, reason: str) -> str:
    """A normalised relative POSIX path: no absolute path, drive, `..`/`.`/empty segment, NUL or control character."""
    if (not isinstance(path, str) or not path or len(path) > max_len or _has_control(path) or path.startswith("/")
            or "\\" in path or (len(path) > 1 and path[1] == ":")):
        raise ManifestFailure(reason, "unsafe path")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ManifestFailure(reason, "unsafe path")
    try:
        path.encode("utf-8")          # a name git gave us as non-UTF-8 bytes (surrogateescape) cannot be stored as JSON
    except UnicodeEncodeError:
        raise ManifestFailure(reason, "path is not valid UTF-8") from None
    return path


def is_sensitive_path(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in P.SENSITIVE_PATH_PATTERNS)


# --- entries ----------------------------------------------------------------------------------------------------------------
def check_entry(entry) -> dict:
    if not isinstance(entry, dict):
        raise ManifestFailure("MANIFEST_CORRUPT", "entry is not an object")
    kind = entry.get("kind")
    if kind == "FILE":
        if (set(entry) != {"kind", "sha256", "executable"} or not isinstance(entry["sha256"], str)
                or not _SHA256.fullmatch(entry["sha256"]) or not isinstance(entry["executable"], bool)):
            raise ManifestFailure("MANIFEST_CORRUPT", "invalid FILE entry")
    elif kind == "SYMLINK":
        target = entry.get("target")
        if (set(entry) != {"kind", "target"} or not isinstance(target, str) or not target or _has_control(target)
                or len(target) > P.MANIFEST_MAX_SYMLINK_TARGET_LEN):
            raise ManifestFailure("MANIFEST_CORRUPT", "invalid SYMLINK entry")
    elif kind == "GITLINK":
        if set(entry) != {"kind", "oid"} or not isinstance(entry["oid"], str) or not _OID.fullmatch(entry["oid"]):
            raise ManifestFailure("MANIFEST_CORRUPT", "invalid GITLINK entry")
    else:
        raise ManifestFailure("MANIFEST_CORRUPT", "unknown entry kind")
    return entry


def _hash_file(path: Path, deadline: Deadline | None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            if deadline is not None:
                deadline.check()
            digest.update(chunk)
    return digest.hexdigest()


def _link_ancestor(root: Path, rel: str, plain: set[str] | None = None) -> str | None:
    """`plain` memoises directories already seen not to be links, so a deep tree costs one lstat per directory."""
    parts = rel.split("/")
    for depth in range(1, len(parts)):
        candidate = "/".join(parts[:depth])
        if plain is not None and candidate in plain:
            continue
        if is_link_or_junction(root / candidate):
            return candidate
        if plain is not None:
            plain.add(candidate)
    return None


def _entry_for(root: Path, rel: str, deadline: Deadline | None) -> dict | None:
    """The working-tree state of one path; None for a path that is gone or is a directory."""
    full = root / rel
    try:
        st = full.lstat()
    except FileNotFoundError:
        return None
    if is_link_or_junction(full):
        return {"kind": "SYMLINK", "target": read_link_target(full)}
    if full.is_dir():
        return None
    # the same policy as the fingerprint: an executable bit only exists off Windows
    return {"kind": "FILE", "sha256": _hash_file(full, deadline),
            "executable": os.name != "nt" and bool(st.st_mode & 0o111)}


def _add(entries: dict, root: Path, rel: str, seen_links: set, deadline: Deadline | None,
         plain: set[str] | None = None) -> None:
    if deadline is not None:
        deadline.check()
    safe_rel_path(rel, max_len=P.MANIFEST_MAX_PATH_LEN, reason="INVALID_PATH")
    link = _link_ancestor(root, rel, plain)
    if link is not None:                                   # a file behind a directory link: the link is the one entry
        if link in seen_links:
            return
        seen_links.add(link)
        rel = link
    entry = _entry_for(root, rel, deadline)
    if entry is not None:
        entries[rel] = check_entry(entry)
    if len(entries) > P.MANIFEST_MAX_FILES:
        raise ManifestFailure("TOO_MANY_FILES", f"more than {P.MANIFEST_MAX_FILES} entries")


def normalize_dirty_paths(paths) -> list[str]:
    """Bounded and safe, or the whole supplemental baseline is unusable: a partial dirty list would make the clean/dirty
    classification wrong, so a limit is a failure (DIRTY_PATHS_LIMIT), not a truncation."""
    # git lists an untracked nested repository / directory as `dir/`: that is the directory, not a malformed path
    unique = sorted({p[:-1] if p.endswith("/") and len(p) > 1 else p for p in paths})
    if len(unique) > P.DIRTY_PATHS_MAX_ITEMS:
        raise ManifestFailure("DIRTY_PATHS_LIMIT", "too many dirty paths")
    out = [safe_rel_path(p, max_len=P.DIRTY_PATHS_MAX_PATH_LEN, reason="DIRTY_PATHS_INVALID") for p in unique]
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8", "surrogatepass")) > P.DIRTY_PATHS_MAX_SERIALIZED_BYTES:
        raise ManifestFailure("DIRTY_PATHS_LIMIT", "the dirty path list is too large")
    return out


# --- measuring ----------------------------------------------------------------------------------------------------------------
def measure(root: Path, *, deadline: Deadline | None = None, mode: str | None = None) -> Measured:
    """Measures the workspace in `mode` (None = GIT when the workspace qualifies, else CONTENT). Raises ManifestFailure
    (reason code) or DeadlineExceeded; never returns a partial result."""
    root = Path(root)
    if mode not in (None, *MODES):
        raise ManifestFailure("MODE_UNAVAILABLE", "unknown mode")
    try:
        inventory = None
        if mode != "CONTENT":
            inventory = git_inventory(root, deadline=deadline)
            if inventory is None and mode == "GIT":
                raise ManifestFailure("MODE_UNAVAILABLE", "the workspace is no longer a usable git workspace")
        entries: dict = {}
        if inventory is not None:
            seen_links: set[str] = set()
            plain: set[str] = set()
            for rel, git_mode, oid in inventory.tracked:
                if git_mode == "160000":
                    entries[safe_rel_path(rel, max_len=P.MANIFEST_MAX_PATH_LEN, reason="INVALID_PATH")] = check_entry(
                        {"kind": "GITLINK", "oid": oid})
                else:
                    _add(entries, root, rel, seen_links, deadline, plain)
            for rel in inventory.untracked:
                if rel.endswith("/"):         # an untracked nested git repository: its content is not this repository's
                    continue
                _add(entries, root, rel, seen_links, deadline, plain)
            return Measured("GIT", inventory.head_kind, inventory.head_sha, entries, tuple(normalize_dirty_paths(inventory.dirty)))
        walker = walk_workspace(root)
        for item in walker:
            if item.kind == "emptydir" or is_volatile(item.rel):
                continue
            _add(entries, root, item.rel, set(), deadline)
        return Measured("CONTENT", None, None, entries, ())
    except GitInventoryError as exc:
        raise ManifestFailure("GIT_ENUMERATION_FAILED", str(exc)) from exc
    except OSError as exc:
        raise ManifestFailure("FILESYSTEM_ERROR", f"{type(exc).__name__}") from exc


# --- encoding / decoding ----------------------------------------------------------------------------------------------------------
def canonical_json(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def encode(measured: Measured) -> Encoded:
    """Canonical JSON (sorted keys, fixed separators); the digest is of the UNcompressed bytes. Limits are checked first."""
    if len(measured.entries) > P.MANIFEST_MAX_FILES:
        raise ManifestFailure("TOO_MANY_FILES", "too many entries")
    raw = canonical_json({"schema": MANIFEST_VERSION, "mode": measured.mode, "entries": measured.entries})
    if len(raw) > P.MANIFEST_MAX_UNCOMPRESSED_BYTES:
        raise ManifestFailure("MANIFEST_TOO_LARGE", "the manifest is too large")
    packed = gzip.compress(raw, compresslevel=6, mtime=0)
    if len(packed) > P.MANIFEST_MAX_COMPRESSED_BYTES:
        raise ManifestFailure("MANIFEST_TOO_LARGE", "the compressed manifest is too large")
    return Encoded(packed, len(raw), hashlib.sha256(raw).hexdigest(), len(measured.entries))


def decode(data: bytes, *, version: str, expected_mode: str, expected_sha256: str, expected_bytes: int) -> dict:
    """The entries of a stored manifest, or a ManifestFailure. Decompression is BOUNDED: output is never allowed to exceed
    the recorded size (or the policy maximum), so a corrupt or malicious stream cannot exhaust memory."""
    if version not in SUPPORTED_VERSIONS:
        raise ManifestFailure("UNSUPPORTED_MANIFEST_VERSION", f"unsupported manifest version {version!r}")
    if len(data) > P.MANIFEST_MAX_COMPRESSED_BYTES or expected_bytes > P.MANIFEST_MAX_UNCOMPRESSED_BYTES:
        raise ManifestFailure("MANIFEST_TOO_LARGE", "the stored manifest exceeds the limits")
    limit = expected_bytes
    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        raw = stream.decompress(data, limit + 1)
    except zlib.error as exc:
        raise ManifestFailure("MANIFEST_CORRUPT", "the manifest is not valid gzip") from exc
    if len(raw) > limit or stream.unconsumed_tail:
        raise ManifestFailure("MANIFEST_TOO_LARGE", "the manifest expands beyond its recorded size")
    if not stream.eof or stream.unused_data or len(raw) != expected_bytes:
        raise ManifestFailure("MANIFEST_CORRUPT", "the manifest is truncated or has trailing data")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ManifestFailure("MANIFEST_CORRUPT", "the manifest digest does not match")
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ManifestFailure("MANIFEST_CORRUPT", "the manifest is not valid JSON") from exc
    if (not isinstance(document, dict) or set(document) != {"schema", "mode", "entries"}
            or document["schema"] != version or document["mode"] != expected_mode or expected_mode not in MODES
            or not isinstance(document["entries"], dict) or len(document["entries"]) > P.MANIFEST_MAX_FILES):
        raise ManifestFailure("MANIFEST_CORRUPT", "the manifest header does not match the baseline")
    for path, entry in document["entries"].items():
        safe_rel_path(path, max_len=P.MANIFEST_MAX_PATH_LEN, reason="MANIFEST_CORRUPT")
        check_entry(entry)
    return document["entries"]


# --- comparing ------------------------------------------------------------------------------------------------------------------
def compute_changes(base: dict, now: dict, dirty_at_start: frozenset[str] = frozenset()) -> list[Change]:
    """ADDED / REMOVED / MODIFIED, with RENAMED where one removed FILE and one added FILE share a content hash (paired
    one-to-one in path order). A path that was already dirty at the start is flagged, never claimed as the task's own."""
    added = sorted(now.keys() - base.keys())
    removed = sorted(base.keys() - now.keys())
    modified = sorted(p for p in base.keys() & now.keys() if base[p] != now[p])
    by_hash: dict[str, list[str]] = {}
    for path in removed:
        if base[path]["kind"] == "FILE":
            by_hash.setdefault(base[path]["sha256"], []).append(path)
    changes: list[Change] = []
    renamed_from: set[str] = set()
    for path in added:
        entry = now[path]
        candidates = by_hash.get(entry["sha256"]) if entry["kind"] == "FILE" else None
        if candidates:
            old = candidates.pop(0)
            renamed_from.add(old)
            changes.append(Change(path, "RENAMED", "FILE", old, base[old], entry, old in dirty_at_start or path in dirty_at_start))
        else:
            changes.append(Change(path, "ADDED", entry["kind"], None, None, entry, path in dirty_at_start))
    for path in removed:
        if path not in renamed_from:
            changes.append(Change(path, "REMOVED", base[path]["kind"], None, base[path], None, path in dirty_at_start))
    for path in modified:
        changes.append(Change(path, "MODIFIED", now[path]["kind"], None, base[path], now[path], path in dirty_at_start))
    return sorted(changes, key=lambda c: c.path)


def entries_digest(measured: Measured) -> str:
    """One digest of a whole measurement (mode + entries): two measurements of an unchanged workspace are equal."""
    return hashlib.sha256(canonical_json({"mode": measured.mode, "entries": measured.entries})).hexdigest()
