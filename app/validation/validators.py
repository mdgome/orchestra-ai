"""Feature-scoped validators. Small functions; policy values come from policies.py."""
import math
import os
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from app.validation import policies as P
from app.validation.exceptions import Conflict, FailClosed, ValidationFailed


def is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def validate_recovery_timeouts(statement: float, operation: float, shutdown_wait: float) -> None:
    """The single place that checks the recovery timeouts; Settings and Runner.__init__ both call it."""
    for name, value in (("statement", statement), ("operation", operation), ("shutdown_wait", shutdown_wait)):
        if not math.isfinite(value) or not value > 0:         # inf would silently remove the bound, nan compares False
            raise ValueError(f"recovery {name} timeout must be a finite number greater than 0 (got {value!r})")
    if not statement < operation:
        raise ValueError(f"recovery statement timeout ({statement}s) must be smaller than the operation timeout "
                         f"({operation}s)")
    if int(statement * 1000) < 1:
        raise ValueError(f"recovery statement timeout {statement}s is below one millisecond")
    if int(statement * 1000) > P.MAX_PG_TIMEOUT_MS:
        raise ValueError(f"recovery statement timeout {statement}s exceeds PostgreSQL's millisecond range")


def validate_response_limits() -> None:
    """Startup check (read at call time so a test can change a limit): a bound that cannot hold even one minimal
    answer would make an endpoint unable to return a legitimate page, so the application refuses to start instead."""
    if P.ARTIFACT_LIST_RESPONSE_MAX_BYTES < P.RESPONSE_ENVELOPE_MAX_OVERHEAD_BYTES + P.ARTIFACT_ITEM_MAX_BYTES:
        raise ValueError("ARTIFACT_LIST_RESPONSE_MAX_BYTES must hold the envelope plus one artifact of "
                         "ARTIFACT_ITEM_MAX_BYTES, otherwise a page could be empty although artifacts exist")
    if P.ARTIFACT_ITEM_MAX_BYTES < P.SANITIZED_JSON_MAX_BYTES + P.RESPONSE_ENVELOPE_MAX_OVERHEAD_BYTES:
        raise ValueError("ARTIFACT_ITEM_MAX_BYTES must hold a full SANITIZED_JSON_MAX_BYTES field plus the item's own fields")
    if P.CHANGES_RESPONSE_MAX_BYTES < P.CHANGES_TOTAL_DIFF_MAX_BYTES + P.RESPONSE_ENVELOPE_MAX_OVERHEAD_BYTES:
        raise ValueError("CHANGES_RESPONSE_MAX_BYTES must hold the largest allowed set of diff bodies plus the envelope")
    if P.REPORT_RESPONSE_MAX_BYTES < 2 * P.RESPONSE_ENVELOPE_MAX_OVERHEAD_BYTES:
        raise ValueError("REPORT_RESPONSE_MAX_BYTES is too small for the report's core fields")
    for name in ("SANITIZE_MAX_DEPTH", "SANITIZE_MAX_KEYS", "SANITIZE_MAX_ITEMS", "SANITIZE_MAX_STRING_CHARS",
                 "SANITIZE_MAX_NODES", "RESPONSE_SHRINK_MAX_ROUNDS"):
        if getattr(P, name) < 1:
            raise ValueError(f"{name} must be at least 1")


def validate_artifact_kind(kind: str | None, allowed: tuple[str, ...]) -> None:
    if kind is not None and kind not in allowed:
        raise ValidationFailed("unknown artifact kind", {"allowed": list(allowed)})


def validate_changes_path_filter(path: str | None) -> str | None:
    """A relative POSIX path (an exact file or a directory prefix): no absolute path, drive, `..`, empty segment or control
    character. It only selects among paths the measurement already found; it never opens anything."""
    if path is None or path == "":
        return None
    candidate = path.rstrip("/") if path != "/" else path
    bad = (not candidate or len(candidate) > P.MANIFEST_MAX_PATH_LEN or candidate.startswith("/") or "\\" in candidate
           or (len(candidate) > 1 and candidate[1] == ":")
           or any(ord(c) < 32 or ord(c) == 127 for c in candidate)
           or any(part in ("", ".", "..") for part in candidate.split("/")))
    if bad:
        raise ValidationFailed("path must be a relative path inside the project (no '..', absolute path or drive)",
                               {"path": path[:200]})
    return candidate


def _profile_text(value, field: str, max_len: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"profile {field} must be a string")
    text = value.strip()
    if len(text) > max_len:
        raise ValidationFailed(f"profile {field} is longer than {max_len} characters")
    if any((ord(c) < 32 and not (multiline and c in "\n\t")) or ord(c) == 127 for c in text):
        raise ValidationFailed(f"profile {field} contains control characters")
    return text


def _profile_list(value, field: str, max_items: int, max_len: int) -> list[str]:
    if not isinstance(value, list):
        raise ValidationFailed(f"profile {field} must be a list of strings")
    if len(value) > max_items:
        raise ValidationFailed(f"profile {field} has more than {max_items} items")
    out = [_profile_text(item, field, max_len) for item in value]
    if any(not item for item in out):
        raise ValidationFailed(f"profile {field} must not contain empty items")
    return out


def validate_profile(raw) -> dict:
    """The canonical content of a project profile: every key present, strings stripped, so one meaning has one hash.

    Unknown keys are rejected (not ignored): a profile can never carry permissions, tools, models, paths or an
    interpreter. `test_policy_ref` can only reference the project's own test policy."""
    if not isinstance(raw, dict):
        raise ValidationFailed("profile must be a JSON object")
    extra = set(raw) - P.PROFILE_KEYS
    if extra:
        raise ValidationFailed("unknown profile fields: a profile cannot carry permissions, tools, models, paths or "
                               "interpreters", {"rejected": sorted(extra), "allowed": sorted(P.PROFILE_KEYS)})
    version = raw.get("schema_version", P.PROFILE_SCHEMA_VERSION)
    if isinstance(version, bool) or version != P.PROFILE_SCHEMA_VERSION:
        raise ValidationFailed("unsupported profile schema_version", {"supported": P.PROFILE_SCHEMA_VERSION})
    if raw.get("test_policy_ref", P.PROFILE_TEST_POLICY_REF) != P.PROFILE_TEST_POLICY_REF:
        raise ValidationFailed("profile test_policy_ref can only be 'project' (the project's own test policy)")
    criteria_raw = raw.get("completion_criteria", [])
    if not isinstance(criteria_raw, list) or len(criteria_raw) > P.PROFILE_CRITERIA_MAX:
        raise ValidationFailed(f"profile completion_criteria must be a list of at most {P.PROFILE_CRITERIA_MAX} items")
    criteria, seen = [], set()
    for item in criteria_raw:
        if not isinstance(item, dict) or set(item) != {"id", "text"}:
            raise ValidationFailed("each completion criterion must be an object with exactly 'id' and 'text'")
        cid = _profile_text(item["id"], "criterion id", 32)
        if not re.fullmatch(P.PROFILE_CRITERION_ID_PATTERN, cid):
            raise ValidationFailed("invalid completion criterion id", {"id": cid, "pattern": P.PROFILE_CRITERION_ID_PATTERN})
        if cid in seen:
            raise ValidationFailed("duplicate completion criterion id", {"id": cid})
        seen.add(cid)
        text = _profile_text(item["text"], "criterion text", P.PROFILE_RULE_MAX)
        if not text:
            raise ValidationFailed("a completion criterion needs a text", {"id": cid})
        criteria.append({"id": cid, "text": text})
    return {
        "schema_version": P.PROFILE_SCHEMA_VERSION,
        "description": _profile_text(raw.get("description", ""), "description", P.PROFILE_DESCRIPTION_MAX, multiline=True),
        "languages": _profile_list(raw.get("languages", []), "languages", P.PROFILE_LABELS_MAX, P.PROFILE_LABEL_MAX),
        "frameworks": _profile_list(raw.get("frameworks", []), "frameworks", P.PROFILE_LABELS_MAX, P.PROFILE_LABEL_MAX),
        "architecture_rules": _profile_list(raw.get("architecture_rules", []), "architecture_rules",
                                            P.PROFILE_RULES_MAX, P.PROFILE_RULE_MAX),
        "coding_rules": _profile_list(raw.get("coding_rules", []), "coding_rules", P.PROFILE_RULES_MAX, P.PROFILE_RULE_MAX),
        "completion_criteria": criteria,
        "test_policy_ref": P.PROFILE_TEST_POLICY_REF,
    }


