"""The Python classifier of a tracked `executions` row (design 6.1, 7.4): the mirror of the SQL predicates of `tracked_sql`.

Pure, no I/O. Input is exactly what the project-wide queries select: id, pid, finished_at and `detail::text` (decoded here
and nowhere else, strictly: numbers become Decimal, `NaN`/`Infinity` are refused). The branches follow the SQL `CASE` of
`ANOMALY_KIND_SQL` (first match wins); a parity test runs both on the same fixture matrix.

SQL null semantics: `detail ->> 'x' IS NULL` is true for an absent key AND for a JSON null, so "has a value" below means
"the key is present and not None".
"""
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.validation import policies as P

REQUIRED_KEYS = tuple(f.key for f in P.RISK_ACCEPTANCE_SCHEMA if f.required)
_FIELDS = {f.key: f for f in P.RISK_ACCEPTANCE_SCHEMA}


class RowClass(StrEnum):
    LEGACY = "LEGACY"                      # no tracked marker, no tracked-only key: outside every B2 rule
    UNRESOLVED = "UNRESOLVED"              # set U
    RESOLVED_PROVEN = "RESOLVED_PROVEN"
    RESOLVED_RISK = "RESOLVED_RISK"
    ANOMALY = "ANOMALY"                    # set A


@dataclass(frozen=True)
class Classification:
    row_class: RowClass
    anomaly_kind: str | None = None
    sub_reason: str | None = None          # fixed codes only, never a value read from the row

    @property
    def is_unresolved(self) -> bool:
        return self.row_class is RowClass.UNRESOLVED

    @property
    def is_anomaly(self) -> bool:
        return self.row_class is RowClass.ANOMALY


def _refuse_constant(name: str):
    raise ValueError(f"non-finite JSON constant {name}")


def decode_detail(text: str):
    """Strict decode of `detail::text`: Decimal numbers, no NaN/Infinity. Raises ValueError on any problem."""
    return json.loads(text, parse_float=Decimal, parse_int=Decimal, parse_constant=_refuse_constant)


def _is_number(value) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _has(d: dict, key: str) -> bool:
    return d.get(key) is not None


def _is_tracked_ish(d: dict) -> bool:
    return "tracked" in d or any(k in d for k in P.TRACKED_ONLY_KEYS)


def _fullmatch(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern, value) is not None


def risk_acceptance_pid_valid(pid: int | None) -> bool:
    """The ONE rule of the identity requirement of a `risk_acceptance` record: the row must carry a valid pid. Shared by the
    classifier (rejection `identity:pid`) and the `accept_unproven_risk` eligibility (no record can be written without it)."""
    return pid is not None and 1 <= pid <= 2147483647


def risk_acceptance_problem(d: dict, row_id: uuid.UUID, pid: int | None) -> str | None:
    """None when `detail.risk_acceptance` (and reason_code) satisfy the validity rule of 7.5 item 7; otherwise the first failing
    sub-reason. Own-row relations only (execution_id, pid): the cross-table ones are write-time checks."""
    r = d.get("risk_acceptance")
    if not isinstance(r, dict):
        return "risk_acceptance_not_object"
    for key in REQUIRED_KEYS:
        if key not in r:
            return f"key_missing:{key}"
    for key in REQUIRED_KEYS:
        if r[key] is None:
            return f"key_null:{key}"
    for key in REQUIRED_KEYS:
        field = _FIELDS[key]
        value = r[key]
        ok = _is_number(value) if field.json_type == "number" else isinstance(value, str)
        if not ok:
            return f"type:{key}"
    if not _fullmatch(P.RISK_ACCEPTANCE_UUID_RE, r["approval_id"]):
        return "format:approval_id"
    if not _fullmatch(P.RISK_ACCEPTANCE_UUID_RE, r["execution_id"]):
        return "format:execution_id"
    if r["execution_id"] != str(row_id):
        return "identity:execution_id"
    if not risk_acceptance_pid_valid(pid) or r["pid"] != Decimal(pid):
        return "identity:pid"
    if r["create_time"] < 0:
        return "format:create_time"
    if r["last_verdict"] != "UNCLEAR":
        return "format:last_verdict"
    if not _fullmatch(P.RISK_ACCEPTANCE_TS_RE, r["requested_at"]):
        return "format:requested_at"
    rationale = r["rationale"]
    if (not 1 <= len(rationale) <= P.PROCESS_RISK_RATIONALE_MAX_LENGTH or re.search(r"[^ \t\n]", rationale) is None
            or re.search(P.RISK_ACCEPTANCE_RATIONALE_FORBIDDEN, rationale) is not None):
        return "format:rationale"
    if r["reverify_result"] not in _FIELDS["reverify_result"].allowed:
        return "format:reverify_result"
    if not _fullmatch(P.RISK_ACCEPTANCE_TS_RE, r["applied_at"]):
        return "format:applied_at"
    if "requested_by" in r:
        value = r["requested_by"]
        if not isinstance(value, str):
            return "type:requested_by"
        if not _fullmatch(P.RISK_ACCEPTANCE_REQUESTED_BY_RE, value):
            return "format:requested_by"
    reason = d.get("reason_code")
    if not isinstance(reason, str) or reason != P.DISPOSITION_KIND_RISK_ACCEPTED:
        return "reason_code"
    return None


