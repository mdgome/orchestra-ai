"""Feature-scoped validators. Small functions; policy values come from policies.py."""
import re
import uuid
from datetime import datetime

from app.validation import policies as P
from app.validation.exceptions import FailClosed, ValidationFailed


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


def validate_test_policy(policy: dict | None) -> dict:
    policy = policy or P.DEFAULT_TEST_POLICY
    commands = policy.get("commands")
    if not commands or not all(isinstance(c, list) and c and all(isinstance(a, str) for a in c) for c in commands):
        raise ValidationFailed("test_policy.commands must be a non-empty list of argument lists")
    if int(policy.get("min_tests_executed", 1)) < 1:
        raise ValidationFailed("test_policy.min_tests_executed must be >= 1")
    timeout = int(policy.get("timeout_seconds", 600))
    if not (1 <= timeout <= P.MAX_CLI_TIMEOUT_SECONDS):
        raise ValidationFailed("test_policy.timeout_seconds out of range")
    return {**P.DEFAULT_TEST_POLICY, **policy, "timeout_seconds": timeout}


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
