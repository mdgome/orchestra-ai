from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://orchestrator:orchestrator@localhost:5432/orchestrator"
    workspace_root: Path = Path("./workspaces")
    codex_bin: str = "codex"
    claude_bin: str = "claude"
    lease_ttl_seconds: int = 60
    claim_timeout_seconds: int = 300
    # Unique key for the single-runner advisory lock (see services/runner.py).
    runner_advisory_lock_key: int = 748_201_001


def get_settings() -> Settings:
    return Settings()
