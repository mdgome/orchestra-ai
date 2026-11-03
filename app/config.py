from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://orchestrator:orchestrator@localhost:5432/orchestrator"
    workspace_root: Path = Path("./workspaces")
    # Git clones for projects registered by repository_url are created below this directory only.
    project_clone_root: Path = Path("./project-clones")
    codex_bin: str = "codex"
    claude_bin: str = "claude"
    lease_ttl_seconds: int = 60
    claim_timeout_seconds: int = 300      # grace before a CLAIMED request without a live lease is considered stale
    # Bounded parallel execution (one process; new OS processes per project are intentionally not used).
    max_concurrent_tasks: int = Field(2, ge=1)
    max_tasks_per_project: int = Field(1, ge=1)
    max_concurrent_cli_processes: int = Field(4, ge=1)
    # Database pool. `db_pool_size` unset -> the recommended `4 * MAX_CONCURRENT_TASKS + 6` (README explains the
    # estimate). The only hard requirement is a total capacity of 2: the Runner's advisory lock holds one connection
    # for its whole life, so at least one more is needed for work.
    db_pool_size: int | None = Field(None, ge=1)
    db_max_overflow: int = Field(10, ge=0)
    runner_shutdown_grace_seconds: float = 30.0
    # Unique key for the single-runner advisory lock (see services/runner.py).
    runner_advisory_lock_key: int = 748_201_001


    @property
    def effective_db_pool_size(self) -> int:
        return self.db_pool_size if self.db_pool_size is not None else 4 * self.max_concurrent_tasks + 6

    @model_validator(mode="after")
    def _pool_has_room_beside_the_runner_lock(self) -> "Settings":
        if self.effective_db_pool_size + self.db_max_overflow < 2:
            raise ValueError("DB_POOL_SIZE + DB_MAX_OVERFLOW must be at least 2: the Runner's advisory lock "
                             "permanently holds one connection")
        return self


def get_settings() -> Settings:
    return Settings()
