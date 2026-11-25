"""B2 phase 6 diagnostics (design 14.1, 14.2, 14.3, 6.2): the RUNNER_STATE causes (table DIAG-BG), the sweeper causes, the
PROCESS_TRACKING item, the TRACKED_EXECUTIONS aggregation and the cache key. The runtime snapshots of the DIAG-BG shapes are
built from the REAL background supervisor; the tracked-row evaluation itself is tested in `test_tracked_causes.py`."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import sweeper_stall as ss
from app.services.background_supervisor import CODE_BUDGET, DERIVED_EXITED_UNSUPERVISED, Origin, UnitState
from app.services.capability_reader import CapabilityReading
from app.services.diagnostics_service import (
    DiagnosticCause, DiagnosticsService, Status, TrackedVerdict, merge_causes,
)
from app.services.runner import RunnerLifecycle as L
from app.services.runtime_status import BackgroundComponentStatus, RunnerRuntimeState, StartupOutcome as S, runtime_snapshot, snapshot_of
from app.validation import policies as P
from tests.unit.test_background_supervisor import dying, make, settle_loop
from tests.unit.test_diagnostics_service import GOOD_PREFLIGHT, READY, Clock, FakeExecutor, FakeProjects, by_code, workspace
from tests.unit.test_runtime_background_status import (
    NOW, FakeRunner, case_a1, config_invalid_state, retrieve, stalled_state,
)


def ws(tmp_path):
    return workspace(tmp_path, uuid.uuid4().hex)


def service(runtime=READY, *, capability=None, tracked=None, clock=None):
    projects, ex = FakeProjects(), FakeExecutor()
    state = SimpleNamespace(runtime=runtime)
    svc = DiagnosticsService(projects, ex, protected=(), get_runtime=lambda: state.runtime,
                             get_preflight=lambda: GOOD_PREFLIGHT, clock=clock or Clock(),
                             read_capability=capability, read_tracked=tracked)
    return svc, projects, state


async def runner_item(tmp_path, runtime):
    svc, projects, _ = service(runtime)
    report = await svc.diagnose(projects.add(ws(tmp_path)).id)
    return by_code(report)["RUNNER_STATE"], report


def codes(item):
    return [(c.reason_code, c.severity) for c in item.causes]


# --- table DIAG-BG: the four response shapes -------------------------------------------------------------------------------
async def test_diag_bg_none_is_pass_with_no_cause(tmp_path):
    item, report = await runner_item(tmp_path, snapshot_of(FakeRunner(*make()[:2]), S.STARTED))
    assert item.status is Status.PASS and item.causes == () and not item.blocks_execution and report.can_start


async def test_diag_bg_case_a_derived_only_is_warn_with_one_derived_cause_and_never_blocks(tmp_path):
    sup, host = await case_a1()
    snapshot = snapshot_of(FakeRunner(sup, host), S.STARTED)
    item, report = await runner_item(tmp_path, snapshot)
    assert item.status is Status.WARN and item.blocks_execution is False
    assert codes(item) == [("BACKGROUND_COMPONENT_DERIVED", Status.WARN)]
    cause = item.causes[0]
    assert cause.blocks_execution is False and cause.components == (
        BackgroundComponentStatus("sweeper", None, None, False, DERIVED_EXITED_UNSUPERVISED),)
    assert snapshot.background_degraded is False and snapshot.blockers == () and report.can_start and not report.blockers


async def test_diag_bg_case_a2_a_waiterless_backoff_has_the_case_a_shape(tmp_path):
    sup, host, *_ = make()
    sup.units["sweeper"].state = UnitState.BACKOFF
    item, _ = await runner_item(tmp_path, snapshot_of(FakeRunner(sup, host), S.STARTED))
    assert item.status is Status.WARN and codes(item) == [("BACKGROUND_COMPONENT_DERIVED", Status.WARN)]
    assert item.causes[0].components[0].display_state == "BACKOFF_ORPHANED"


async def test_diag_bg_degraded_only_is_warn_and_not_blocking(tmp_path):
    sup, host, *_ = make()
    sup._degrade(sup.units["risk-worker"], CODE_BUDGET, exhausted=True, label="T5")
    snapshot = snapshot_of(FakeRunner(sup, host), S.STARTED)
    item, report = await runner_item(tmp_path, snapshot)
    assert item.status is Status.WARN and item.blocks_execution is False and report.can_start
    assert codes(item) == [("BACKGROUND_COMPONENT_DEGRADED", Status.WARN)]
    component = item.causes[0].components[0]
    assert (component.component, component.reason_code, component.restart_exhausted, component.display_state) == (
        "risk-worker", CODE_BUDGET, True, "DEGRADED") and component.since is not None
    assert "restart the Runner" in item.causes[0].recommendation and "risk-worker" in item.causes[0].cause


async def test_diag_bg_case_b_is_fail_and_blocking_with_two_causes_each_with_its_own_item(tmp_path):
    sup, host, *_ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await settle_loop()
    retrieve(sup)
    while not sup.queue.empty():
        sup.process_event(sup.queue.get_nowait())
    sup.units["sweeper"].waiter.cancel()
    await settle_loop()
    retrieve(sup)
    sup.supervisor_degraded, sup.supervisor_degraded_at = True, NOW
    snapshot = snapshot_of(FakeRunner(sup, host), S.STARTED)
    item, report = await runner_item(tmp_path, snapshot)
    assert item.status is Status.FAIL and item.blocks_execution is True and not report.can_start
    assert codes(item) == [("BACKGROUND_COMPONENT_DEGRADED", Status.FAIL), ("BACKGROUND_COMPONENT_DERIVED", Status.WARN)]
    degraded, derived = item.causes
    assert degraded.blocks_execution is True and [c.component for c in degraded.components] == ["supervisor"]
    assert derived.blocks_execution is False and [(c.component, c.display_state) for c in derived.components] == [
        ("sweeper", "BACKOFF_WAITER_DONE_UNSUPERVISED")]
    assert "RUNNER_STATE" in report.blockers and snapshot.blockers == ("SUPERVISOR_DEGRADED",)


async def test_a_derived_item_in_stopping_makes_no_cause_and_the_base_status_stays(tmp_path):
    sup, host = await case_a1()
    host.lifecycle = "STOPPING"
    item, _ = await runner_item(tmp_path, snapshot_of(FakeRunner(sup, host), S.STARTED))
    assert item.causes == () and item.status is Status.FAIL and "SHUTTING_DOWN" in item.cause          # the base FAIL only


async def test_a_higher_existing_severity_is_kept_and_the_base_warn_is_not_lowered(tmp_path):
    derived = BackgroundComponentStatus("sweeper", None, None, False, DERIVED_EXITED_UNSUPERVISED)
    lock_held = runtime_snapshot(L.STOPPED, S.ALREADY_ACTIVE, False)               # base WARN (ADVISORY_LOCK_HELD)
    assert lock_held.blockers == ("ADVISORY_LOCK_HELD",)
    item, _ = await runner_item(tmp_path, lock_held)
    assert item.status is Status.WARN and item.causes == ()                         # an API-only instance: no sweeper object
    stopped = runtime_snapshot(L.STOPPED, S.STARTED, False, RunnerRuntimeState(background_components=(derived,)))
    item2, _ = await runner_item(tmp_path, stopped)
    assert item2.status is Status.FAIL and item2.causes == ()                       # derived items never exist outside RUNNING


# --- the sweeper causes ----------------------------------------------------------------------------------------------------------
def with_sweeper(stall=None, config=None, **kw):
    return runtime_snapshot(L.RUNNING, S.STARTED, False, RunnerRuntimeState(
        sweeper_stall=stall.snapshot() if stall else None, sweeper_config_invalid=config.config_snapshot() if config else None, **kw))


async def test_a_stalled_sweeper_lifts_pass_to_warn_with_the_stall_object_and_changes_no_blocker(tmp_path):
    st = stalled_state()
    item, report = await runner_item(tmp_path, with_sweeper(st, st))
    assert item.status is Status.WARN and item.blocks_execution is False and report.can_start
    assert codes(item) == [("SWEEPER_STALLED", Status.WARN)]
    cause = item.causes[0]
    assert cause.sweeper == st.snapshot() and cause.config_invalid is None and cause.components == ()
    assert str(uuid.UUID(int=7)) in cause.cause and "1 failed passes" in cause.cause and "SWEEP_QUERY_FAILED" in cause.cause
    assert "never skips the row" in cause.recommendation


@pytest.mark.parametrize("valid_again,fragment", [(False, "configuration is invalid"), (True, "is valid now")])
async def test_the_configuration_cause_is_immediate_and_its_text_follows_config_currently_valid(tmp_path, valid_again, fragment):
    st = config_invalid_state(valid_again=valid_again)
    item, _ = await runner_item(tmp_path, with_sweeper(st, st))
    assert item.status is Status.WARN and codes(item) == [("SWEEPER_CONFIG_INVALID", Status.WARN)]
    cause = item.causes[0]
    assert fragment in cause.cause and cause.config_invalid is not None and cause.config_invalid.config_invalid_active
    assert cause.sweeper is not None and cause.sweeper.stalled is False and cause.sweeper.consecutive_failures >= 0
    if valid_again:
        assert "currently invalid" not in cause.cause and "wait for the sweeper pass" in cause.recommendation
    else:
        assert "correct the sweeper configuration" in cause.recommendation


async def test_stall_and_configuration_invalid_together_are_two_causes_ordered_by_code(tmp_path):
    st = stalled_state()
    st.record_config_invalid(pass_start=None, boundary_cursor=None, now=NOW, fallback_seconds=86400.0)
    item, _ = await runner_item(tmp_path, with_sweeper(st, st))
    assert [c for c, _ in codes(item)] == ["SWEEPER_CONFIG_INVALID", "SWEEPER_STALLED"]


async def test_a_healthy_default_sweeper_has_no_cause(tmp_path):
    item, _ = await runner_item(tmp_path, with_sweeper(ss.SweeperStallState(), ss.SweeperStallState()))
    assert item.status is Status.PASS and item.causes == ()


# --- merge and ordering (14.1) -------------------------------------------------------------------------------------------------
def cause(code, severity=Status.WARN, blocks=False, ids=(), requests=()):
    return DiagnosticCause(code, severity, blocks, code, "r", related_execution_ids=tuple(ids), related_request_ids=tuple(requests))


def test_duplicates_are_removed_with_their_ids_merged_ascending_and_the_order_is_severity_then_code():
    a, b, c = (uuid.UUID(int=i) for i in (3, 1, 2))
    merged = merge_causes([cause("LEASE_EXPIRED", ids=[a]), cause("TRACKED_EXECUTION_ANOMALY", Status.FAIL),
                           cause("CLOSED_UNPROVEN", ids=[c]), cause("LEASE_EXPIRED", ids=[b, a], blocks=False)])
    assert [m.reason_code for m in merged] == ["TRACKED_EXECUTION_ANOMALY", "CLOSED_UNPROVEN", "LEASE_EXPIRED"]
    assert merged[2].related_execution_ids == (b, a)                                # 1 then 3: ascending
    assert merge_causes([]) == ()


# --- PROCESS_TRACKING (6.2) ---------------------------------------------------------------------------------------------------
async def capability_item(tmp_path, reader):
    svc, projects, _ = service(capability=reader)
    return by_code(await svc.diagnose(projects.add(ws(tmp_path)).id))["PROCESS_TRACKING"]


def returns(reading):
    async def reader():
        return reading
    return reader


async def test_supported_is_pass_and_carries_no_blocks_features(tmp_path):
    item = await capability_item(tmp_path, returns(CapabilityReading("SUPPORTED")))
    assert item.status is Status.PASS and item.blocks_features == () and not item.blocks_execution


@pytest.mark.parametrize("state,status,blocks,code", [
    ("UNSUPPORTED_PLATFORM", Status.WARN, False, "PROCESS_TRACKING_UNSUPPORTED_PLATFORM"),
    ("UNAVAILABLE", Status.FAIL, True, "PROCESS_TRACKING_UNAVAILABLE"),
    ("STATUS_UNKNOWN", Status.UNKNOWN, False, "PROCESS_TRACKING_STATUS_UNKNOWN")])
async def test_each_non_supported_state_has_its_status_blocking_flag_code_and_blocks_features(tmp_path, state, status, blocks, code):
    item = await capability_item(tmp_path, returns(CapabilityReading(state, "SELF_TEST_FAILED")))
    assert item.status is status and item.blocks_execution is blocks and code in item.cause
    assert item.blocks_features == ("TEST_RUN",) and item.recommendation


async def test_an_unavailable_capability_blocks_the_project_diagnosis(tmp_path):
    svc, projects, _ = service(capability=returns(CapabilityReading("UNAVAILABLE")))
    report = await svc.diagnose(projects.add(ws(tmp_path)).id)
    assert not report.can_start and "PROCESS_TRACKING" in report.blockers


async def test_a_failing_a_hanging_and_a_missing_reader_are_unknown_never_an_exception(tmp_path, monkeypatch):
    async def failing():
        raise RuntimeError("secret SQL text")

    async def hanging():
        await asyncio.sleep(60)

    monkeypatch.setattr(P, "DIAGNOSTICS_ITEM_TIMEOUT_SECONDS", 0.05)
    for reader, word in ((failing, "could not be read"), (hanging, "could not be read"), (None, "not configured")):
        item = await capability_item(tmp_path, reader)
        assert item.status is Status.UNKNOWN and word in item.cause and "secret" not in item.cause
        assert item.blocks_features == ("TEST_RUN",) and not item.blocks_execution


# --- TRACKED_EXECUTIONS aggregation over an injected reader ---------------------------------------------------------------------
async def tracked_item(tmp_path, verdict):
    async def reader(project_id):
        return verdict
    svc, projects, _ = service(tracked=reader)
    return by_code(await svc.diagnose(projects.add(ws(tmp_path)).id))["TRACKED_EXECUTIONS"]


async def test_no_cause_is_pass_and_normal_progress_is_only_information(tmp_path):
    empty = await tracked_item(tmp_path, TrackedVerdict())
    assert empty.status is Status.PASS and empty.causes == () and not empty.blocks_execution
    busy = await tracked_item(tmp_path, TrackedVerdict((), 2))
    assert busy.status is Status.PASS and "2 tracked executions running" in busy.cause and busy.causes == ()


async def test_the_item_status_is_the_highest_severity_and_the_text_is_the_joined_cause_list(tmp_path):
    verdict = TrackedVerdict((cause("NO_OWNING_RUN"), cause("TRACKED_EXECUTION_ANOMALY", Status.FAIL), cause("LEASE_EXPIRED")), 1)
    item = await tracked_item(tmp_path, verdict)
    assert item.status is Status.FAIL and item.blocks_execution is False             # no 14.1 cause blocks execution
    assert [c.reason_code for c in item.causes] == ["TRACKED_EXECUTION_ANOMALY", "LEASE_EXPIRED", "NO_OWNING_RUN"]
    assert item.cause == "TRACKED_EXECUTION_ANOMALY; LEASE_EXPIRED; NO_OWNING_RUN; 1 tracked executions running"
    warn_only = await tracked_item(tmp_path, TrackedVerdict((cause("CLOSED_UNPROVEN"),)))
    assert warn_only.status is Status.WARN


async def test_an_unreadable_or_missing_tracked_reader_is_unknown(tmp_path):
    async def failing(project_id):
        raise RuntimeError("boom")

    for reader in (failing, None):
        svc, projects, _ = service(tracked=reader)
        item = by_code(await svc.diagnose(projects.add(ws(tmp_path)).id))[
            "TRACKED_EXECUTIONS"]
        assert item.status is Status.UNKNOWN and not item.blocks_execution


# --- the cache key (14.3) --------------------------------------------------------------------------------------------------------
async def test_the_policy_version_is_two_and_is_part_of_the_key(tmp_path, monkeypatch):
    assert P.DIAGNOSTICS_POLICY_VERSION == 2
    svc, projects, _ = service()
    pid = projects.add(ws(tmp_path)).id
    assert (await svc.diagnose(pid)).cached is False and (await svc.diagnose(pid)).cached is True
    monkeypatch.setattr(P, "DIAGNOSTICS_POLICY_VERSION", 3)
    assert (await svc.diagnose(pid)).cached is False


async def test_an_unchanged_state_keeps_the_key_inside_the_ttl(tmp_path):
    sup, host = await case_a1()
    runner = FakeRunner(sup, host)
    svc, projects, state = service(snapshot_of(runner, S.STARTED))
    pid = projects.add(ws(tmp_path)).id
    assert (await svc.diagnose(pid)).cached is False
    for _ in range(3):
        state.runtime = snapshot_of(runner, S.STARTED)                               # a fresh read of the same state
        assert (await svc.diagnose(pid)).cached is True


async def test_a_derived_value_that_appears_and_disappears_inside_the_ttl_is_visible_at_the_next_read(tmp_path):
    sup, host, *_ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    runner = FakeRunner(sup, host)
    svc, projects, state = service(snapshot_of(runner, S.STARTED))
    pid = projects.add(ws(tmp_path)).id
    first = by_code(await svc.diagnose(pid))["RUNNER_STATE"]
    assert first.status is Status.PASS                                               # the component task has not ended yet
    await settle_loop()                                                              # the task ends, the event is unprocessed
    retrieve(sup)
    state.runtime = snapshot_of(runner, S.STARTED)
    second_report = await svc.diagnose(pid)
    second = by_code(second_report)["RUNNER_STATE"]
    assert second_report.cached is False and second.status is Status.WARN
    assert codes(second) == [("BACKGROUND_COMPONENT_DERIVED", Status.WARN)]
    while not sup.queue.empty():                                                     # the supervisor processes the event
        sup.process_event(sup.queue.get_nowait())
    state.runtime = snapshot_of(runner, S.STARTED)
    third_report = await svc.diagnose(pid)
    assert third_report.cached is False and by_code(third_report)["RUNNER_STATE"].causes == ()   # BACKOFF with a waiter now
    assert (await svc.diagnose(pid)).cached is True                                  # and the unchanged set is a hit again


async def test_a_background_revision_change_misses_the_cache_at_once(tmp_path):
    svc, projects, state = service(runtime_snapshot(L.RUNNING, S.STARTED, False, RunnerRuntimeState(background_revision=0)))
    pid = projects.add(ws(tmp_path)).id
    await svc.diagnose(pid)
    assert (await svc.diagnose(pid)).cached is True
    state.runtime = runtime_snapshot(L.RUNNING, S.STARTED, False, RunnerRuntimeState(background_revision=1))
    assert (await svc.diagnose(pid)).cached is False


async def test_the_runner_state_item_is_computed_from_the_same_snapshot_as_the_key(tmp_path):
    """One read of the runtime per diagnosis: a second read between the key and the item would let them disagree."""
    reads = []
    snapshots = iter([with_sweeper(stalled_state(), stalled_state()), READY, READY])
    projects, ex = FakeProjects(), FakeExecutor()

    def get_runtime():
        reads.append(1)
        return next(snapshots)

    svc = DiagnosticsService(projects, ex, protected=(), get_runtime=get_runtime, get_preflight=lambda: GOOD_PREFLIGHT,
                             clock=Clock())
    report = await svc.diagnose(projects.add(ws(tmp_path)).id)
    assert len(reads) == 1 and by_code(report)["RUNNER_STATE"].status is Status.WARN


# --- stall / configuration-invalid: appear, are read at once, clear back to PASS, survive a stop/start of the same object ---------
async def test_a_stall_and_a_configuration_invalid_state_appear_at_the_next_read_and_clear_back_to_pass(tmp_path):
    """One REAL Runner object feeds `snapshot_of` before every read: the cache key moves with the real public state (the
    background revision), so the first read after each change shows it, and the recovery shows PASS again."""
    from tests.unit.test_runner_sweeper import make_runner
    r = make_runner()
    r._state = L.RUNNING
    r._stall = ss.SweeperStallState(threshold=1)
    svc, projects, state = service(snapshot_of(r, S.STARTED))
    pid = projects.add(ws(tmp_path)).id

    async def read():
        state.runtime = snapshot_of(r, S.STARTED)
        report = await svc.diagnose(pid)
        return by_code(report)["RUNNER_STATE"], report

    first, report = await read()
    assert first.status is Status.PASS and first.causes == () and report.cached is False
    again, report = await read()
    assert again.status is Status.PASS and report.cached is True                       # unchanged state: a hit
    r._stall.record_failed_pass(pass_start=None, position=uuid.UUID(int=7), kind=ss.CANDIDATE, code="SWEEP_QUERY_FAILED",
                                now=NOW, retry_delay_seconds=30)
    stalled, report = await read()                                                    # stall-only: PASS -> WARN, at the first read
    assert report.cached is False and stalled.status is Status.WARN and not stalled.blocks_execution
    assert [c for c, _ in codes(stalled)] == ["SWEEPER_STALLED"]
    r._stall.record_pass_without_failure(committed=None, advanced=False, wrapped=True, config_valid=True, now=NOW)
    healthy, report = await read()                                                    # the stall cleared: back to PASS
    assert report.cached is False and healthy.status is Status.PASS and healthy.causes == ()
    r._stall = ss.SweeperStallState()                                                  # a detection counts one failure: default threshold
    r._stall.record_config_invalid(pass_start=None, boundary_cursor=None, now=NOW, fallback_seconds=86400.0, reasons=("RELATION",))
    invalid, report = await read()
    assert report.cached is False and invalid.status is Status.WARN
    assert [c for c, _ in codes(invalid)] == ["SWEEPER_CONFIG_INVALID"]
    r._stall.record_pass_without_failure(committed=None, advanced=False, wrapped=True, config_valid=True, now=NOW)
    cleared, report = await read()
    assert report.cached is False and cleared.status is Status.PASS and cleared.causes == ()


async def test_the_stall_and_configuration_records_are_kept_by_a_stop_and_a_start_of_the_same_object_and_reset_with_a_new_one():
    """Row 31 / row 21 on the Runner object itself (the PostgreSQL start/stop variant is in test_runner_background)."""
    from tests.unit.test_runner_sweeper import make_runner
    r = make_runner()
    r._stall.record_failed_pass(pass_start=None, position=uuid.UUID(int=1), kind=ss.CANDIDATE, code="SWEEP_QUERY_FAILED",
                                now=NOW, retry_delay_seconds=30)
    r._stall.record_config_invalid(pass_start=None, boundary_cursor=None, now=NOW, fallback_seconds=86400.0)
    before = (r._stall.snapshot(), r._stall.config_snapshot())
    r._supervisor.begin_generation(2, lambda e: None)                                  # what start() does for a new generation
    r._supervisor.clear_flags()
    assert (r._stall.snapshot(), r._stall.config_snapshot()) == before
    assert make_runner()._stall.snapshot().consecutive_failures == 0                   # a new object starts clean
