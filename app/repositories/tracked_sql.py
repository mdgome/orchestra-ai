"""The canonical SQL text of the tracked-execution classification (design 7.4, 'Single ownership of the SQL text').

Text only, no queries: the model's partial-index predicates and the repository queries import these constants, and migration
0005 freezes the same text as literals (a test compares the frozen text, the model text and these constants by string
equality). Every comparison literal is written out, never a bind parameter and never SQLAlchemy's `astext`: an index predicate
cannot contain a bind. `<RA_VALID>` and `<RESOLVED_VALID>` are rendered from RISK_ACCEPTANCE_SCHEMA and the key list of
`<TRACKED_ISH>` from TRACKED_ONLY_KEYS (policies.py).
"""
from app.validation import policies as P

# The order of the key array of <TRACKED_ISH> is part of the frozen text; the set itself is TRACKED_ONLY_KEYS.
_TRACKED_ONLY_KEYS_SQL_ORDER = ("v", "runner_process", "checkpoint_step_key", "proof", "disposition", "reason_code",
                                "risk_acceptance", "anomaly_original", "anomaly_settlement")
assert set(_TRACKED_ONLY_KEYS_SQL_ORDER) == P.TRACKED_ONLY_KEYS and len(_TRACKED_ONLY_KEYS_SQL_ORDER) == len(P.TRACKED_ONLY_KEYS)


def _array(values) -> str:
    return "ARRAY[" + ",".join(f"'{v}'" for v in values) + "]"


def _literal(text: str) -> str:
    assert "'" not in text
    return f"'{text}'"


# The canonical fragment `(pid::text)::jsonb` is written literally inside RA_VALID_SQL and is not rendered separately.
PID_JSONB_SQL = "(pid::text)::jsonb"

PROJECT_SCOPE_SQL = "task_id IN (SELECT id FROM tasks WHERE project_id = :project_id)"

TRACKED_ISH_SQL = (
    "jsonb_typeof(detail) = 'object'\n"
    "AND ( jsonb_exists(detail, 'tracked')\n"
    "      OR jsonb_exists_any(detail, " + _array(_TRACKED_ONLY_KEYS_SQL_ORDER) + ") )"
)

U_SQL = (
    "(detail -> 'tracked') = 'true'::jsonb\n"
    "AND (detail ->> 'proof') IS NULL AND (detail ->> 'disposition') IS NULL"
)

_RA_VALID_TEMPLATE = (
    "jsonb_typeof(detail -> 'reason_code') = 'string' AND (detail ->> 'reason_code') = '@DISPOSITION@'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance') = 'object'\n"
    "AND (detail -> 'risk_acceptance') ?& @REQUIRED@\n"
    "AND jsonb_typeof(@R@ -> 'approval_id')  = 'string' AND (@R@ ->> 'approval_id')  ~ @UUID_RE@\n"
    "AND jsonb_typeof(@R@ -> 'execution_id') = 'string' AND (@R@ ->> 'execution_id') ~ @UUID_RE@ AND (@R@ ->> 'execution_id') = id::text\n"
    "AND jsonb_typeof(@R@ -> 'pid')          = 'number' AND (@R@ -> 'pid') = (pid::text)::jsonb AND pid BETWEEN 1 AND 2147483647\n"
    "AND jsonb_typeof(@R@ -> 'create_time')  = 'number' AND (@R@ -> 'create_time') >= '0'::jsonb\n"
    "AND jsonb_typeof(@R@ -> 'last_verdict') = 'string' AND (@R@ ->> 'last_verdict') = 'UNCLEAR'\n"
    "AND jsonb_typeof(@R@ -> 'requested_at') = 'string' AND (@R@ ->> 'requested_at') ~ @TS_RE@\n"
    "AND jsonb_typeof(@R@ -> 'rationale')    = 'string' AND char_length(@R@ ->> 'rationale') BETWEEN 1 AND @RATIONALE_MAX@\n"
    "    AND (@R@ ->> 'rationale') ~ '[^ \\t\\n]' AND (@R@ ->> 'rationale') !~ @RATIONALE_FORBIDDEN@\n"
    "AND jsonb_typeof(@R@ -> 'reverify_result') = 'string'\n"
    "    AND (@R@ ->> 'reverify_result') IN (@REVERIFY@)\n"
    "AND jsonb_typeof(@R@ -> 'applied_at')   = 'string' AND (@R@ ->> 'applied_at')   ~ @TS_RE@\n"
    "AND ( NOT ((detail -> 'risk_acceptance') ? 'requested_by')\n"
    "      OR ( jsonb_typeof(@R@ -> 'requested_by') = 'string' AND (@R@ ->> 'requested_by') ~ @REQUESTED_BY_RE@ ) )"
)


