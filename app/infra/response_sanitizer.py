"""One recursive sanitiser for every free-form JSON / text a response returns (artifacts, the report).

* The input is never modified: a new structure is built. No key is ever ADDED to it (no `_truncated` marker); what was
  cut is reported next to the value in a `SanitizedJson` (`truncated`, `reasons`, `omitted_paths`).
* Removed keys: an exact deny list plus a reviewed suffix rule (`..._token`, `..._secret`, ...). A normal field such as
  `session_fingerprint` or `token_count` is allow-listed and never removed by substring matching.
* Strings lose credentials (URL userinfo, `ghp_`/`glpat-`/`sk-` tokens, Bearer tokens) and every KNOWN native session id
  of the task; ordinary UUIDs (task, step, approval ids) are meaningful and stay.
* Bounded: depth, keys per object, items per array, string length, node count, cycles; then a byte budget that is met by
  cutting the biggest strings first, then array tails, then the biggest nested object, and only as a last resort the
  whole value. Each cause has its own reason code.
"""
import dataclasses
import enum
import json
import uuid
from datetime import date, datetime
from typing import Any

from app.infra.redaction import redact_credentials, redact_session_ids
from app.validation import policies as P

TRUNCATION_MARK = "...[truncated]"


class SanitizeReason(str, enum.Enum):
    KEY_REMOVED = "KEY_REMOVED"
    SECRET_REDACTED = "SECRET_REDACTED"          # informational: something was masked, nothing was cut
    STRING_TRUNCATED = "STRING_TRUNCATED"
    ARRAY_TRUNCATED = "ARRAY_TRUNCATED"
    KEYS_LIMIT = "KEYS_LIMIT"
    DEPTH_LIMIT = "DEPTH_LIMIT"
    NODE_LIMIT = "NODE_LIMIT"
    CYCLE_DETECTED = "CYCLE_DETECTED"
    SIZE_LIMIT = "SIZE_LIMIT"


# Reasons that mean information is missing from the returned value (SECRET_REDACTED / KEY_REMOVED are masking).
LOSSY = frozenset({SanitizeReason.STRING_TRUNCATED, SanitizeReason.ARRAY_TRUNCATED, SanitizeReason.KEYS_LIMIT,
                   SanitizeReason.DEPTH_LIMIT, SanitizeReason.NODE_LIMIT, SanitizeReason.CYCLE_DETECTED,
                   SanitizeReason.SIZE_LIMIT})


@dataclasses.dataclass
class SanitizedJson:
    value: Any
    truncated: bool
    reasons: tuple[str, ...]
    omitted_paths: tuple[str, ...]


class _Run:
    """Accumulates one sanitising run."""

    def __init__(self) -> None:
        self.reasons: dict[SanitizeReason, None] = {}      # ordered set
        self.paths: list[str] = []
        self.nodes = 0
        self.stack: set[int] = set()

    def note(self, reason: SanitizeReason, path: tuple[str, ...] = ()) -> None:
        self.reasons[reason] = None
        if path and len(self.paths) < P.SANITIZE_MAX_OMITTED_PATHS:
            text = "/".join(path)
            if text not in self.paths:
                self.paths.append(text)

    def result(self, value: Any) -> SanitizedJson:
        return SanitizedJson(value, any(r in LOSSY for r in self.reasons), tuple(r.value for r in self.reasons),
                             tuple(self.paths))


