import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.container import Container, build_container
from app.providers.preflight import check_installed_cli, scan_all_settings
from app.routers import approvals, conversations, events, projects, tasks
from app.services.runner import RunnerAlreadyActive
from app.validation.error_handlers import register_error_handlers

log = logging.getLogger("orchestrator")


async def _preflight(container: Container) -> None:
    report = await check_installed_cli(container.executor, container.guard.root, container.settings.claude_bin,
                                       container.settings.codex_bin)
    report.settings_findings = scan_all_settings(container.guard.root)
    container.preflight = report


def raise_combined(errors: list[BaseException]) -> None:
    """Raises what a shutdown should report. One error: itself. A CancelledError wins (cancellation must stay a
    cancellation, never sit inside a group) and the other error is kept as its explicit __cause__. Otherwise several
    plain Exceptions form an ExceptionGroup, and BaseExceptionGroup is only used when a non-Exception is present."""
    if not errors:
        return
    for err in errors:
        log.error("shutdown error: %r", err)
    if len(errors) == 1:
        raise errors[0]
    cancelled = next((e for e in errors if isinstance(e, asyncio.CancelledError)), None)
    if cancelled is not None:
        others = [e for e in errors if e is not cancelled]
        raise cancelled from (others[0] if len(others) == 1 else
                              (ExceptionGroup if all(isinstance(e, Exception) for e in others)
                               else BaseExceptionGroup)("shutdown errors", others))
    group = ExceptionGroup if all(isinstance(e, Exception) for e in errors) else BaseExceptionGroup
    raise group("shutdown errors", errors)


async def shutdown_runtime(c: Container, owns_db: bool, body_error: BaseException | None) -> None:
    """Stops the Runner, then disposes the database ONLY if this application created it and stop() succeeded: a failed
    stop (RunnerStopIncomplete) keeps the advisory lock, and closing the pool would silently void that. When the
    process itself is going away the OS closes the connection and so releases the lock; a caller that survives the
    failed stop still holds it until a later stop() succeeds."""
    errors: list[BaseException] = [body_error] if body_error is not None else []
    try:
        await c.runner.stop()
    except BaseException as exc:  # noqa: BLE001 - collected, combined below
        errors.append(exc)
    else:
        if owns_db:
            try:
                await c.db.dispose()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
    raise_combined(errors)


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
        body_error: BaseException | None = None
        try:
            yield
        except BaseException as exc:  # noqa: BLE001 - preserved, then combined with any shutdown error
            body_error = exc
        await shutdown_runtime(c, owns_db=container is None, body_error=body_error)

    app = FastAPI(title="Multi-Agent Orchestrator", version="0.1.0", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(conversations.router)
    app.include_router(projects.router)
    app.include_router(tasks.router)
    app.include_router(approvals.router)
    app.include_router(events.router)

    @app.get("/health", tags=["health"])
    async def health():
        c = app.state.container
        return {"status": "ok", "runner_active": c.runner_active}

    return app


app = create_app()
