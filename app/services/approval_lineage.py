"""The approval lineage of design 7.5 item 11: keys written ONCE into the `target` of a new gate at its INSERT (never an UPDATE of an
existing Approval), the successor lookup by the `supersedes_approval_id` link only (never by time or UUID order), and the
23505 / `uq_approvals_lineage_successor` detection of the lineage race on the ordinary path (C-7)."""
import uuid
from dataclasses import dataclass

LINEAGE_ORIGINS = ("HANDLER", "WORKER", "REFRESH_GATE", "ENSURE_FOR_STEP", "LINEAGE_CONFLICT")
LINEAGE_INDEX = "uq_approvals_lineage_successor"


@dataclass(frozen=True)
class GateLineage:
    """What the ordinary `ensure_for_step` path needs to link its gate: the verified approval it follows and its lineage root."""
    supersedes_approval_id: uuid.UUID
    lineage_root_id: uuid.UUID


def lineage_keys(previous, project_epoch: int | None, origin: str) -> dict:
    """The four lineage keys of a gate that follows `previous` (an Approval). The root is the previous gate's root or itself."""
    assert origin in LINEAGE_ORIGINS
    root = (previous.target or {}).get("lineage_root_id") or str(previous.id)
    return {"supersedes_approval_id": str(previous.id), "lineage_root_id": str(root),
            "lineage_dirty_epoch": project_epoch, "lineage_origin": origin}


def lineage_keys_from(lineage: GateLineage, project_epoch: int | None, origin: str = "ENSURE_FOR_STEP") -> dict:
    return {"supersedes_approval_id": str(lineage.supersedes_approval_id), "lineage_root_id": str(lineage.lineage_root_id),
            "lineage_dirty_epoch": project_epoch, "lineage_origin": origin}


def is_lineage_violation(exc: BaseException) -> bool:
    """True only for a unique violation (SQLSTATE 23505) of exactly `uq_approvals_lineage_successor`. The structure of the driver
    exception is version-specific (SQLAlchemy 2.1 + asyncpg 0.32: `exc.orig.sqlstate` and `exc.orig.__cause__.constraint_name`),
    so anything not exactly as verified is NOT treated as the lineage race and the original error propagates."""
    orig = getattr(exc, "orig", None)
    if orig is None or getattr(orig, "sqlstate", None) != "23505":
        return False
    cause = getattr(orig, "__cause__", None)
    return (cause is not None and getattr(cause, "sqlstate", None) == "23505"
            and getattr(cause, "constraint_name", None) == LINEAGE_INDEX)
