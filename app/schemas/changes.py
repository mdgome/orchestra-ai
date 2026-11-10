"""Response shape of GET /tasks/{id}/changes and its size contract (same pipeline as the report, see schemas/reports.py).

`lock_consistency` (DB: owner, write epoch and workspace state compared before and after) and `workspace_consistency`
(the workspace itself, measured twice) are separate on purpose: an outside process can change files without touching any
database row, so a STABLE lock says nothing about the files. `workspace_consistency` is STABLE only when two real
measurements agree, UNVERIFIED when no second measurement was made (the default) or it ran out of time.
"""
import uuid

from pydantic import BaseModel

from app.infra.response_sanitizer import SanitizeReason
from app.schemas.reports import SanitizationOut, _note, finalize
from app.validation import policies as P


class _Out(BaseModel):
    pass


class ChangeItemOut(_Out):
    path: str
    change_type: str                    # ADDED | REMOVED | MODIFIED | RENAMED
    kind: str                           # FILE | SYMLINK | GITLINK
    previous_path: str | None = None    # RENAMED
    binary: bool = False
    pre_existing_dirty: bool = False    # already different from HEAD when the task started: not claimed as the task's own
    diff_status: str                    # AVAILABLE | UNAVAILABLE | WITHHELD | TRUNCATED
    diff_reason: str | None = None
    diff: str | None = None
    base_target: str | None = None      # SYMLINK: the target before / after (strings only)
    target: str | None = None


class DiffBasisOut(_Out):
    """What the changes are measured against. The three epochs: `proof_epoch` = the project epoch when the fingerprint was
    measured (before acquisition), `acquired_epoch` = the epoch the lock acquisition produced (proof_epoch + 1 for origin
    ACQUISITION; for a recovery it is the owner epoch proven by the fingerprint), `recorded_epoch` = the epoch when the row was
    written. A recovery that could not prove the original epoch has null proof/acquired epochs: they are never invented."""
    origin: str
    mode: str
    head_kind: str | None = None
    head_sha: str | None = None
    proof_epoch: int | None = None
    acquired_epoch: int | None = None
    recorded_epoch: int
    manifest_version: str
    file_count: int


class ChangesOut(_Out):
    task_id: uuid.UUID
    state: str                          # AVAILABLE | UNAVAILABLE
    unavailable_reason: str | None = None
    precision: str                      # HASH_COMPARISON | NONE
    note: str                           # always: this is a LIVE workspace comparison, not a point-in-time snapshot
    diff_basis: DiffBasisOut | None = None
    workspace_relation: str             # CURRENT_OWNER | OTHER_TASK_OWNS | UNOWNED_AFTER_TASK | UNVERIFIED
    lock_consistency: str               # STABLE | UNSTABLE
    workspace_consistency: str          # STABLE | UNSTABLE | UNVERIFIED
    workspace_consistency_reason: str | None = None
    supported_scope: str | None = None
    limitations: list[str]
    changes: list[ChangeItemOut]
    total_changes: int
    returned: int
    has_more: bool
    next_offset: int | None = None
    limit: int
    offset: int
    truncated: bool = False
    sanitization: SanitizationOut


def _drop_bodies(payload: dict) -> bool:
    """Size cut 1: diff bodies, last item first, through the fields this schema reserves (diff_status, diff_reason)."""
    dropped = False
    for item in reversed(payload["changes"]):
        if item.get("diff"):
            item.update(diff=None, diff_status="TRUNCATED", diff_reason="SIZE_LIMIT")
            dropped = True
            break
    if dropped:
        payload["truncated"] = True
        _note(payload, SanitizeReason.SIZE_LIMIT, section="changes[].diff")
    return dropped


def _drop_items(payload: dict) -> bool:
    """Size cut 2: whole items from the end of the page; next_offset says where to continue."""
    items = payload["changes"]
    if len(items) <= 1:
        return False
    drop = max(1, len(items) // 2)
    del items[-drop:]
    payload["returned"] = len(items)
    payload["next_offset"] = payload["offset"] + payload["returned"]
    payload["has_more"] = True
    payload["truncated"] = True
    _note(payload, SanitizeReason.SIZE_LIMIT, items=drop)
    return True


def _fallback(payload: dict) -> dict:
    out = {**payload, "changes": [], "returned": 0, "has_more": payload["total_changes"] > payload["offset"],
           "next_offset": payload["offset"], "truncated": True}
    _note(out, SanitizeReason.SIZE_LIMIT, section="changes", items=len(payload["changes"]))
    return out


def render_changes(payload: dict) -> ChangesOut:
    return finalize(ChangesOut, payload, P.CHANGES_RESPONSE_MAX_BYTES, [_drop_bodies, _drop_items], _fallback)