def _render_ra_valid() -> str:
    fields = {f.key: f for f in P.RISK_ACCEPTANCE_SCHEMA}
    required = tuple(f.key for f in P.RISK_ACCEPTANCE_SCHEMA if f.required)
    text = _RA_VALID_TEMPLATE
    for token, value in (
            ("@DISPOSITION@", P.DISPOSITION_KIND_RISK_ACCEPTED),
            ("@REQUIRED@", _array(required)),
            ("@UUID_RE@", _literal(P.RISK_ACCEPTANCE_UUID_RE)),
            ("@TS_RE@", _literal(P.RISK_ACCEPTANCE_TS_RE)),
            ("@RATIONALE_MAX@", str(P.PROCESS_RISK_RATIONALE_MAX_LENGTH)),
            ("@RATIONALE_FORBIDDEN@", _literal(P.RISK_ACCEPTANCE_RATIONALE_FORBIDDEN)),
            ("@REVERIFY@", ",".join(f"'{v}'" for v in fields["reverify_result"].allowed)),
            ("@REQUESTED_BY_RE@", _literal(P.RISK_ACCEPTANCE_REQUESTED_BY_RE)),
            ("@R@", "detail -> 'risk_acceptance'")):
        text = text.replace(token, value)
    return text


RA_VALID_SQL = _render_ra_valid()

RESOLVED_VALID_SQL = (
    "(detail -> 'tracked') = 'true'::jsonb\n"
    "AND jsonb_typeof(detail -> 'v') = 'number' AND (detail -> 'v') = '1'::jsonb\n"
    "AND finished_at IS NOT NULL\n"
    "AND (\n"
    "  ( (detail ->> 'proof') IN ('JOB_EMPTY','JOB_KILL_ON_CLOSE_ASSUMED','NEVER_RAN')\n"
    "    AND (detail ->> 'disposition') IS NULL )\n"
    "  OR\n"
    "  ( (detail ->> 'proof') IS NULL\n"
    "    AND (detail ->> 'disposition') = 'OPERATOR_RISK_ACCEPTED'\n"
    "    AND " + RA_VALID_SQL + " )\n"
    ")"
)

# Membership of A: a tracked row that is neither normal unresolved nor properly resolved. `IS NOT TRUE`, never `NOT`: a
# missing key makes a branch NULL and NOT NULL is NULL, which would silently hide the row.
ANOMALY_SQL = "(" + TRACKED_ISH_SQL + ") AND ((" + U_SQL + ") IS NOT TRUE) AND ((" + RESOLVED_VALID_SQL + ") IS NOT TRUE)"

# Evaluated on rows of A only; the first matching WHEN wins.
ANOMALY_KIND_SQL = (
    "CASE\n"
    "  WHEN ((detail ->> 'proof') IS NOT NULL OR (detail ->> 'disposition') IS NOT NULL)\n"
    "       AND (jsonb_typeof(detail -> 'v') = 'number' AND (detail -> 'v') = '1'::jsonb) IS NOT TRUE\n"
    "    THEN 'UNKNOWN_SCHEMA_VERSION'\n"
    "  WHEN (detail ->> 'proof') IS NOT NULL AND (detail ->> 'disposition') IS NOT NULL\n"
    "    THEN 'CONTRADICTORY_PROOF_DISPOSITION'\n"
    "  WHEN (detail ->> 'proof') IS NOT NULL\n"
    "       AND (jsonb_typeof(detail -> 'proof') = 'string'\n"
    "            AND (detail ->> 'proof') IN ('JOB_EMPTY','JOB_KILL_ON_CLOSE_ASSUMED','NEVER_RAN')) IS NOT TRUE\n"
    "    THEN 'INVALID_PROOF'\n"
    "  WHEN (detail ->> 'disposition') IS NOT NULL\n"
    "       AND (jsonb_typeof(detail -> 'disposition') = 'string'\n"
    "            AND (detail ->> 'disposition') = 'OPERATOR_RISK_ACCEPTED') IS NOT TRUE\n"
    "    THEN 'INVALID_DISPOSITION_KIND'\n"
    "  WHEN (detail ->> 'disposition') = 'OPERATOR_RISK_ACCEPTED' AND (" + RA_VALID_SQL + ") IS NOT TRUE\n"
    "    THEN 'INCOMPLETE_RISK_ACCEPTANCE'\n"
    "  ELSE 'OTHER_SCHEMA_INVALID'\n"
    "END"
)

# --- the project-wide queries (design 7.4): `executions` is the ONLY outer FROM table, no JOIN of tasks and executions, no
# OFFSET. The entry point appends `LIMIT :bounded_limit` OUTSIDE these canonical blocks. `<PROJECT_U_QUERY>`, `<PROJECT_A_QUERY>`.
TRACKED_ROW_COLUMNS_SQL = "id, task_id, pid, process_create_time, finished_at, detail::text"