def validate_requirements(text: str) -> str:
    value = (text or "").strip()
    if not (P.REQUIREMENTS_MIN_LENGTH <= len(value) <= P.REQUIREMENTS_MAX_LENGTH):
        raise ValidationFailed(
            "requirements length out of range",
            {"min": P.REQUIREMENTS_MIN_LENGTH, "max": P.REQUIREMENTS_MAX_LENGTH, "actual": len(value)},
        )
    return value


def validate_message(role: str, content: str) -> tuple[str, str]:
    if role not in P.MESSAGE_ROLES:
        raise ValidationFailed("invalid message role", {"allowed": sorted(P.MESSAGE_ROLES)})
    value = (content or "").strip()
    if not value or len(value) > P.MESSAGE_MAX_LENGTH:
        raise ValidationFailed("message content length out of range", {"max": P.MESSAGE_MAX_LENGTH})
    return role, value


def validate_limits(overrides: dict | None) -> P.ExecutionLimits:
    base = P.ExecutionLimits()
    values = {k: getattr(base, k) for k in P.LIMIT_BOUNDS}
    for key, val in (overrides or {}).items():
        if key not in P.LIMIT_BOUNDS:
            raise ValidationFailed("unknown limit", {"limit": key})
        low, high = P.LIMIT_BOUNDS[key]
        if not isinstance(val, int) or isinstance(val, bool) or not (low <= val <= high):
            raise ValidationFailed("limit out of range", {"limit": key, "min": low, "max": high})
        values[key] = val
    return P.ExecutionLimits(**values)


def validate_junit_path(value) -> str:
    """A workspace-relative path or glob; never absolute, never leaving the workspace."""
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValidationFailed("test_policy.junit_path must be a relative path or glob")
    normal = value.strip().replace("\\", "/")
    parts = normal.split("/")
    if normal.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", normal) or ".." in parts or "" in parts:
        raise ValidationFailed("test_policy.junit_path must stay inside the workspace")
    return normal


def _validate_env_allow(names) -> list[str]:
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValidationFailed("test_policy.env_allow must be a list of variable names")
    bad = [n for n in names if n not in P.TEST_ENV_ALLOWED_NAMES]
    if bad:
        raise ValidationFailed("test_policy.env_allow may only name these variables (exact names, no patterns)",
                               {"allowed": sorted(P.TEST_ENV_ALLOWED_NAMES), "rejected": bad})
    return list(names)


def validate_test_policy(policy: dict | None) -> dict:
    """Two accepted shapes: a preset reference ({"preset": "maven"}, resolved to one executable per task later)
    and an explicit policy with commands. Acceptance criteria are never relaxed here."""
    policy = policy or P.DEFAULT_TEST_POLICY
    min_tests = int(policy.get("min_tests_executed", 1))
    if min_tests < 1:
        raise ValidationFailed("test_policy.min_tests_executed must be >= 1")
    timeout = int(policy.get("timeout_seconds", 600))
    if not (1 <= timeout <= P.MAX_CLI_TIMEOUT_SECONDS):
        raise ValidationFailed("test_policy.timeout_seconds out of range")
    if "preset" in policy:
        if policy["preset"] not in P.TEST_PRESETS:
            raise ValidationFailed("unknown test preset", {"allowed": sorted(P.TEST_PRESETS)})
        extra = set(policy) - {"preset", "min_tests_executed", "timeout_seconds"}
        if extra:
            raise ValidationFailed("a preset policy only accepts min_tests_executed and timeout_seconds",
                                   {"rejected": sorted(extra)})
        return {"preset": policy["preset"], "min_tests_executed": min_tests, "timeout_seconds": timeout}
    commands = policy.get("commands")
    if not commands or not all(isinstance(c, list) and c and all(isinstance(a, str) for a in c) for c in commands):
        raise ValidationFailed("test_policy.commands must be a non-empty list of argument lists")
    if any(c[0].lower().endswith(P.SHELL_SCRIPT_SUFFIXES) for c in commands):
        raise ValidationFailed("a custom test command may not be a .cmd/.bat script; use a preset, which resolves "
                               "the project's own wrapper safely")
    merged = {**P.DEFAULT_TEST_POLICY, **policy, "timeout_seconds": timeout}
    merged["junit_path"] = validate_junit_path(merged["junit_path"])
    if "env_allow" in policy:
        merged["env_allow"] = _validate_env_allow(policy["env_allow"])
    return merged


def validate_project_name(name: str) -> str:
    value = (name or "").strip()
    if not (P.PROJECT_NAME_MIN_LENGTH <= len(value) <= P.PROJECT_NAME_MAX_LENGTH):
        raise ValidationFailed("project name length out of range",
                               {"min": P.PROJECT_NAME_MIN_LENGTH, "max": P.PROJECT_NAME_MAX_LENGTH})
    return value


def validate_project_status(status: str) -> str:
    if status not in P.PROJECT_STATUSES:
        raise ValidationFailed("invalid project status", {"allowed": sorted(P.PROJECT_STATUSES)})
    return status


def _same_or_under(path: Path, parent: Path) -> bool:
    key, base = os.path.normcase(str(path)), os.path.normcase(str(parent))
    return key == base or key.startswith(base.rstrip("\\/") + os.sep)


def workspace_key(path: Path) -> str:
    return os.path.normcase(str(path))


