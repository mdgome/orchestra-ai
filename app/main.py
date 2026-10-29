import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.container import Container, build_container
from app.providers.preflight import check_installed_cli, scan_all_settings
from app.routers import approvals, conversations, events, tasks
from app.services.runner import RunnerAlreadyActive
from app.validation.error_handlers import register_error_handlers

log = logging.getLogger("orchestrator")


async def _preflight(container: Container) -> None:
    report = await check_installed_cli(container.executor, container.guard.root, container.settings.claude_bin,
                                       container.settings.codex_bin)
    report.settings_findings = scan_all_settings(container.guard.root)
    container.preflight = report


def create_app(settings: Settings | None = None, container: Container | None = None,
               run_preflight: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = container or build_container(settings)
        app.state.container = c
        if run_preflight:
            await _preflight(c)
            if c.preflight and c.preflight.blocking:
                log.error("preflight failed, the Runner will not execute tasks: %s", c.preflight.blocking)
        if not (c.preflight and c.preflight.blocking):
            try:
                await c.runner.start()
                c.runner_active = True
            except RunnerAlreadyActive:
                log.error("another Runner process is active: this API instance only stores requests")
        try:
            yield
        finally:
            await c.runner.stop()
            if container is None:
                await c.db.dispose()

    app = FastAPI(title="Multi-Agent Orchestrator", version="0.1.0", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(conversations.router)
    app.include_router(tasks.router)
    app.include_router(approvals.router)
    app.include_router(events.router)

    @app.get("/health", tags=["health"])
    async def health():
        c = app.state.container
        return {"status": "ok", "runner_active": c.runner_active}

    return app


app = create_app()
