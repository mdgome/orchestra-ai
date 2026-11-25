"""The reader side of the process-tracking capability (design 6.2): what the API, the diagnostics and any instance may believe.

A plain async callable (`CapabilityReader.__call__`) so that `DiagnosticsService` takes it as an injected function. Order:

1. a Runner that is RUNNING in THIS process reads its own memory (never the table);
2. an unsupported platform is decided locally (no row, no database): `UNSUPPORTED_PLATFORM`;
3. otherwise the stored row is trusted only if ALL hold: its state is not `UNKNOWN`, `updated_at` is within
   `CAPABILITY_TTL_SECONDS`, the Runner advisory lock is currently held by some backend and that holder is the backend named
   by the row (`pg_locks`: `locktype='advisory'`, `granted`, `database` = the current database's OID, `pid` =
   `lock_backend_pid`). Anything else, a failing query included, is `STATUS_UNKNOWN` (fail closed).

`pg_locks` mapping: a session advisory lock taken with a 64-bit key appears with `classid = key >> 32`,
`objid = key & 0xFFFFFFFF` and `objsubid = 1`; an integration test takes the real lock and asserts exactly this.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text

from app.db import Database
from app.repositories.runtime_capability_repository import RuntimeCapabilityRepository
from app.validation import policies as P

STATE_SUPPORTED = "SUPPORTED"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
STATE_STATUS_UNKNOWN = "STATUS_UNKNOWN"             # the READER-side unknown (not the stored `UNKNOWN`)
_TRUSTED_STATES = frozenset({STATE_SUPPORTED, STATE_UNAVAILABLE, STATE_UNSUPPORTED_PLATFORM})
CAPABILITY_KEY = "process_tracking"

LOCK_HOLDER_SQL = text(
    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND classid::bigint = :classid AND objid::bigint = :objid AND objsubid = 1")


@dataclass(frozen=True)
class CapabilityReading:
    state: str                         # SUPPORTED | UNAVAILABLE | UNSUPPORTED_PLATFORM | STATUS_UNKNOWN
    reason_code: str | None = None
    source: str = "DATABASE"           # OWN_MEMORY | LOCAL_PLATFORM | DATABASE | UNTRUSTED


def advisory_key_parts(key: int) -> tuple[int, int]:
    """(classid, objid) of a session advisory lock taken with the bigint `key` (see the module docstring)."""
    unsigned = key & 0xFFFFFFFFFFFFFFFF
    return unsigned >> 32, unsigned & 0xFFFFFFFF


class CapabilityReader:
    def __init__(self, db: Database, advisory_key: int, *, own_state: Callable[[], str | None] = lambda: None,
                 platform_supported: Callable[[], bool] = lambda: True, ttl_seconds: float = P.CAPABILITY_TTL_SECONDS):
        """`own_state` returns the capability of the live Runner of THIS process while it is RUNNING, else None."""
        self._db, self._key = db, advisory_key
        self._own_state, self._platform_supported, self._ttl = own_state, platform_supported, ttl_seconds

    async def __call__(self) -> CapabilityReading:
        own = self._own_state()
        if own is not None:
            return (CapabilityReading(own, source="OWN_MEMORY") if own in _TRUSTED_STATES
                    else CapabilityReading(STATE_STATUS_UNKNOWN, source="OWN_MEMORY"))
        if not self._platform_supported():
            return CapabilityReading(STATE_UNSUPPORTED_PLATFORM, source="LOCAL_PLATFORM")
        try:
            return await self._from_database()
        except Exception:  # noqa: BLE001 - a failing query never trusts an earlier value
            return CapabilityReading(STATE_STATUS_UNKNOWN, source="UNTRUSTED")

    async def _from_database(self) -> CapabilityReading:
        untrusted = CapabilityReading(STATE_STATUS_UNKNOWN, source="UNTRUSTED")
        async with self._db.transaction() as s:
            row = await RuntimeCapabilityRepository(s).get(CAPABILITY_KEY)
            if row is None or not isinstance(row.value, dict):
                return untrusted
            value = row.value
            if value.get("state") not in _TRUSTED_STATES:
                return untrusted                                   # UNKNOWN: the incarnation has not finished its self-test
            db_now = (await s.execute(text("SELECT now()"))).scalar_one()
            if db_now - row.updated_at > timedelta(seconds=self._ttl):
                return untrusted
            classid, objid = advisory_key_parts(self._key)
            holders = {r[0] for r in (await s.execute(LOCK_HOLDER_SQL, {"classid": classid, "objid": objid})).all()}
            pid = value.get("lock_backend_pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid not in holders:
                return untrusted                                   # nobody holds the lock, or it is not the row's incarnation
            reason = value.get("reason_code")
            return CapabilityReading(value["state"], reason if isinstance(reason, str) else None)