PROJECT_U_QUERY_SQL = (
    "SELECT " + TRACKED_ROW_COLUMNS_SQL + "\n"
    "FROM executions\n"
    "WHERE " + PROJECT_SCOPE_SQL + " AND (" + U_SQL + ")\n"
    "ORDER BY id"
)

PROJECT_A_QUERY_SQL = (
    "SELECT " + TRACKED_ROW_COLUMNS_SQL + "\n"
    "FROM executions\n"
    "WHERE " + PROJECT_SCOPE_SQL + " AND (" + ANOMALY_SQL + ")\n"
    "ORDER BY id"
)

# Task-local view only (the report, the diagnostics detail of one Task): never read by a CLEAN decision.
TASK_U_QUERY_SQL = (
    "SELECT " + TRACKED_ROW_COLUMNS_SQL + "\n"
    "FROM executions\n"
    "WHERE task_id = :task_id AND (" + U_SQL + ")\n"
    "ORDER BY id"
)

TRACKED_ROW_BY_ID_SQL = "SELECT " + TRACKED_ROW_COLUMNS_SQL + " FROM executions WHERE id = :id"

# The ordered row-lock statement of 7.5 item 15 (the only `FOR UPDATE` on executions besides the risk-target lock).
LOCK_EXECUTIONS_ORDERED_SQL = "SELECT id FROM executions WHERE id = ANY(:ids) ORDER BY id FOR UPDATE"

# --- keyset page blocks (design 7.4 'Candidate scan'): the first page has no `AND id > :last_seen_id`; `LIMIT :page_size`
# is part of each page block (never `:bounded_limit`). One block per consumer, none borrows another's.
_AFTER = " AND id > :last_seen_id"


def _page(where: str, after: bool) -> str:
    return ("SELECT " + TRACKED_ROW_COLUMNS_SQL + "\nFROM executions\nWHERE " + where + (_AFTER if after else "") +
            "\nORDER BY id\nLIMIT :page_size")


JUDGEABLE_SQL = (
    "EXISTS (SELECT 1 FROM tasks t WHERE t.id = executions.task_id AND t.status IN ('COMPLETED','FAILED','CANCELLED'))\n"
    "OR run_id IS NULL\n"
    "OR EXISTS (SELECT 1 FROM task_runs r WHERE r.id = executions.run_id\n"
    "           AND (r.status IN ('FINISHED','ABANDONED') OR (r.status = 'RUNNING' AND r.lease_expires_at <= now())))"
)

_SWEEP_WHERE = "( ((" + U_SQL + ") AND (" + JUDGEABLE_SQL + ")) OR (" + ANOMALY_SQL + ") )"
_RISK_OWN_WHERE = PROJECT_SCOPE_SQL + " AND task_id = :task_id AND (" + U_SQL + ")"
_RISK_REST_WHERE = PROJECT_SCOPE_SQL + " AND task_id <> :task_id AND (" + U_SQL + ")"
_PROJECT_U_WHERE = PROJECT_SCOPE_SQL + " AND (" + U_SQL + ")"

SWEEP_PAGE_FIRST_SQL, SWEEP_PAGE_NEXT_SQL = _page(_SWEEP_WHERE, False), _page(_SWEEP_WHERE, True)
RISK_PAGE_OWN_FIRST_SQL, RISK_PAGE_OWN_NEXT_SQL = _page(_RISK_OWN_WHERE, False), _page(_RISK_OWN_WHERE, True)
RISK_PAGE_REST_FIRST_SQL, RISK_PAGE_REST_NEXT_SQL = _page(_RISK_REST_WHERE, False), _page(_RISK_REST_WHERE, True)
PROJECT_U_PAGE_FIRST_SQL, PROJECT_U_PAGE_NEXT_SQL = _page(_PROJECT_U_WHERE, False), _page(_PROJECT_U_WHERE, True)


# <RISK_TARGET_LOCK> (design 7.4, CM-4 revision 22): the final risk snapshot, locked by ONE ordered statement; the rows still in U
# come back. The only `FOR UPDATE` on executions besides LOCK_EXECUTIONS_ORDERED_SQL; used by TaskRepository.lock_risk_targets only.
RISK_TARGET_LOCK_SQL = (
    "SELECT " + TRACKED_ROW_COLUMNS_SQL + "\nFROM executions\nWHERE " + PROJECT_SCOPE_SQL + " AND (" + U_SQL +
    ") AND id = ANY(:target_ids)\nORDER BY id\nFOR UPDATE")
