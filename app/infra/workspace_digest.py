"""Canonical, versioned digests shared by the Step snapshot and the Project fingerprint.

Two values with different purposes carry different version prefixes, so one can never be mistaken for the other:
  STEP_SNAPSHOT_V1          Step reuse, code freshness, Completion        (`workspace_snapshot`)
  WORKSPACE_FINGERPRINT_V1  Project baseline, DIRTY, FingerprintProof     (`workspace_fingerprint`)
`is_current_version` only accepts the prefix of the purpose it is asked about. A schema version change means stored
values can no longer be compared and must not be reused.

Standard library only: this module imports no other workspace module and knows no domain exception.
"""
import hashlib
import struct
from dataclasses import dataclass

STEP_SNAPSHOT_V1 = "STEP_SNAPSHOT_V1"
WORKSPACE_FINGERPRINT_V1 = "WORKSPACE_FINGERPRINT_V1"


@dataclass(frozen=True)
class Entry:
    tag: str         # FILE SYMLINK DELETE DIRECTORY GIT_INDEX UNTRACKED SUBMODULE HEAD
    path: str        # workspace-relative, '/' separated, case preserved
    mode: str
    value: bytes


def is_current_version(value: str | None, version: str) -> bool:
    return bool(value) and value.startswith(version + ":")


def _field32(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _field64(data: bytes) -> bytes:
    return struct.pack(">Q", len(data)) + data


def canonical_digest(version: str, mode: str, entries: list[Entry]) -> str:
    """Length-prefixed, byte-ordered serialisation: ('a','bc') and ('ab','c') can never collide."""
    h = hashlib.sha256()
    h.update(_field32(version.encode("utf-8")))
    h.update(_field32(mode.encode("utf-8")))
    for e in sorted(entries, key=lambda x: (x.path.encode("utf-8"), x.tag)):
        h.update(_field32(e.tag.encode("ascii")))
        h.update(_field64(e.path.encode("utf-8")))
        h.update(_field32(e.mode.encode("utf-8")))
        h.update(_field64(e.value))
    return f"{version}:{h.hexdigest()}"
