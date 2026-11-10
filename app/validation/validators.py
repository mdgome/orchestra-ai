"""Feature-scoped validators. Small functions; policy values come from policies.py."""
import math
import os
import re
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
