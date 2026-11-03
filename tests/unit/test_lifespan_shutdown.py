import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.main import create_app, raise_combined, shutdown_runtime
from app.services.runner import RunnerStopIncomplete


class FakeRunner:
    def __init__(self, error=None):
        self.error, self.stops = error, 0

    async def stop(self):
        self.stops += 1
        if self.error:
            raise self.error


class FakeDb:
    def __init__(self, error=None):
        self.error, self.disposed = error, 0

    async def dispose(self):
        self.disposed += 1
        if self.error:
            raise self.error


def container(runner_error=None, dispose_error=None):
    return SimpleNamespace(runner=FakeRunner(runner_error), db=FakeDb(dispose_error))


async def test_clean_shutdown_disposes_an_internally_created_db():
    c = container()
    await shutdown_runtime(c, owns_db=True, body_error=None)
    assert c.runner.stops == 1 and c.db.disposed == 1


async def test_an_injected_db_is_never_disposed():
    c = container()
    await shutdown_runtime(c, owns_db=False, body_error=None)
    assert c.runner.stops == 1 and c.db.disposed == 0


async def test_a_failed_stop_propagates_and_skips_dispose():
    c = container(runner_error=RunnerStopIncomplete(["r"]))
    with pytest.raises(RunnerStopIncomplete):
        await shutdown_runtime(c, owns_db=True, body_error=None)
    assert c.db.disposed == 0


async def test_a_body_error_alone_is_re_raised_as_is():
    body = KeyError("body")
    c = container()
    with pytest.raises(KeyError) as info:
        await shutdown_runtime(c, owns_db=True, body_error=body)
    assert info.value is body and c.db.disposed == 1


async def test_body_error_plus_failed_stop_is_an_exception_group_without_dispose():
    c = container(runner_error=RunnerStopIncomplete(["r"]))
    with pytest.raises(ExceptionGroup) as info:
        await shutdown_runtime(c, owns_db=True, body_error=KeyError("body"))
    assert {type(e) for e in info.value.exceptions} == {KeyError, RunnerStopIncomplete} and c.db.disposed == 0


async def test_cancellation_stays_a_cancellation_with_the_stop_error_as_cause():
    cancelled, stop_error = asyncio.CancelledError(), RunnerStopIncomplete(["r"])
    c = container(runner_error=stop_error)
    with pytest.raises(asyncio.CancelledError) as info:
        await shutdown_runtime(c, owns_db=True, body_error=cancelled)
    assert info.value is cancelled and info.value.__cause__ is stop_error and c.db.disposed == 0


async def test_dispose_failure_after_a_good_stop_is_reported_alone_or_with_the_body_error():
    with pytest.raises(OSError):
        await shutdown_runtime(container(dispose_error=OSError("dispose")), owns_db=True, body_error=None)
    with pytest.raises(ExceptionGroup):
        await shutdown_runtime(container(dispose_error=OSError("d")), owns_db=True, body_error=KeyError("b"))


def test_non_exception_base_errors_use_a_base_exception_group():
    with pytest.raises(BaseExceptionGroup) as info:
        raise_combined([KeyboardInterrupt(), SystemExit(1)])
    assert not isinstance(info.value, ExceptionGroup)
    with pytest.raises(ExceptionGroup):
        raise_combined([KeyError("a"), ValueError("b")])


# --- the real lifespan wiring (create_app) -------------------------------------------------------------------------------------
class StartableRunner(FakeRunner):
    async def start(self):
        return None


def injected_container(runner_error=None):
    """The minimum the lifespan touches when a container is injected and run_preflight=False."""
    return SimpleNamespace(preflight=None, runner_active=False, runner=StartableRunner(runner_error), db=FakeDb(),
                           settings=Settings(_env_file=None))


def lifespan_of(c):
    app = create_app(settings=c.settings, container=c, run_preflight=False)
    return app.router.lifespan_context(app)


async def test_the_lifespan_starts_and_stops_the_runner_and_never_disposes_an_injected_db():
    c = injected_container()
    async with lifespan_of(c):
        assert c.runner_active is True
    assert c.runner.stops == 1 and c.db.disposed == 0


async def test_a_body_exception_reaches_the_caller_through_the_lifespan_and_the_runner_is_still_stopped():
    c, body = injected_container(), KeyError("body")
    with pytest.raises(KeyError) as info:
        async with lifespan_of(c):
            raise body
    assert info.value is body and c.runner.stops == 1 and c.db.disposed == 0


async def test_a_body_cancellation_stays_a_cancellation_through_the_lifespan():
    c, body = injected_container(), asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as info:
        async with lifespan_of(c):
            raise body
    assert info.value is body and c.runner.stops == 1 and c.db.disposed == 0


async def test_a_failed_stop_surfaces_from_the_lifespan_and_a_body_error_travels_with_it():
    stop_error = RunnerStopIncomplete(["r"])
    with pytest.raises(RunnerStopIncomplete) as info:
        async with lifespan_of(injected_container(runner_error=stop_error)):
            pass
    assert info.value is stop_error
    with pytest.raises(ExceptionGroup) as grouped:
        async with lifespan_of(injected_container(runner_error=RunnerStopIncomplete(["r"]))):
            raise KeyError("body")
    assert {type(e) for e in grouped.value.exceptions} == {KeyError, RunnerStopIncomplete}
