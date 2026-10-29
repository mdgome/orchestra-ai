"""Embedded PostgreSQL for tests (pixeltable-pgserver). Fails loudly when unavailable: no silent skips."""
import tempfile

import pixeltable_pgserver as pgserver


class EmbeddedPg:
    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="orch-pg-")
        self._server = pgserver.get_server(self._dir)

    @property
    def sync_uri(self) -> str:
        return self._server.get_uri()

    def async_url(self, database: str = "postgres") -> str:
        base = self.sync_uri.replace("postgresql://", "postgresql+asyncpg://", 1)
        return base.rsplit("/", 1)[0] + "/" + database

    def stop(self) -> None:
        self._server.cleanup()