def _kind6_sub_reason(d: dict, finished_at: datetime | None) -> str:
    if "tracked" in d and d["tracked"] is not True:
        return "tracked_type"
    if "tracked" not in d:
        return "tracked_missing"
    if finished_at is None and (_has(d, "proof") or _has(d, "disposition")):
        return "finished_at_missing"
    return "other"


def _anomaly(d: dict, row_id: uuid.UUID, pid: int | None, finished_at: datetime | None) -> Classification:
    """The `ANOMALY_KIND_SQL` CASE, branch by branch, evaluated on a row of A."""
    has_proof, has_disposition = _has(d, "proof"), _has(d, "disposition")
    v = d.get("v")
    version_ok = _is_number(v) and v == 1
    if (has_proof or has_disposition) and not version_ok:
        return Classification(RowClass.ANOMALY, "UNKNOWN_SCHEMA_VERSION", _kind6_sub_reason_if_marker(d))
    if has_proof and has_disposition:
        return Classification(RowClass.ANOMALY, "CONTRADICTORY_PROOF_DISPOSITION", _kind6_sub_reason_if_marker(d))
    if has_proof and not (isinstance(d["proof"], str) and d["proof"] in P.POSITIVE_PROOFS):
        return Classification(RowClass.ANOMALY, "INVALID_PROOF", _kind6_sub_reason_if_marker(d))
    if has_disposition and not (isinstance(d["disposition"], str) and d["disposition"] == P.DISPOSITION_KIND_RISK_ACCEPTED):
        return Classification(RowClass.ANOMALY, "INVALID_DISPOSITION_KIND", _kind6_sub_reason_if_marker(d))
    if has_disposition and d["disposition"] == P.DISPOSITION_KIND_RISK_ACCEPTED:
        problem = risk_acceptance_problem(d, row_id, pid)
        if problem is not None:
            return Classification(RowClass.ANOMALY, "INCOMPLETE_RISK_ACCEPTANCE", problem)
    return Classification(RowClass.ANOMALY, "OTHER_SCHEMA_INVALID", _kind6_sub_reason(d, finished_at))


def _kind6_sub_reason_if_marker(d: dict) -> str | None:
    """The marker defect stays visible for kinds 1-5 (the Python-only `anomaly_detail.marker`); a fixed code or None."""
    if "tracked" in d and d["tracked"] is not True:
        return "tracked_type"
    if "tracked" not in d:
        return "tracked_missing"
    return None


def classify_row(row_id: uuid.UUID, pid: int | None, finished_at: datetime | None, detail_text: str | None) -> Classification:
    """One row, as selected by the project-wide queries. A document that cannot be decoded is an anomaly (`decode_error`)."""
    if detail_text is None:
        return Classification(RowClass.LEGACY)
    try:
        d = decode_detail(detail_text)
    except (ValueError, RecursionError):
        return Classification(RowClass.ANOMALY, "OTHER_SCHEMA_INVALID", "decode_error")
    if not isinstance(d, dict):
        return Classification(RowClass.LEGACY)           # SQL NULL, JSON null, array, scalar: the legacy branch (6.1 (a))
    if not _is_tracked_ish(d):
        return Classification(RowClass.LEGACY)
    has_proof, has_disposition = _has(d, "proof"), _has(d, "disposition")
    if d.get("tracked") is True and not has_proof and not has_disposition:
        return Classification(RowClass.UNRESOLVED)
    if d.get("tracked") is True and _is_number(d.get("v")) and d["v"] == 1 and finished_at is not None:
        if has_proof and not has_disposition and isinstance(d["proof"], str) and d["proof"] in P.POSITIVE_PROOFS:
            return Classification(RowClass.RESOLVED_PROVEN)
        if (not has_proof and has_disposition and d["disposition"] == P.DISPOSITION_KIND_RISK_ACCEPTED
                and risk_acceptance_problem(d, row_id, pid) is None):
            return Classification(RowClass.RESOLVED_RISK)
    return _anomaly(d, row_id, pid, finished_at)
