"""Phase 4b unit tests: the shared DB semaphore (BACKGROUND_DB_CONCURRENCY), the pass-snapshot membership against the judgement
table of design 9.3, and SUP-T T9/T10/T12/T27 to T30 as per-row matrices."""
import ast
import asyncio

import pytest

import app.services.runner as runner_module
from app.services.background_supervisor import ExitEvent, Origin, TaskCancelReason, UnitState
from app.validation import policies as P
from app.validation.validators import build_sweeper_pass_policy
from tests.unit.test_background_supervisor import Host, alive, cleanup, drain, dying, make, settle_loop
from tests.unit.test_runner_sweeper import make_runner


# --- BACKGROUND_DB_CONCURRENCY -----------------------------------------------------------------------------------------------
async def test_the_db_semaphore_limits_all_component_sections_together_and_is_released_on_every_exit():
    r = make_runner()
    assert r._db_slots._value == P.BACKGROUND_DB_CONCURRENCY == 1
    r._db_slots = asyncio.Semaphore(2)
    state = {"running": 0, "peak": 0}

    async def section(fail=False):
        async with r.background_db_section():
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.02)
            state["running"] -= 1
            if fail:
                raise RuntimeError("boom")

    await asyncio.gather(*(section() for _ in range(6)))
    assert state["peak"] == 2                                                    # never more than the configured slots
    with pytest.raises(RuntimeError):
        await section(fail=True)
    holders = [asyncio.ensure_future(section()), asyncio.ensure_future(section())]
    waiter = asyncio.ensure_future(section())
    await asyncio.sleep(0)
    waiter.cancel()                                                              # a cancelled waiter takes no slot
    await asyncio.gather(*holders, waiter, return_exceptions=True)
    assert r._db_slots._value == 2


