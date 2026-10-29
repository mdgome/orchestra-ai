import hashlib
import json


def stable_hash(*parts) -> str:
    """Deterministic sha256 over JSON-serialisable parts (key order independent)."""
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