def validate_project_path(path: str, *, protected: tuple[Path, ...] = ()) -> Path:
    """An existing, absolute, link-free, accessible directory that is not a system/protected location.

    `protected` are orchestrator-owned locations: a project may be neither inside nor above any of them."""
    if not isinstance(path, str) or not path.strip():
        raise ValidationFailed("workspace_path is required")
    candidate = Path(path.strip())
    if not candidate.is_absolute():
        raise ValidationFailed("workspace_path must be an absolute path")
    probe = candidate
    while probe != probe.parent:
        if probe.exists() and is_link_or_junction(probe):
            raise FailClosed("workspace path traverses a symlink or junction")
        probe = probe.parent
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationFailed("workspace path does not exist or is not accessible") from exc
    if not resolved.is_dir():
        raise ValidationFailed("workspace path must be a directory")
    if resolved == Path(resolved.anchor):
        raise ValidationFailed("a drive or filesystem root cannot be a project workspace")
    if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
        raise ValidationFailed("workspace path is not readable and writable by the orchestrator")
    for env_name in P.PROJECT_FORBIDDEN_ENV_DIRS:
        value = os.environ.get(env_name)
        if value and _same_or_under(resolved, Path(value)):
            raise ValidationFailed("system directories cannot be a project workspace")
    if workspace_key(resolved) == workspace_key(Path.home()):
        raise ValidationFailed("the user profile root cannot be a project workspace")
    for guarded in protected:
        if _same_or_under(resolved, guarded) or _same_or_under(guarded, resolved):
            raise ValidationFailed("workspace path overlaps an orchestrator-managed location")
    return resolved


def validate_no_project_overlap(key: str, existing_keys: list[str]) -> None:
    """Duplicate registration and parent/child nesting between projects are both rejected."""
    for other in existing_keys:
        if key == other:
            raise Conflict("this workspace is already registered as a project")
        if key.startswith(other.rstrip("\\/") + os.sep) or other.startswith(key.rstrip("\\/") + os.sep):
            raise Conflict("workspace overlaps (is nested with) another registered project")


def validate_repository_url(url: str) -> str:
    """https/ssh only, no embedded credentials. Returns scheme://host/path (never userinfo)."""
    value = (url or "").strip()
    if not value or len(value) > P.PROJECT_REPOSITORY_URL_MAX_LENGTH:
        raise ValidationFailed("repository_url length out of range")
    scp_like = re.fullmatch(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9._/~-]+)", value)
    if scp_like:
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in P.PROJECT_REPOSITORY_URL_SCHEMES or not parsed.hostname:
        raise ValidationFailed("repository_url must be an https:// or ssh:// URL, or git@host:path",
                               {"allowed_schemes": sorted(P.PROJECT_REPOSITORY_URL_SCHEMES)})
    if parsed.password is not None or (parsed.username and parsed.scheme != "ssh") or parsed.query or parsed.fragment:
        raise ValidationFailed("repository_url must not embed credentials; use a credential helper or ssh-agent")
    if parsed.scheme == "ssh" and parsed.username not in (None, "git"):
        raise ValidationFailed("repository_url must not embed credentials; use a credential helper or ssh-agent")
    if value.startswith("-") or any(c in value for c in " \t\r\n\0"):
        raise ValidationFailed("repository_url contains invalid characters")
    host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    user = f"{parsed.username}@" if parsed.username else ""
    return f"{parsed.scheme}://{user}{host}{parsed.path}"


def validate_decision(decision: str, idempotency_key: str | None, selected_option: str | None) -> None:
    if decision not in P.APPROVAL_DECISIONS:
        raise ValidationFailed("invalid decision", {"allowed": sorted(P.APPROVAL_DECISIONS)})
    if not idempotency_key or not re.fullmatch(r"[A-Za-z0-9._:-]+", idempotency_key) \
            or len(idempotency_key) > P.IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ValidationFailed("Idempotency-Key header is required (alphanumerics . _ : -)")
    if selected_option is not None and not (1 <= len(selected_option) <= 64):
        raise ValidationFailed("selected_option length out of range")


RISK_OPTIONS = ("accept_unproven_risk", "recheck_processes")
_SURROGATE = re.compile("[\ud800-\udfff]")


def _storage_safe(value: str, field: str) -> None:
    """U+0000 (PostgreSQL text cannot hold it) and lone surrogates (not encodable as UTF-8) are never accepted."""
    if "\x00" in value or _SURROGATE.search(value):
        raise ValidationFailed(f"{field} contains a character that cannot be stored", {"field": field})


def validate_rationale(value: str | None, option: str | None) -> str | None:
    """The stored form of a decision rationale. Storage safety applies to every decision; the risk options also require a
    non-blank value of at most PROCESS_RISK_RATIONALE_MAX_LENGTH code points after trimming and NFC, without a control character
    other than newline and tab. Returns the value to store (trimmed and NFC for a risk option, unchanged otherwise)."""
    if value is not None:
        _storage_safe(value, "rationale")
    if option not in RISK_OPTIONS:
        return value
    if value is None:
        raise ValidationFailed("rationale is required for this option", {"field": "rationale"})
    stored = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(stored) <= P.PROCESS_RISK_RATIONALE_MAX_LENGTH:
        raise ValidationFailed("rationale must be 1 to %d characters" % P.PROCESS_RISK_RATIONALE_MAX_LENGTH,
                               {"field": "rationale"})
    if re.search(P.RISK_ACCEPTANCE_RATIONALE_FORBIDDEN, stored):
        raise ValidationFailed("rationale contains a control character", {"field": "rationale"})
    return stored


def validate_requested_by(value: str | None, option: str | None) -> str | None:
    """Optional claimed identity, accepted only with the two risk options. An empty string is rejected, never read as absent."""
    if value is None:
        return None
    _storage_safe(value, "requested_by")
    if option not in RISK_OPTIONS:
        raise ValidationFailed("requested_by is only accepted with a risk option", {"field": "requested_by"})
    stored = value.strip()
    if not 1 <= len(stored) <= P.REQUESTED_BY_MAX_LENGTH or re.fullmatch(P.REQUESTED_BY_PATTERN, stored) is None:
        raise ValidationFailed("requested_by is invalid", {"field": "requested_by"})
    return stored