def test_the_semaphore_is_used_by_component_sections_only_never_by_dispatch_recovery_or_closing_writes():
    tree = ast.parse(open(runner_module.__file__, encoding="utf-8").read())
    funcs = {n.name: ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    users = {name for name, src in funcs.items() if "background_db_section" in src and name != "background_db_section"}
    assert users == {"_write_capability"}                                        # the phase-5 seams opt in the same way
    for forbidden in ("_dispatch", "_recover_rest", "_recovery_body", "_cancel_idle_tasks", "process", "_park_uncertain"):
        assert "_db_slots" not in funcs[forbidden] and "background_db_section" not in funcs[forbidden], forbidden


@pytest.mark.parametrize("value", [1, 2, 8])
def test_a_valid_db_concurrency_reaches_the_policy_copy(monkeypatch, value):
    monkeypatch.setattr(P, "BACKGROUND_DB_CONCURRENCY", value)
    assert make_runner()._bg.db_concurrency == value


@pytest.mark.parametrize("value", [0, -1, 9, 1.0, 10**400, True, None], ids=["zero", "negative", "nine", "float", "huge", "bool", "none"])
def test_an_invalid_db_concurrency_is_refused_at_construction(monkeypatch, value):
    monkeypatch.setattr(P, "BACKGROUND_DB_CONCURRENCY", value)
    with pytest.raises(ValueError):
        make_runner()


async def test_t11_stop_on_a_stopped_runner_touches_nothing():
    r = make_runner()
    before = (r._generation, r._state, r._supervisor.registry_empty)
    await r.stop()
    assert (r._generation, r._state, r._supervisor.registry_empty) == before and r._ctx is None


# --- pass-snapshot membership against the judgement table of 9.3 --------------------------------------------------------------
@pytest.mark.parametrize("name,bad", [
    ("ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP", 63), ("ORPHAN_SWEEP_STALL_THRESHOLD", 0), ("BACKGROUND_WAKEUP_DEBOUNCE_SECONDS", 0.09),
    ("ORPHAN_SWEEP_MAX_INTERVAL_SECONDS", 86400.5), ("ORPHAN_SWEEP_INTERVAL_SECONDS", 0.09),
    ("ORPHAN_SWEEP_BACKOFF_MIN_SECONDS", 0.09), ("ORPHAN_SWEEP_BACKOFF_MAX_SECONDS", 86400.5),
    ("ORPHAN_SWEEP_LOG_THROTTLE_SECONDS", 0.09), ("BACKGROUND_MIN_INTERVAL_SECONDS", 0.09),
    ("ORPHAN_SWEEP_BATCH_ROWS", 0), ("ORPHAN_SWEEP_PASS_SECONDS", 0), ("CANDIDATE_PAGE_ROWS", 0),
    ("CANDIDATE_MAX_SCANNED_ROWS", 0), ("CANDIDATE_SCAN_MAX_SECONDS", 0), ("CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS", 0)])
def test_every_constant_the_table_maps_to_sweep_config_invalid_is_a_pass_snapshot_member(monkeypatch, name, bad):
    monkeypatch.setattr(P, name, bad)
    with pytest.raises(ValueError):
        build_sweeper_pass_policy()


@pytest.mark.parametrize("name,bad", [
    ("BACKGROUND_MAX_RESTARTS", 0), ("BACKGROUND_RESTART_WINDOW_MAX", 0), ("BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP", 63),
    ("BACKGROUND_RESTART_BACKOFF_BASE_SECONDS", 0.09), ("BACKGROUND_RESTART_BACKOFF_CAP_SECONDS", 3600.5),
    ("BACKGROUND_STABLE_SECONDS", 0), ("BACKGROUND_RESTART_WINDOW_SECONDS", 0),
    ("BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS", 0.99), ("BACKGROUND_READY_TIMEOUT_SECONDS", 0.49),
    ("BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS", 0.49), ("ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS", 86400.5),
    ("ORPHAN_SWEEP_STARTUP_WAIT_SECONDS", 0), ("BACKGROUND_DB_CONCURRENCY", 9)])
def test_supervisor_ready_settle_fallback_startup_and_db_constants_are_not_pass_snapshot_members(monkeypatch, name, bad):
    monkeypatch.setattr(P, name, bad)
    build_sweeper_pass_policy()                                                  # the pass is unaffected: refused at construction only
    with pytest.raises(ValueError):
        make_runner()


# --- SUP-T T9, T10, T12, T27 to T30 as per-row matrices -------------------------------------------------------------------------
async def raising():
    raise RuntimeError("x")


@pytest.mark.parametrize("lifecycle", ["STOPPING", "STOP_INCOMPLETE"])
@pytest.mark.parametrize("kind", ["component", "waiter"])
@pytest.mark.parametrize("abnormal", [True, False])
@pytest.mark.parametrize("in_record", [True, False])
async def test_t9_t10_t28_t30_events_during_a_stop_never_restart_never_count_and_change_no_state(lifecycle, kind, abnormal, in_record):
    host = Host("RUNNING")
    sup, *_ = make(host=host)
    sup.ensure_component("sweeper", alive(), Origin.RECOVERY, 1)
    await settle_loop()
    unit = sup.units["sweeper"]
    task = asyncio.ensure_future(raising() if abnormal else asyncio.sleep(0))
    await asyncio.gather(task, return_exceptions=True)
    identity = 700 + (kind == "waiter")
    host.lifecycle = lifecycle
    if in_record:
        sup.settling.add(identity)
    ev = ExitEvent(5, 1, "sweeper", identity, kind == "waiter", 1.0, True, 0.0, not abnormal, task)
    label = sup.process_event(ev)
    assert label == ("T9/T17/T27/T28" if in_record else "T34")                      # one rule per cell (SUP-G 1 or 3)
    assert unit.consecutive == 0 and not unit.window and unit.waiter is None and unit.state is UnitState.RUNNING
    assert unit.last_failure is None
    await cleanup(sup)


@pytest.mark.parametrize("lifecycle", ["STOPPING", "STOP_INCOMPLETE", "STOPPED"])
@pytest.mark.parametrize("recorded", [True, False])
async def test_t27_t29_a_supervisor_exit_outside_running_and_starting_sets_no_flag_and_completes_nothing(lifecycle, recorded):
    host = Host("RUNNING")
    sup, _, _, _, failures = make(host=host)
    task = sup.create_supervisor(1)
    await settle_loop()
    if recorded:
        sup.record_settling(TaskCancelReason.STOP)
    host.lifecycle = lifecycle
    task.cancel()
    await settle_loop()
    assert sup.supervisor_degraded is False and failures == []


@pytest.mark.parametrize("lifecycle", ["STOPPING", "STOP_INCOMPLETE"])
async def test_t12_a_backoff_waiter_cancelled_by_the_stop_is_a_normal_end_in_either_stop_lifecycle(lifecycle):
    sup, host, clock, sleeper, _ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await drain(sup)
    unit = sup.units["sweeper"]
    sup.record_settling(TaskCancelReason.STOP)
    host.lifecycle = lifecycle
    unit.waiter.cancel()
    await settle_loop()
    labels = [sup.process_event(sup.queue.get_nowait()) for _ in range(sup.queue.qsize())]
    assert labels and all(label.startswith("T9") for label in labels)
    assert unit.consecutive == 1                                                 # what the real exit made it; the stop added nothing