def is_denied_key(key: str) -> bool:
    k = key.strip().lower()
    if k in P.SANITIZE_ALLOW_KEYS:
        return False
    return k in P.SANITIZE_DENY_KEYS or k.endswith(P.SANITIZE_DENY_KEY_SUFFIXES)


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class Sanitizer:
    def __init__(self, known_session_ids: tuple[str, ...] = ()):
        self._known = tuple(i for i in known_session_ids if i)

    # --- text ---------------------------------------------------------------------------------------------
    def text(self, value: str | None, *, max_chars: int | None = None) -> tuple[str, tuple[str, ...]]:
        """(sanitised text, reason codes). Used for fields that are plain strings in a response."""
        run = _Run()
        out = self._string(value or "", (), run, max_chars)
        return out, tuple(r.value for r in run.reasons)

    def _string(self, text: str, path: tuple[str, ...], run: _Run, max_chars: int | None = None) -> str:
        max_chars = P.SANITIZE_MAX_STRING_CHARS if max_chars is None else max_chars    # read at call time
        clean = redact_session_ids(redact_credentials(text), self._known, all_uuids=False)
        if clean != text:
            run.note(SanitizeReason.SECRET_REDACTED)
        if len(clean) > max_chars:
            run.note(SanitizeReason.STRING_TRUNCATED, path)
            clean = clean[:max_chars] + TRUNCATION_MARK
        return clean

    # --- json -----------------------------------------------------------------------------------------------
    def json(self, value: Any, *, max_bytes: int | None = None) -> SanitizedJson:
        max_bytes = P.SANITIZED_JSON_MAX_BYTES if max_bytes is None else max_bytes
        run = _Run()
        out = self._walk(value, 0, (), run)
        out = self._fit(out, max_bytes, run)
        return run.result(out)

    def _walk(self, v: Any, depth: int, path: tuple[str, ...], run: _Run) -> Any:
        run.nodes += 1
        if run.nodes > P.SANITIZE_MAX_NODES:
            run.note(SanitizeReason.NODE_LIMIT, path)
            return None
        if v is None or isinstance(v, (bool, int)):
            return v
        if isinstance(v, float):
            return v if v == v and v not in (float("inf"), float("-inf")) else None
        if isinstance(v, str):
            return self._string(v, path, run)
        if isinstance(v, (dict, list, tuple)):
            if depth >= P.SANITIZE_MAX_DEPTH:
                run.note(SanitizeReason.DEPTH_LIMIT, path)
                return None
            if id(v) in run.stack:
                run.note(SanitizeReason.CYCLE_DETECTED, path)
                return None
            run.stack.add(id(v))
            try:
                return self._container(v, depth, path, run)
            finally:
                run.stack.discard(id(v))
        if isinstance(v, enum.Enum):
            return self._walk(v.value, depth, path, run)
        if isinstance(v, (datetime, date)):
            return v.isoformat()
        if isinstance(v, uuid.UUID):
            return str(v)
        return self._string(str(v), path, run)

    def _container(self, v: Any, depth: int, path: tuple[str, ...], run: _Run) -> Any:
        if isinstance(v, dict):
            out: dict = {}
            for index, (key, val) in enumerate(v.items()):
                if index >= P.SANITIZE_MAX_KEYS:
                    run.note(SanitizeReason.KEYS_LIMIT, path)
                    break
                name = str(key)
                if is_denied_key(name):
                    run.note(SanitizeReason.KEY_REMOVED, (*path, name))
                    continue
                out[name] = self._walk(val, depth + 1, (*path, name), run)
            return out
        items: list = []
        for index, item in enumerate(v):
            if index >= P.SANITIZE_MAX_ITEMS:
                run.note(SanitizeReason.ARRAY_TRUNCATED, path)
                break
            items.append(self._walk(item, depth + 1, (*path, str(index)), run))
        return items

    # --- byte budget ------------------------------------------------------------------------------------------
    def _fit(self, value: Any, max_bytes: int, run: _Run) -> Any:
        """Meets `max_bytes`: biggest strings first, then array tails, then the biggest nested object, last the whole
        value. `value` is our own copy, so it is edited in place."""
        for _ in range(P.SANITIZE_SHRINK_MAX_ROUNDS):
            if _size(value) <= max_bytes:
                return value
            if (self._cut_string(value, run) or self._cut_array(value, run) or self._null_object(value, run)):
                continue
            break
        if _size(value) <= max_bytes:
            return value
        run.note(SanitizeReason.SIZE_LIMIT)
        return None

    @staticmethod
    def _nodes(value: Any, path: tuple[str, ...] = ()):
        """(parent, key, node, path) for every node below the root."""
        if isinstance(value, dict):
            for k, v in list(value.items()):
                yield value, k, v, (*path, k)
                yield from Sanitizer._nodes(v, (*path, k))
        elif isinstance(value, list):
            for i, v in enumerate(list(value)):
                yield value, i, v, (*path, str(i))
                yield from Sanitizer._nodes(v, (*path, str(i)))

    def _cut_string(self, value: Any, run: _Run) -> bool:
        best = None
        for parent, key, node, path in self._nodes(value):
            if isinstance(node, str) and len(node) > P.SANITIZE_MIN_SHRINK_STRING and (best is None or len(node) > len(best[2])):
                best = (parent, key, node, path)
        if best is None:
            return False
        parent, key, node, path = best
        parent[key] = node[:max(P.SANITIZE_MIN_SHRINK_STRING, len(node) // 2)] + TRUNCATION_MARK
        run.note(SanitizeReason.STRING_TRUNCATED, path)
        return True

    def _cut_array(self, value: Any, run: _Run) -> bool:
        best = None
        candidates = [(value, None, value, ())] if isinstance(value, list) else []
        candidates += [(p, k, n, path) for p, k, n, path in self._nodes(value) if isinstance(n, list)]
        for entry in candidates:
            if len(entry[2]) > 1 and (best is None or len(entry[2]) > len(best[2])):
                best = entry
        if best is None:
            return False
        arr, path = best[2], best[3]
        del arr[max(1, len(arr) // 2):]
        run.note(SanitizeReason.ARRAY_TRUNCATED, path)
        return True

    def _null_object(self, value: Any, run: _Run) -> bool:
        best = None
        for parent, key, node, path in self._nodes(value):
            if isinstance(node, (dict, list)):
                size = _size(node)
                if best is None or size > best[0]:
                    best = (size, parent, key, path)
        if best is None:
            return False
        _, parent, key, path = best
        parent[key] = None
        run.note(SanitizeReason.SIZE_LIMIT, path)
        return True
