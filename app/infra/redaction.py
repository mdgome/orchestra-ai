"""Masks credentials and native session ids before text reaches logs, the database or API responses.

Both providers and the response sanitizer use these primitives: the dependency points from them to infra, never back."""
import hashlib
import re

# scheme://user:secret@host  and  scheme://token@host
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@")
_TOKEN_PATTERNS = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:bearer|token)\s+[A-Za-z0-9._~+/=-]{16,}"),
)
MASK = "***"


def redact_credentials(text: str | None) -> str:
    if not text:
        return ""
    text = _URL_USERINFO.sub(lambda m: m.group("scheme") + MASK + "@", text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(MASK, text)
    return text


_UUIDISH = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def session_fingerprint(native_id: str) -> str:
    """Irreversible short tag, safe for audit logs. Cannot be used to resume a session."""
    return "sess:" + hashlib.sha256(native_id.encode()).hexdigest()[:8]


def redact_session_ids(text: str | None, known_ids: tuple[str, ...] = (), *, all_uuids: bool, tail: int | None = None) -> str:
    """Replaces every KNOWN native session id by its fingerprint. With `all_uuids` anything UUID-shaped goes too: right
    for raw CLI output, where an unknown id may appear; wrong for stored data, where ordinary UUIDs (task ids, step ids)
    are meaningful and must stay. `tail` keeps only the last N characters (log tails)."""
    if not text:
        return ""
    for known in known_ids:
        if known:
            text = text.replace(known, session_fingerprint(known))
    if all_uuids:
        text = _UUIDISH.sub(lambda m: session_fingerprint(m.group(0)), text)
    return text[-tail:] if tail is not None else text