def validate_uuid(value: str, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ValidationFailed(f"{field} must be a UUID") from exc


def validate_cli_timeout(seconds: int) -> int:
    if not (1 <= seconds <= P.MAX_CLI_TIMEOUT_SECONDS):
        raise ValidationFailed("timeout out of range", {"max": P.MAX_CLI_TIMEOUT_SECONDS})
    return seconds


def validate_session_binding(
    *, role: str, provider: str, task_conversation_id, session_conversation_id=None,
    existing_provider: str | None = None,
) -> None:
    """agent_sessions relationship rules (Session Relationship + provider/role pairing)."""
    if role not in P.SESSION_ROLES:
        raise ValidationFailed("invalid agent role", {"allowed": sorted(P.SESSION_ROLES)})
    if provider not in P.SESSION_PROVIDERS:
        raise ValidationFailed("invalid provider", {"allowed": sorted(P.SESSION_PROVIDERS)})
    if P.ROLE_PROVIDER[role] != provider:
        raise ValidationFailed("provider does not match role", {"role": role, "provider": provider})
    if session_conversation_id is not None and session_conversation_id != task_conversation_id:
        raise FailClosed("task and agent session belong to different conversations")
    if existing_provider is not None and existing_provider != provider:
        raise FailClosed("provider changed; native session must not be reused")


def validate_not_expired(expires_at: datetime | None, now: datetime, what: str) -> None:
    if expires_at is not None and expires_at <= now:
        raise FailClosed(f"{what} expired")


# --- B2 process lifecycle hardening: the central constant validators (design 9.2, 9.3, R31-9) ----------------------------
# One error envelope for every violation: a ValueError (the convention of validate_response_limits and
# validate_recovery_timeouts). Numbers are checked in the order of the design: reject a bool, accept only int or float,
# compare ranges (an int of any size compares safely), apply isfinite only to a float. Constants are read from the policies
# module at call time so a test can change one. Nothing here reads an environment variable or a setting.
_PROCESS_MAX_FIXED_SECONDS = 86400.0       # upper bound of several fixed code-policy ranges (design choice, 9.2)


def ConfigValueError(message: str, reason: str) -> ValueError:  # noqa: N802 - used like an exception class: `raise ConfigValueError(..)`
    """A plain `ValueError` (the one envelope of R31-9, `type(error) is ValueError`) that carries WHY the constant is invalid as
    `error.reason`, one of the fixed `SWEEPER_CONFIG_INVALID_REASONS` of design 9.4: TYPE, NON_FINITE, OUT_OF_RANGE, RELATION,
    OVERFLOW, DATETIME_LIMIT. The reason is never free text, a constant name or a value; the message (which may name a constant)
    stays internal and is never exposed."""
    error = ValueError(message)
    error.reason = reason  # type: ignore[attr-defined]
    return error


def _num(name: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigValueError(f"{name} must be a number that is not a bool (got {type(value).__name__})", "TYPE")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigValueError(f"{name} must be finite (got {value!r})", "NON_FINITE")


def _positive(name: str, value) -> None:
    _num(name, value)
    if not value > 0:
        raise ConfigValueError(f"{name} must be greater than 0 (got {value!r})", "OUT_OF_RANGE")


def _count(name: str, value, minimum: int, maximum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValueError(f"{name} must be an int that is not a bool (got {type(value).__name__})", "TYPE")
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigValueError(
            f"{name} must be in {minimum}..{maximum if maximum is not None else 'unbounded'} (got {value!r})", "OUT_OF_RANGE")


def _bounds(name: str, value, minimum, maximum) -> None:
    _num(name, value)
    if value < minimum or value > maximum:
        raise ConfigValueError(f"{name} must be in [{minimum}, {maximum}] (got {value!r})", "OUT_OF_RANGE")


def _relation(ok: bool, text: str) -> None:
    if not ok:
        raise ConfigValueError(f"violated relation: {text}", "RELATION")


def _pg_ms(name: str, seconds) -> None:
    """A transaction-local statement or lock timeout: a number of milliseconds in [1, MAX_PG_TIMEOUT_MS]."""
    _positive(name, seconds)
    if not 1 <= seconds * 1000 <= P.MAX_PG_TIMEOUT_MS:
        raise ConfigValueError(f"{name} must be 1 ms to {P.MAX_PG_TIMEOUT_MS} ms (got {seconds!r} s)", "OUT_OF_RANGE")


def _normalised(func):
    """Every ArithmeticError, OverflowError, TypeError and ValueError leaves as one ValueError."""
    def wrapper() -> None:
        try:
            func()
        except ValueError:
            raise
        except (ArithmeticError, TypeError) as exc:
            reason = "OVERFLOW" if isinstance(exc, ArithmeticError) else "TYPE"
            raise ConfigValueError(f"{func.__name__}: {type(exc).__name__}: {exc}", reason) from exc
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


def _sweeper_pass_checks() -> None:
    """The members of the sweeper pass snapshot (design 9.3 / SUP-28): the sweeper intervals, the candidate scan and the pass
    budgets. One definition: `validate_process_timing_limits` and `build_sweeper_pass_policy` both run exactly this."""
    interval_floor = P.BACKGROUND_MIN_INTERVAL_SECONDS
    _num("BACKGROUND_MIN_INTERVAL_SECONDS", interval_floor)
    _relation(interval_floor >= 0.1, "BACKGROUND_MIN_INTERVAL_SECONDS has the fixed literal floor 0.1")
    ceiling = P.ORPHAN_SWEEP_MAX_INTERVAL_SECONDS
    _num("ORPHAN_SWEEP_MAX_INTERVAL_SECONDS", ceiling)
    _relation(ceiling == _PROCESS_MAX_FIXED_SECONDS, "ORPHAN_SWEEP_MAX_INTERVAL_SECONDS is exactly 86400.0")

    # sweeper intervals
    for name in ("ORPHAN_SWEEP_INTERVAL_SECONDS", "ORPHAN_SWEEP_BACKOFF_MIN_SECONDS", "ORPHAN_SWEEP_BACKOFF_MAX_SECONDS",
                 "ORPHAN_SWEEP_LOG_THROTTLE_SECONDS"):
        _bounds(name, getattr(P, name), interval_floor, ceiling)
    _relation(P.ORPHAN_SWEEP_BACKOFF_MIN_SECONDS >= P.ORPHAN_SWEEP_INTERVAL_SECONDS,
              "ORPHAN_SWEEP_BACKOFF_MIN_SECONDS >= ORPHAN_SWEEP_INTERVAL_SECONDS")
    _relation(P.ORPHAN_SWEEP_BACKOFF_MIN_SECONDS <= P.ORPHAN_SWEEP_BACKOFF_MAX_SECONDS,
              "ORPHAN_SWEEP_BACKOFF_MIN_SECONDS <= ORPHAN_SWEEP_BACKOFF_MAX_SECONDS")

    # candidate scan and sweeper pass
    for name in ("ORPHAN_SWEEP_BATCH_ROWS", "CANDIDATE_PAGE_ROWS", "CANDIDATE_MAX_SCANNED_ROWS"):
        _count(name, getattr(P, name), 1)
    _relation(P.CANDIDATE_PAGE_ROWS <= P.CANDIDATE_MAX_SCANNED_ROWS, "CANDIDATE_PAGE_ROWS <= CANDIDATE_MAX_SCANNED_ROWS")
    _relation(P.ORPHAN_SWEEP_BATCH_ROWS <= P.CANDIDATE_MAX_SCANNED_ROWS,
              "ORPHAN_SWEEP_BATCH_ROWS <= CANDIDATE_MAX_SCANNED_ROWS")
    _relation(P.CANDIDATE_MAX_SCANNED_ROWS >= P.PROCESS_RISK_MAX_TARGETS,
              "CANDIDATE_MAX_SCANNED_ROWS >= PROCESS_RISK_MAX_TARGETS")
    _positive("ORPHAN_SWEEP_PASS_SECONDS", P.ORPHAN_SWEEP_PASS_SECONDS)
    _relation(P.ORPHAN_SWEEP_PASS_SECONDS <= _PROCESS_MAX_FIXED_SECONDS, "ORPHAN_SWEEP_PASS_SECONDS <= 86400.0")
    _relation(P.ORPHAN_SWEEP_PASS_SECONDS >= P.JOB_CLOSE_SETTLE_SECONDS + P.PROCESS_TERMINATE_GRACE_SECONDS,
              "ORPHAN_SWEEP_PASS_SECONDS >= JOB_CLOSE_SETTLE_SECONDS + PROCESS_TERMINATE_GRACE_SECONDS")
    _relation(P.ORPHAN_SWEEP_STARTUP_WAIT_SECONDS <= P.ORPHAN_SWEEP_PASS_SECONDS,
              "ORPHAN_SWEEP_STARTUP_WAIT_SECONDS <= ORPHAN_SWEEP_PASS_SECONDS")
    _positive("CANDIDATE_SCAN_MAX_SECONDS", P.CANDIDATE_SCAN_MAX_SECONDS)
    _relation(P.CANDIDATE_SCAN_MAX_SECONDS < P.ORPHAN_SWEEP_PASS_SECONDS,
              "CANDIDATE_SCAN_MAX_SECONDS < ORPHAN_SWEEP_PASS_SECONDS (strictly)")
    _relation(P.CANDIDATE_SCAN_MAX_SECONDS <= P.PROCESS_RISK_ROW_TIMEOUT_SECONDS,
              "CANDIDATE_SCAN_MAX_SECONDS <= PROCESS_RISK_ROW_TIMEOUT_SECONDS")
    _pg_ms("CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS", P.CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS)
    _relation(P.CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS <= P.CANDIDATE_SCAN_MAX_SECONDS,
              "CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS <= CANDIDATE_SCAN_MAX_SECONDS")
    _count("ORPHAN_SWEEP_STALL_THRESHOLD", P.ORPHAN_SWEEP_STALL_THRESHOLD, 1, 1000)
    _count("ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP", P.ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP, 0, 62)
    _bounds("BACKGROUND_WAKEUP_DEBOUNCE_SECONDS", P.BACKGROUND_WAKEUP_DEBOUNCE_SECONDS, interval_floor, 60.0)


@_normalised
def validate_process_timing_limits() -> None:
    """Every timing and count relation of design 9.2 (sweeper, background supervisor, capability, tracked run, candidate
    scan, risk-target lock, tracked-age threshold). Refused on any violation."""
    interval_floor = P.BACKGROUND_MIN_INTERVAL_SECONDS
    # inputs of relations only (design 9.3 judgement table): their own shape is a construction check, not a pass-snapshot member
    _positive("ORPHAN_SWEEP_STARTUP_WAIT_SECONDS", P.ORPHAN_SWEEP_STARTUP_WAIT_SECONDS)
    _positive("JOB_CLOSE_SETTLE_SECONDS", P.JOB_CLOSE_SETTLE_SECONDS)
    _num("PROCESS_TERMINATE_GRACE_SECONDS", P.PROCESS_TERMINATE_GRACE_SECONDS)
    _count("PROCESS_RISK_MAX_TARGETS", P.PROCESS_RISK_MAX_TARGETS, 1)
    _num("PROCESS_RISK_ROW_TIMEOUT_SECONDS", P.PROCESS_RISK_ROW_TIMEOUT_SECONDS)
    _sweeper_pass_checks()
    ceiling = P.ORPHAN_SWEEP_MAX_INTERVAL_SECONDS
    # NOT a snapshot constant (SUP-28 owner 3): refused at construction here, consumed only by the failure path of a pass
    check_sweep_fallback(P.ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS)
    # risk-target lock phase: lock wait < statement ceiling <= total budget
    for name in ("PROCESS_RISK_TARGET_LOCK_SECONDS", "PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS",
                 "PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS"):
        _pg_ms(name, getattr(P, name))
    _relation(P.PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS <= P.PROCESS_RISK_TARGET_LOCK_SECONDS,
              "PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS <= PROCESS_RISK_TARGET_LOCK_SECONDS")
    _relation(P.PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS < P.PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS,
              "PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS < PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS")

    # background supervisor
    _bounds("BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS", P.BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS, 1.0, 300.0)
    _positive("TREE_SETTLE_POLL_SECONDS", P.TREE_SETTLE_POLL_SECONDS)
    _relation(P.TREE_SETTLE_POLL_SECONDS <= P.PROCESS_TERMINATE_GRACE_SECONDS,
              "TREE_SETTLE_POLL_SECONDS <= PROCESS_TERMINATE_GRACE_SECONDS")
    _relation(P.BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS
              > P.PROCESS_TERMINATE_GRACE_SECONDS + P.TREE_SETTLE_POLL_SECONDS + 1.0,
              "BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS > PROCESS_TERMINATE_GRACE_SECONDS + TREE_SETTLE_POLL_SECONDS + 1.0")
    _bounds("BACKGROUND_READY_TIMEOUT_SECONDS", P.BACKGROUND_READY_TIMEOUT_SECONDS, 0.5, 60.0)
    _bounds("BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS", P.BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS, 0.5, 60.0)
    _bounds("BACKGROUND_RESTART_BACKOFF_BASE_SECONDS", P.BACKGROUND_RESTART_BACKOFF_BASE_SECONDS, interval_floor, 3600.0)
    _bounds("BACKGROUND_RESTART_BACKOFF_CAP_SECONDS", P.BACKGROUND_RESTART_BACKOFF_CAP_SECONDS, interval_floor, 3600.0)
    _count("BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP", P.BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP, 0, 62)
    _count("BACKGROUND_MAX_RESTARTS", P.BACKGROUND_MAX_RESTARTS, 1, 100)
    _count("BACKGROUND_RESTART_WINDOW_MAX", P.BACKGROUND_RESTART_WINDOW_MAX, 1, 1000)
    _positive("BACKGROUND_STABLE_SECONDS", P.BACKGROUND_STABLE_SECONDS)
    _positive("BACKGROUND_RESTART_WINDOW_SECONDS", P.BACKGROUND_RESTART_WINDOW_SECONDS)
    _relation(P.BACKGROUND_RESTART_BACKOFF_BASE_SECONDS <= P.BACKGROUND_RESTART_BACKOFF_CAP_SECONDS,
              "BACKGROUND_RESTART_BACKOFF_BASE_SECONDS <= BACKGROUND_RESTART_BACKOFF_CAP_SECONDS")
    _relation(P.BACKGROUND_RESTART_BACKOFF_CAP_SECONDS <= ceiling,
              "BACKGROUND_RESTART_BACKOFF_CAP_SECONDS <= ORPHAN_SWEEP_MAX_INTERVAL_SECONDS")
    _relation(P.BACKGROUND_RESTART_BACKOFF_CAP_SECONDS <= P.BACKGROUND_STABLE_SECONDS,
              "BACKGROUND_RESTART_BACKOFF_CAP_SECONDS <= BACKGROUND_STABLE_SECONDS")
    _relation(P.BACKGROUND_STABLE_SECONDS <= P.BACKGROUND_RESTART_WINDOW_SECONDS,
              "BACKGROUND_STABLE_SECONDS <= BACKGROUND_RESTART_WINDOW_SECONDS")
    _relation(P.BACKGROUND_STABLE_SECONDS <= _PROCESS_MAX_FIXED_SECONDS
              and P.BACKGROUND_RESTART_WINDOW_SECONDS <= _PROCESS_MAX_FIXED_SECONDS,
              "BACKGROUND_STABLE_SECONDS and BACKGROUND_RESTART_WINDOW_SECONDS <= 86400.0")
    _relation(P.BACKGROUND_MAX_RESTARTS <= P.BACKGROUND_RESTART_WINDOW_MAX,
              "BACKGROUND_MAX_RESTARTS <= BACKGROUND_RESTART_WINDOW_MAX")
    _bounds("BACKGROUND_WAKEUP_DEBOUNCE_SECONDS", P.BACKGROUND_WAKEUP_DEBOUNCE_SECONDS, interval_floor, 60.0)
    _relation(P.BACKGROUND_WAKEUP_DEBOUNCE_SECONDS <= P.ORPHAN_SWEEP_INTERVAL_SECONDS,
              "BACKGROUND_WAKEUP_DEBOUNCE_SECONDS <= ORPHAN_SWEEP_INTERVAL_SECONDS")
    _count("BACKGROUND_DB_CONCURRENCY", P.BACKGROUND_DB_CONCURRENCY, 1, 8)

    # capability task
    _num("CAPABILITY_REFRESH_SECONDS", P.CAPABILITY_REFRESH_SECONDS)
    _relation(P.CAPABILITY_REFRESH_SECONDS >= interval_floor,
              "CAPABILITY_REFRESH_SECONDS >= BACKGROUND_MIN_INTERVAL_SECONDS")
    _positive("CAPABILITY_WRITE_TIMEOUT_SECONDS", P.CAPABILITY_WRITE_TIMEOUT_SECONDS)
    _relation(P.CAPABILITY_WRITE_TIMEOUT_SECONDS < P.CAPABILITY_REFRESH_SECONDS,
              "CAPABILITY_WRITE_TIMEOUT_SECONDS < CAPABILITY_REFRESH_SECONDS")
    _positive("CAPABILITY_TTL_SECONDS", P.CAPABILITY_TTL_SECONDS)
    _relation(P.CAPABILITY_TTL_SECONDS >= 2 * P.CAPABILITY_REFRESH_SECONDS + P.CAPABILITY_WRITE_TIMEOUT_SECONDS,
              "CAPABILITY_TTL_SECONDS >= 2 * CAPABILITY_REFRESH_SECONDS + CAPABILITY_WRITE_TIMEOUT_SECONDS")

    # BACKGROUND_MIN_INTERVAL_SECONDS is at most every constant that it bounds (the risk ones are checked by
    # validate_process_risk_limits(), which owns their minimum rules)
    for name in ("ORPHAN_SWEEP_INTERVAL_SECONDS", "ORPHAN_SWEEP_BACKOFF_MIN_SECONDS", "ORPHAN_SWEEP_BACKOFF_MAX_SECONDS",
                 "ORPHAN_SWEEP_LOG_THROTTLE_SECONDS", "BACKGROUND_RESTART_BACKOFF_BASE_SECONDS",
                 "BACKGROUND_RESTART_BACKOFF_CAP_SECONDS", "BACKGROUND_WAKEUP_DEBOUNCE_SECONDS",
                 "CAPABILITY_REFRESH_SECONDS"):
        _relation(interval_floor <= getattr(P, name), f"BACKGROUND_MIN_INTERVAL_SECONDS <= {name}")

    # tracked run and the diagnostics thresholds
    _positive("PUMP_DRAIN_SECONDS", P.PUMP_DRAIN_SECONDS)
    _positive("TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS", P.TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS)
    _positive("TRACKED_EXECUTION_MAX_AGE_SECONDS", P.TRACKED_EXECUTION_MAX_AGE_SECONDS)
    _num("MAX_CLI_TIMEOUT_SECONDS", P.MAX_CLI_TIMEOUT_SECONDS)
    live_sum = (P.MAX_CLI_TIMEOUT_SECONDS + P.TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS
                + P.PROCESS_TERMINATE_GRACE_SECONDS + P.PUMP_DRAIN_SECONDS + P.TREE_SETTLE_POLL_SECONDS)
    _relation(P.TRACKED_EXECUTION_MAX_AGE_SECONDS >= live_sum,
              "TRACKED_EXECUTION_MAX_AGE_SECONDS >= MAX_CLI_TIMEOUT_SECONDS + TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS "
              "+ PROCESS_TERMINATE_GRACE_SECONDS + PUMP_DRAIN_SECONDS + TREE_SETTLE_POLL_SECONDS (the live sum)")
    _positive("PROCESS_RISK_REQUEST_STALE_SECONDS", P.PROCESS_RISK_REQUEST_STALE_SECONDS)
    _num("PROCESS_RISK_IDLE_INTERVAL_SECONDS", P.PROCESS_RISK_IDLE_INTERVAL_SECONDS)
    _num("PROCESS_RISK_PASS_SECONDS", P.PROCESS_RISK_PASS_SECONDS)
    _relation(P.PROCESS_RISK_REQUEST_STALE_SECONDS == P.PROCESS_RISK_IDLE_INTERVAL_SECONDS + P.PROCESS_RISK_PASS_SECONDS,
              "PROCESS_RISK_REQUEST_STALE_SECONDS == PROCESS_RISK_IDLE_INTERVAL_SECONDS + PROCESS_RISK_PASS_SECONDS")


@_normalised
def validate_process_risk_limits() -> None:
    """Rules (i) to (vii) of design 7.5 item 5, the single-constant minimum rules of the worker constants and the recheck
    rule of item 8. Refused on any violation."""
    for name in ("PROCESS_RISK_ROW_OVERHEAD_SECONDS", "PROCESS_RISK_ROW_TIMEOUT_SECONDS", "PROCESS_RISK_DB_OVERHEAD_SECONDS",
                 "PROCESS_RISK_REQUEST_TIMEOUT_SECONDS", "PROCESS_RISK_PASS_OVERHEAD_SECONDS", "PROCESS_RISK_PASS_SECONDS",
                 "PROCESS_RISK_CLAIM_MARGIN_SECONDS", "PROCESS_RISK_CLAIM_TIMEOUT_SECONDS", "JOB_CLOSE_SETTLE_SECONDS"):
        _positive(name, getattr(P, name))
    _num("PROCESS_TERMINATE_GRACE_SECONDS", P.PROCESS_TERMINATE_GRACE_SECONDS)
    _count("PROCESS_RISK_MAX_TARGETS", P.PROCESS_RISK_MAX_TARGETS, 1)
    _count("PROCESS_RISK_TARGETS_DB_LIMIT", P.PROCESS_RISK_TARGETS_DB_LIMIT, 1)
    _count("PROCESS_RISK_NORMAL_UNRESOLVED_PER_TASK", P.PROCESS_RISK_NORMAL_UNRESOLVED_PER_TASK, 1, 1)
    _relation(P.PROCESS_RISK_ROW_TIMEOUT_SECONDS >= (P.PROCESS_TERMINATE_GRACE_SECONDS + P.JOB_CLOSE_SETTLE_SECONDS
                                                     + P.PROCESS_RISK_ROW_OVERHEAD_SECONDS),
              "(i) PROCESS_RISK_ROW_TIMEOUT_SECONDS >= PROCESS_TERMINATE_GRACE_SECONDS + JOB_CLOSE_SETTLE_SECONDS "
              "+ PROCESS_RISK_ROW_OVERHEAD_SECONDS")
    _relation(P.PROCESS_RISK_REQUEST_TIMEOUT_SECONDS >= (P.PROCESS_RISK_MAX_TARGETS * P.PROCESS_RISK_ROW_TIMEOUT_SECONDS
                                                         + P.PROCESS_RISK_DB_OVERHEAD_SECONDS),
              "(ii) PROCESS_RISK_REQUEST_TIMEOUT_SECONDS >= PROCESS_RISK_MAX_TARGETS * PROCESS_RISK_ROW_TIMEOUT_SECONDS "
              "+ PROCESS_RISK_DB_OVERHEAD_SECONDS")
    _relation(P.PROCESS_RISK_PASS_SECONDS >= P.PROCESS_RISK_REQUEST_TIMEOUT_SECONDS + P.PROCESS_RISK_PASS_OVERHEAD_SECONDS,
              "(iii) PROCESS_RISK_PASS_SECONDS >= PROCESS_RISK_REQUEST_TIMEOUT_SECONDS + PROCESS_RISK_PASS_OVERHEAD_SECONDS")
    _relation(P.PROCESS_RISK_CLAIM_TIMEOUT_SECONDS >= (P.PROCESS_RISK_REQUEST_TIMEOUT_SECONDS
                                                       + P.PROCESS_RISK_CLAIM_MARGIN_SECONDS),
              "(iv) PROCESS_RISK_CLAIM_TIMEOUT_SECONDS >= PROCESS_RISK_REQUEST_TIMEOUT_SECONDS "
              "+ PROCESS_RISK_CLAIM_MARGIN_SECONDS")
    # (v) the backoff, with the single-constant minimum rules of the two intervals
    for name in ("PROCESS_RISK_IDLE_INTERVAL_SECONDS", "PROCESS_RISK_BACKOFF_BASE_SECONDS",
                 "PROCESS_RISK_BACKOFF_CAP_SECONDS"):
        _num(name, getattr(P, name))
        _relation(getattr(P, name) >= P.BACKGROUND_MIN_INTERVAL_SECONDS, f"{name} >= BACKGROUND_MIN_INTERVAL_SECONDS")
    _relation(P.PROCESS_RISK_BACKOFF_BASE_SECONDS <= P.PROCESS_RISK_BACKOFF_CAP_SECONDS,
              "(v) PROCESS_RISK_BACKOFF_BASE_SECONDS <= PROCESS_RISK_BACKOFF_CAP_SECONDS")
    _relation(P.PROCESS_RISK_BACKOFF_CAP_SECONDS < P.PROCESS_RISK_CLAIM_TIMEOUT_SECONDS,
              "(v) PROCESS_RISK_BACKOFF_CAP_SECONDS < PROCESS_RISK_CLAIM_TIMEOUT_SECONDS")
    # (vi) the request size
    _relation(P.PROCESS_RISK_MAX_TARGETS <= P.PROCESS_RISK_TARGETS_DB_LIMIT,
              "(vi) PROCESS_RISK_MAX_TARGETS <= PROCESS_RISK_TARGETS_DB_LIMIT")
    _relation(P.PROCESS_RISK_MAX_TARGETS >= P.PROCESS_RISK_NORMAL_UNRESOLVED_PER_TASK,
              "(vi) PROCESS_RISK_MAX_TARGETS >= PROCESS_RISK_NORMAL_UNRESOLVED_PER_TASK")
    # (vii) counts and the recheck limits
    for name in ("PROCESS_RISK_MAX_ATTEMPTS", "PROCESS_RISK_BATCH_REQUESTS", "PROCESS_RISK_BATCH_ROWS",
                 "PROCESS_RECHECK_MAX_PER_WINDOW"):
        _count(name, getattr(P, name), 1)
    for name in ("PROCESS_RECHECK_COOLDOWN_BASE_SECONDS", "PROCESS_RECHECK_COOLDOWN_CAP_SECONDS",
                 "PROCESS_RECHECK_WINDOW_SECONDS"):
        _positive(name, getattr(P, name))
    _relation(P.PROCESS_RECHECK_COOLDOWN_BASE_SECONDS <= P.PROCESS_RECHECK_COOLDOWN_CAP_SECONDS
              <= P.PROCESS_RECHECK_WINDOW_SECONDS,
              "(vii) PROCESS_RECHECK_COOLDOWN_BASE_SECONDS <= PROCESS_RECHECK_COOLDOWN_CAP_SECONDS "
              "<= PROCESS_RECHECK_WINDOW_SECONDS")


@_normalised
def validate_process_input_constants() -> None:
    """The ranges of design 9.3: the non-timing constants and the schema-fixed input limits. Refused on any violation."""
    _count("JOB_TERMINATE_EXIT_CODE", P.JOB_TERMINATE_EXIT_CODE, 1, 2_147_483_647)
    _count("DIAGNOSTIC_MAX_ANOMALIES", P.DIAGNOSTIC_MAX_ANOMALIES, 1)
    _count("DIAGNOSTIC_MAX_RELATED_IDS", P.DIAGNOSTIC_MAX_RELATED_IDS, 1)
    _count("DIAGNOSTIC_TRACKED_PROJECT_ROWS", P.DIAGNOSTIC_TRACKED_PROJECT_ROWS, 1)
    _count("ORPHAN_SWEEP_STALL_THRESHOLD", P.ORPHAN_SWEEP_STALL_THRESHOLD, 1, 1000)
    _count("ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP", P.ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP, 0, 62)
    # schema-fixed: written into migration 0005 (that the migration and the rendered SQL carry the same numbers is a test)
    _count("REQUESTED_BY_MAX_LENGTH", P.REQUESTED_BY_MAX_LENGTH, 128, 128)
    _count("PROCESS_RISK_RATIONALE_MAX_LENGTH", P.PROCESS_RISK_RATIONALE_MAX_LENGTH, 2000, 2000)
    _count("DETAIL_SCHEMA_VERSION", P.DETAIL_SCHEMA_VERSION, 1, 1)
    if P.DISPOSITION_KIND_RISK_ACCEPTED != "OPERATOR_RISK_ACCEPTED":
        raise ValueError("DISPOSITION_KIND_RISK_ACCEPTED must be exactly OPERATOR_RISK_ACCEPTED")
    # REQUESTED_BY_PATTERN: compiles, ASCII only, no space and no control character
    try:
        pattern = re.compile(P.REQUESTED_BY_PATTERN)
    except (re.error, TypeError) as exc:
        raise ValueError(f"REQUESTED_BY_PATTERN does not compile: {exc}") from exc
    if not P.REQUESTED_BY_PATTERN.isascii():
        raise ValueError("REQUESTED_BY_PATTERN must be ASCII only")
    for code in [*range(0, 33), 127]:
        if pattern.fullmatch(chr(code)):
            raise ValueError(f"REQUESTED_BY_PATTERN must not match U+{code:04X}")
    # the schema: ten keys, nine required, requested_by the only optional one
    schema = P.RISK_ACCEPTANCE_SCHEMA
    if not isinstance(schema, tuple) or len(schema) != 10 or len({f.key for f in schema}) != 10:
        raise ValueError("RISK_ACCEPTANCE_SCHEMA must be a tuple of exactly ten distinct field specs")
    if [f.key for f in schema if not f.required] != ["requested_by"]:
        raise ValueError("RISK_ACCEPTANCE_SCHEMA: requested_by must be the only optional field")
    required = P.RISK_ACCEPTANCE_REQUIRED_KEYS
    if len(required) != 9 or "requested_by" in required or required != {f.key for f in schema if f.required}:
        raise ValueError("RISK_ACCEPTANCE_REQUIRED_KEYS must be exactly the nine required keys of the schema")
    # the regular expressions compile; the requested_by one carries the schema-fixed length
    for name in ("RISK_ACCEPTANCE_UUID_RE", "RISK_ACCEPTANCE_TS_RE", "RISK_ACCEPTANCE_REQUESTED_BY_RE",
                 "RISK_ACCEPTANCE_RATIONALE_FORBIDDEN"):
        try:
            re.compile(getattr(P, name))
        except (re.error, TypeError) as exc:
            raise ValueError(f"{name} does not compile: {exc}") from exc
    if P.RISK_ACCEPTANCE_REQUESTED_BY_RE != f"^{P.REQUESTED_BY_PATTERN.removesuffix('+')}{{1,{P.REQUESTED_BY_MAX_LENGTH}}}$":
        raise ValueError("RISK_ACCEPTANCE_REQUESTED_BY_RE must be generated from REQUESTED_BY_PATTERN and "
                         "REQUESTED_BY_MAX_LENGTH")
    # component ids: a tuple of distinct, non-empty ASCII ids without ':', exactly the registry of the design
    ids = P.BACKGROUND_COMPONENT_IDS
    if (not isinstance(ids, tuple) or not all(isinstance(i, str) and i and i.isascii() and ":" not in i for i in ids)
            or len(set(ids)) != len(ids) or ids != ("sweeper", "risk-worker", "capability")):
        raise ValueError("BACKGROUND_COMPONENT_IDS must be the tuple ('sweeper', 'risk-worker', 'capability')")
    keys = P.TRACKED_ONLY_KEYS
    if not isinstance(keys, frozenset) or len(keys) != 9:
        raise ValueError("TRACKED_ONLY_KEYS must be a frozenset of exactly nine keys")


def validate_process_policy() -> None:
    """The linked set of the B2 constant validators, called together by the Runner constructor (R31-9): the construction is
    refused on the first violation. It holds no rule of its own."""
    validate_process_timing_limits()
    validate_process_risk_limits()
    validate_process_input_constants()


# --- R31-9: the one policy factory (design 9.1). Immutable copies; the Runner constructor owns the construction copies,
# the sweeper pass builds its own snapshot at every entry (SUP-28). Nothing here reads an environment variable. ---------------
from dataclasses import dataclass as _dataclass  # noqa: E402


def check_sweep_fallback(value) -> None:
    """SUP-28 case (3) / R31-10 (e): `ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS` is exactly 86400.0 and equals the ceiling; every
    other value, a usable number such as 86400.5 included, is refused (the sweeper treats it as the abnormal exit of row 28)."""
    _num("ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS", value)
    _relation(value == _PROCESS_MAX_FIXED_SECONDS and value == P.ORPHAN_SWEEP_MAX_INTERVAL_SECONDS,
              "ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS is exactly 86400.0 and equals ORPHAN_SWEEP_MAX_INTERVAL_SECONDS")


@_dataclass(frozen=True)
class SweeperPassPolicy:
    """The pass snapshot: built and validated at the ENTRY of every pass, consumed only inside it."""
    batch_rows: int
    pass_seconds: float
    interval_seconds: float
    backoff_min_seconds: float
    backoff_max_seconds: float
    backoff_exponent_cap: int
    log_throttle_seconds: float
    stall_threshold: int
    debounce_seconds: float
    min_interval_seconds: float
    startup_wait_seconds: float


@_normalised
def _check_sweeper_pass() -> None:
    _sweeper_pass_checks()


def build_sweeper_pass_policy() -> SweeperPassPolicy:
    """Validates the members of the pass snapshot (design 9.3) and returns the frozen copy; ValueError on any violation
    (the sweeper maps it to SWEEP_CONFIG_INVALID, `start()` and the recheck judge it the same way)."""
    _check_sweeper_pass()
    return SweeperPassPolicy(
        P.ORPHAN_SWEEP_BATCH_ROWS, P.ORPHAN_SWEEP_PASS_SECONDS, P.ORPHAN_SWEEP_INTERVAL_SECONDS,
        P.ORPHAN_SWEEP_BACKOFF_MIN_SECONDS, P.ORPHAN_SWEEP_BACKOFF_MAX_SECONDS, P.ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP,
        P.ORPHAN_SWEEP_LOG_THROTTLE_SECONDS, P.ORPHAN_SWEEP_STALL_THRESHOLD, P.BACKGROUND_WAKEUP_DEBOUNCE_SECONDS,
        P.BACKGROUND_MIN_INTERVAL_SECONDS, P.ORPHAN_SWEEP_STARTUP_WAIT_SECONDS)


@_dataclass(frozen=True)
class BackgroundPolicy:
    """The construction copies owned by the Runner object for its whole life (SUP-28 owner 2)."""
    settle_timeout_seconds: float
    recovery_settle_margin_seconds: float
    recovery_shutdown_wait_seconds: float
    ready_timeout_seconds: float
    max_restarts: int
    backoff_base_seconds: float
    backoff_cap_seconds: float
    backoff_exponent_cap: int
    stable_seconds: float
    window_seconds: float
    window_max: int
    min_interval_seconds: float
    capability_refresh_seconds: float
    capability_write_timeout_seconds: float
    risk_idle_interval_seconds: float
    db_concurrency: int = 1

    @property
    def recovery_settle_deadline_seconds(self) -> float:
        """SUP-32 / R31-8: the loop task and `startup-recovery` own deadline."""
        return self.recovery_shutdown_wait_seconds + self.recovery_settle_margin_seconds


def build_background_policy(recovery_shutdown_wait_seconds: float) -> BackgroundPolicy:
    """The Runner constructor's factory: validates the whole linked set (the three B2 validators) and returns the copies.
    A refused configuration changes no lifecycle because this runs before `start()`."""
    validate_process_policy()
    return BackgroundPolicy(
        P.BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS, P.BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS,
        recovery_shutdown_wait_seconds, P.BACKGROUND_READY_TIMEOUT_SECONDS, P.BACKGROUND_MAX_RESTARTS,
        P.BACKGROUND_RESTART_BACKOFF_BASE_SECONDS, P.BACKGROUND_RESTART_BACKOFF_CAP_SECONDS,
        P.BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP, P.BACKGROUND_STABLE_SECONDS, P.BACKGROUND_RESTART_WINDOW_SECONDS,
        P.BACKGROUND_RESTART_WINDOW_MAX, P.BACKGROUND_MIN_INTERVAL_SECONDS, P.CAPABILITY_REFRESH_SECONDS,
        P.CAPABILITY_WRITE_TIMEOUT_SECONDS, P.PROCESS_RISK_IDLE_INTERVAL_SECONDS, P.BACKGROUND_DB_CONCURRENCY)
