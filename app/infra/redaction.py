"""Masks credentials before text reaches logs, the database or API responses."""
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
