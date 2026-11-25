"""GET /projects/{id}/diagnostics and the extended GET /health, over HTTP against a real database."""
import httpx
import pytest

from app.config import Settings
from app.container import Container
from app.main import create_app
from app.providers.preflight import PreflightReport
from app.services.capability_reader import CapabilityReader
from app.services.diagnostics_service import DiagnosticsService
from app.services.project_service import ProjectService
from app.services.runtime_status import StartupOutcome
from app.services.tracked_diagnostics import TrackedDiagnosticsReader
from tests.api.test_api import assert_envelope
from tests.harness import Harness
from tests.unit.test_diagnostics_service import GOOD_PREFLIGHT, FakeExecutor

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"


def build(db, tmp_path, executor=None, with_diagnostics=True):
    h = Harness(db, tmp_path / "ws")
    projects = ProjectService(db, FakeExecutor(), clone_root=tmp_path / "clones", protected=(h.guard.root,))
    container = Container(settings=Settings(), db=db, guard=h.guard, executor=None, events=h.events, tasks=h.tasks,
                          gates=h.gates, runner=h.runner, providers={"CLAUDE": h.claude, "CODEX": h.codex},
                          projects=projects)
    container.preflight = GOOD_PREFLIGHT
    ex = executor or FakeExecutor()
    if with_diagnostics:
        capability = CapabilityReader(db, Settings().runner_advisory_lock_key, platform_supported=lambda: True)
        container.diagnostics = DiagnosticsService(projects, ex, protected=projects.protected,
                                                   get_runtime=container.runtime, get_preflight=lambda: container.preflight,
                                                   read_capability=capability,
                                                   read_tracked=TrackedDiagnosticsReader(db, read_capability=capability))
    app = create_app(container=container, run_preflight=False)
    app.state.container = container
    return container, app, h, ex


async def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def env(db, tmp_path):
    container, app, h, ex = build(db, tmp_path)
    (tmp_path / "projects").mkdir()
    async with await client_for(app) as client:
        yield client, container, h, ex, tmp_path / "projects"


async def register(client, root, name="svc"):
    path = root / name
    path.mkdir()
    created = await client.post("/projects", json={"name": name, "workspace_path": str(path)})
    assert created.status_code == 201
    return created.json()["id"]


# --- diagnostics -----------------------------------------------------------------------------------------------------
async def test_diagnostics_report_items_with_codes_status_time_cause_and_the_blocking_flag(env):
    client, container, _h, ex, root = env
    project_id = await register(client, root)
    container.startup_outcome = StartupOutcome.PREFLIGHT_BLOCKED          # the Runner never started
    response = await client.get(f"/projects/{project_id}/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project_id and body["cached"] is False and "guarantee" in body["disclaimer"]
    assert body["can_start"] is False and body["blockers"] == ["RUNNER_STATE"]
    items = {i["code"]: i for i in body["items"]}
    assert items["RUNNER_STATE"]["status"] == "FAIL" and items["RUNNER_STATE"]["blocks_execution"] is True
    assert "PREFLIGHT_FAILED" in items["RUNNER_STATE"]["cause"] and items["RUNNER_STATE"]["recommendation"]
    assert items["CLI_AUTH"]["status"] == "UNKNOWN" and items["WORKSPACE_ACCESS"]["status"] == "PASS"
    for item in body["items"]:
        # the exact key set is KEPT (never loosened): the original six keys plus the additive `causes` (14.1); the optional
        # `blocks_features` is present only on a PROCESS_TRACKING item that is not SUPPORTED (6.2)
        expected = {"code", "status", "checked_at", "cause", "recommendation", "blocks_execution", "causes"}
        if item["code"] == "PROCESS_TRACKING" and item["status"] != "PASS":
            expected |= {"blocks_features"}
        assert set(item) == expected
        assert item["status"] in {"PASS", "WARN", "FAIL", "UNKNOWN"}
    assert all(call[0] in ("git", "java") for call in ex.calls)            # nothing else was ever run


async def test_a_second_request_is_cached_and_refresh_recomputes(env):
    client, _c, _h, ex, root = env
    project_id = await register(client, root)
    first = (await client.get(f"/projects/{project_id}/diagnostics")).json()
    second = (await client.get(f"/projects/{project_id}/diagnostics")).json()
    refreshed = (await client.get(f"/projects/{project_id}/diagnostics", params={"refresh": "true"})).json()
    assert (first["cached"], second["cached"], refreshed["cached"]) == (False, True, False)
    assert len(ex.calls) == 2


async def test_an_unknown_project_is_a_404_and_a_bad_id_is_a_422(env):
    client, *_ = env
    assert_envelope(await client.get(f"/projects/{UNKNOWN_ID}/diagnostics"), 404, "NOT_FOUND")
    assert (await client.get("/projects/not-a-uuid/diagnostics")).status_code == 422


async def test_diagnostics_never_change_the_project(env):
    client, _c, _h, _ex, root = env
    project_id = await register(client, root)
    before = (await client.get(f"/projects/{project_id}")).json()
    await client.get(f"/projects/{project_id}/diagnostics")
    assert (await client.get(f"/projects/{project_id}")).json() == before


async def test_a_container_without_diagnostics_answers_with_the_error_envelope(db, tmp_path):
    _container, app, *_ = build(db, tmp_path, with_diagnostics=False)
    async with await client_for(app) as client:
        response = await client.get(f"/projects/{UNKNOWN_ID}/diagnostics")
    assert_envelope(response, 503, "SERVICE_NOT_CONFIGURED")


# --- /health ---------------------------------------------------------------------------------------------------------
async def test_health_keeps_its_original_keys_and_adds_readiness(env):
    client, container, *_ = env
    body = (await client.get("/health")).json()
    assert body["status"] == "ok" and body["runner_active"] is False       # the original contract
    assert body["ready"] is False
    # exact dict kept: the original keys plus `background_degraded` and `sweeper` (14.2); `warnings` is top-level and always present
    assert body["warnings"] == []
    assert body["runner"] == {"lifecycle": "STOPPED", "startup_outcome": "NOT_ATTEMPTED", "blockers": ["STOPPED"],
                              "background_degraded": False,
                              "sweeper": {"stalled": False, "consecutive_failures": 0, "last_failure_code": None, "last_failure_at": None,
                                         "next_retry_at": None, "config_invalid_active": False, "clearance_pending": False}}
    assert (await client.get("/health")).status_code == 200                # liveness: always 200 while the API answers


@pytest.mark.parametrize("outcome,blocker", [(StartupOutcome.PREFLIGHT_BLOCKED, "PREFLIGHT_FAILED"),
                                             (StartupOutcome.ALREADY_ACTIVE, "ADVISORY_LOCK_HELD")])
async def test_health_explains_why_the_runner_never_started(env, outcome, blocker):
    client, container, *_ = env
    container.startup_outcome = outcome
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["runner_active"] is False and body["ready"] is False and body["runner"]["blockers"] == [blocker]


async def test_runner_active_and_ready_follow_the_real_runner_lifecycle_including_after_a_stop(env):
    client, container, h, *_ = env
    await h.runner.start()
    try:
        container.startup_outcome = StartupOutcome.STARTED
        running = (await client.get("/health")).json()
        assert running["runner_active"] is True and running["ready"] is True and running["runner"]["blockers"] == []
        assert container.runner_active is True
    finally:
        await h.runner.stop()
    stopped = (await client.get("/health")).json()
    assert stopped["runner_active"] is False and stopped["ready"] is False        # no manual flag is left behind
    assert stopped["runner"]["lifecycle"] == "STOPPED" and stopped["runner"]["blockers"] == ["STOPPED"]


# =====================================================================================================================
# B2 phase 6 (design 14.1, 14.2, 14.3, 6.2): real HTTP, real database, the REAL Runner object of the harness
# =====================================================================================================================
import sys                                                                                      # noqa: E402
import uuid                                                                                     # noqa: E402
from datetime import UTC, datetime                                                              # noqa: E402

from sqlalchemy import text, update                                                             # noqa: E402

from app.models.tables import Task                                                              # noqa: E402
from app.schemas.reports import SweeperStatusOut                                                # noqa: E402
from app.services import sweeper_stall as ss                                                    # noqa: E402
from app.services.background_supervisor import CODE_BUDGET, UnitState                           # noqa: E402
from app.services.runner import RunnerLifecycle as L                                            # noqa: E402
from app.validation import policies as P                                                        # noqa: E402
from tests.helpers import make_task                                                             # noqa: E402
from tests.integration_pg.test_tracked_diagnostics import Held, add_row, publish                # noqa: E402

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
SWEEPER_CURSOR = uuid.UUID(int=7)


def item_of(body, code):
    return next(i for i in body["items"] if i["code"] == code)


def stall_runner(runner):
    runner._stall.threshold = 1
    runner._stall.record_failed_pass(pass_start=None, position=SWEEPER_CURSOR, kind=ss.CANDIDATE, code="SWEEP_QUERY_FAILED",
                                     now=NOW, retry_delay_seconds=30)


async def test_health_carries_the_warnings_the_degraded_flag_and_the_sweeper_object_with_exactly_the_status_keys(env):
    client, container, h, *_ = env
    body = (await client.get("/health")).json()
    assert set(body) == {"status", "runner_active", "ready", "warnings", "runner"} and body["warnings"] == []
    assert set(body["runner"]) == {"lifecycle", "startup_outcome", "blockers", "background_degraded", "sweeper"}
    assert set(body["runner"]["sweeper"]) == set(SweeperStatusOut.model_fields)
    assert body["runner"]["background_degraded"] is False


async def test_a_stalled_sweeper_is_a_health_warning_a_runner_state_cause_and_changes_no_readiness(env):
    client, container, h, _ex, root = env
    h.runner._state = L.RUNNING
    before = (await client.get("/health")).json()
    stall_runner(h.runner)
    health = (await client.get("/health")).json()
    assert health["warnings"] == ["SWEEPER_STALLED"] and health["runner"]["sweeper"]["stalled"] is True
    assert health["runner"]["sweeper"]["last_failure_code"] == "SWEEP_QUERY_FAILED"
    assert (health["runner_active"], health["ready"], health["runner"]["blockers"]) == (
        before["runner_active"], before["ready"], before["runner"]["blockers"])
    assert str(SWEEPER_CURSOR) not in str(health)                                  # no cursor in /health
    project_id = await register(client, root)
    item = item_of((await client.get(f"/projects/{project_id}/diagnostics")).json(), "RUNNER_STATE")
    assert item["status"] == "WARN" and item["blocks_execution"] is False
    cause = item["causes"][0]
    assert cause["reason_code"] == "SWEEPER_STALLED" and cause["severity"] == "WARN" and cause["blocks_execution"] is False
    assert cause["sweeper"]["stalled"] is True and cause["sweeper"]["current_failure_cursor"] == str(SWEEPER_CURSOR)
    assert cause["config_invalid"] is None and cause["components"] == [] and cause["anomalies"] == []


async def test_the_configuration_invalid_state_is_immediate_in_health_and_in_the_diagnostics_cause(env):
    client, container, h, _ex, root = env
    h.runner._state = L.RUNNING
    h.runner._stall.record_config_invalid(pass_start=None, boundary_cursor=None, now=NOW, fallback_seconds=86400.0,
                                          reasons=("RELATION",))
    health = (await client.get("/health")).json()
    assert health["warnings"] == ["SWEEPER_CONFIG_INVALID"] and health["runner"]["sweeper"]["config_invalid_active"] is True
    assert health["runner"]["sweeper"]["stalled"] is False and health["runner"]["blockers"] == []
    project_id = await register(client, root)
    item = item_of((await client.get(f"/projects/{project_id}/diagnostics")).json(), "RUNNER_STATE")
    cause = item["causes"][0]
    assert cause["reason_code"] == "SWEEPER_CONFIG_INVALID" and cause["sweeper"]["stalled"] is False
    assert cause["config_invalid"]["active"] is True and cause["config_invalid"]["reasons"] == ["RELATION"]
    assert set(cause["config_invalid"]) == {"active", "config_currently_valid", "config_evaluated_at", "clearance_pending",
                                            "detected_at", "trigger_position", "next_retry_at", "next_recheck_at", "reasons"}


async def test_the_diag_bg_shapes_over_http_case_a2_degraded_only_and_case_b(env):
    client, container, h, _ex, root = env
    h.runner._state = L.RUNNING
    project_id = await register(client, root)
    sup = h.runner._supervisor
    url = f"/projects/{project_id}/diagnostics"
    # case A2: a waiter-less BACKOFF unit with a live supervisor (only fault injection builds it)
    sup.units["sweeper"].state = UnitState.BACKOFF
    health = (await client.get("/health")).json()
    assert health["runner"]["background_degraded"] is False and health["warnings"] == [] and health["ready"] is True
    body = (await client.get(url, params={"refresh": "true"})).json()
    runner_item = item_of(body, "RUNNER_STATE")
    assert runner_item["status"] == "WARN" and runner_item["blocks_execution"] is False and body["can_start"] is True
    [cause] = runner_item["causes"]
    assert cause["reason_code"] == "BACKGROUND_COMPONENT_DERIVED"
    assert cause["components"] == [{"component": "sweeper", "reason_code": None, "since": None, "restart_exhausted": False,
                                    "display_state": "BACKOFF_ORPHANED"}]
    # DEGRADED-only (a component, the supervisor alive)
    sup.units["sweeper"].state = UnitState.IDLE
    sup._degrade(sup.units["risk-worker"], CODE_BUDGET, exhausted=True, label="T5")
    health = (await client.get("/health")).json()
    assert health["runner"]["background_degraded"] is True and health["warnings"] == ["BACKGROUND_COMPONENT_DEGRADED"]
    assert health["ready"] is True and health["runner"]["blockers"] == []
    runner_item = item_of((await client.get(url, params={"refresh": "true"})).json(), "RUNNER_STATE")
    assert runner_item["status"] == "WARN" and runner_item["blocks_execution"] is False
    [cause] = runner_item["causes"]
    assert cause["reason_code"] == "BACKGROUND_COMPONENT_DEGRADED" and cause["severity"] == "WARN"
    assert cause["components"][0]["component"] == "risk-worker" and cause["components"][0]["restart_exhausted"] is True
    assert cause["components"][0]["reason_code"] == CODE_BUDGET and cause["components"][0]["since"]
    # case B: the supervisor DEGRADED and a derived item
    sup.units["capability"].state = UnitState.BACKOFF
    sup.supervisor_degraded, sup.supervisor_degraded_at = True, NOW
    health = (await client.get("/health")).json()
    assert health["runner"]["background_degraded"] is True and health["ready"] is False
    assert health["runner"]["blockers"] == ["SUPERVISOR_DEGRADED"] and health["runner_active"] is True
    assert "BACKGROUND_COMPONENT_DEGRADED" in health["warnings"]
    body = (await client.get(url, params={"refresh": "true"})).json()
    runner_item = item_of(body, "RUNNER_STATE")
    assert runner_item["status"] == "FAIL" and runner_item["blocks_execution"] is True and body["can_start"] is False
    causes = {c["reason_code"]: c for c in runner_item["causes"]}
    assert set(causes) == {"BACKGROUND_COMPONENT_DEGRADED", "BACKGROUND_COMPONENT_DERIVED"}
    assert causes["BACKGROUND_COMPONENT_DEGRADED"]["severity"] == "FAIL" and causes["BACKGROUND_COMPONENT_DEGRADED"]["blocks_execution"]
    assert causes["BACKGROUND_COMPONENT_DERIVED"]["severity"] == "WARN" and not causes["BACKGROUND_COMPONENT_DERIVED"]["blocks_execution"]
    assert {c["component"] for c in causes["BACKGROUND_COMPONENT_DEGRADED"]["components"]} == {"supervisor", "risk-worker"}
    assert [c["display_state"] for c in causes["BACKGROUND_COMPONENT_DERIVED"]["components"]] == ["BACKOFF_ORPHANED"]


async def test_the_same_state_outside_running_makes_no_derived_warning_and_no_cause(env):
    client, container, h, _ex, root = env
    project_id = await register(client, root)
    h.runner._supervisor.units["sweeper"].state = UnitState.BACKOFF
    for lifecycle in (L.STARTING, L.STOPPING, L.STOP_INCOMPLETE, L.STOPPED):
        h.runner._state = lifecycle
        health = (await client.get("/health")).json()
        assert health["warnings"] == [] and health["runner"]["background_degraded"] is False
        item = item_of((await client.get(f"/projects/{project_id}/diagnostics", params={"refresh": "true"})).json(),
                       "RUNNER_STATE")
        assert [c["reason_code"] for c in item["causes"]] == []


async def test_a_derived_value_changing_inside_the_ttl_is_visible_at_the_next_read_and_an_unchanged_set_is_a_cache_hit(env):
    client, container, h, ex, root = env
    h.runner._state = L.RUNNING
    project_id = await register(client, root)
    url = f"/projects/{project_id}/diagnostics"
    first = (await client.get(url)).json()
    assert first["cached"] is False and item_of(first, "RUNNER_STATE")["causes"] == []
    assert (await client.get(url)).json()["cached"] is True
    h.runner._supervisor.units["sweeper"].state = UnitState.BACKOFF                  # a derived value appears (no refresh)
    second = (await client.get(url)).json()
    assert second["cached"] is False
    assert [c["reason_code"] for c in item_of(second, "RUNNER_STATE")["causes"]] == ["BACKGROUND_COMPONENT_DERIVED"]
    assert (await client.get(url)).json()["cached"] is True                           # the unchanged set keeps the key
    h.runner._supervisor.units["sweeper"].state = UnitState.IDLE                     # it disappears again
    third = (await client.get(url)).json()
    assert third["cached"] is False and item_of(third, "RUNNER_STATE")["causes"] == []


async def test_a_stall_and_its_clearance_are_visible_at_once_without_waiting_for_the_ttl(env):
    client, container, h, ex, root = env
    h.runner._state = L.RUNNING
    project_id = await register(client, root)
    url = f"/projects/{project_id}/diagnostics"
    await client.get(url)
    stall_runner(h.runner)
    stalled = (await client.get(url)).json()
    assert stalled["cached"] is False and item_of(stalled, "RUNNER_STATE")["status"] == "WARN"
    assert (await client.get(url)).json()["cached"] is True
    h.runner._stall.reset()
    cleared = (await client.get(url)).json()
    assert cleared["cached"] is False and item_of(cleared, "RUNNER_STATE")["status"] == "PASS"


# --- PROCESS_TRACKING over HTTP -----------------------------------------------------------------------------------------------
async def test_a_real_runner_publishes_the_capability_and_another_instance_reads_it_through_the_trust_rule(env, db):
    if sys.platform != "win32":
        pytest.skip("the real tracking self-test runs on Windows only (design 5.2)")
    client, container, h, _ex, root = env
    project_id = await register(client, root)
    await h.runner.start()
    try:
        container.startup_outcome = StartupOutcome.STARTED
        # this API instance has no Runner of its own: it applies the trust rule against the real pg_locks
        container.diagnostics._read_capability = CapabilityReader(db, 987_654_321, platform_supported=lambda: True)
        body = (await client.get(f"/projects/{project_id}/diagnostics", params={"refresh": "true"})).json()
        item = item_of(body, "PROCESS_TRACKING")
        assert item["status"] == "PASS" and "blocks_features" not in item
    finally:
        await h.runner.stop()
    body = (await client.get(f"/projects/{project_id}/diagnostics", params={"refresh": "true"})).json()
    after = item_of(body, "PROCESS_TRACKING")
    assert after["status"] == "UNKNOWN" and after["blocks_features"] == ["TEST_RUN"] and "STATUS_UNKNOWN" in after["cause"]


@pytest.mark.parametrize("state,status,blocks,code", [
    ("UNSUPPORTED_PLATFORM", "WARN", False, "PROCESS_TRACKING_UNSUPPORTED_PLATFORM"),
    ("UNAVAILABLE", "FAIL", True, "PROCESS_TRACKING_UNAVAILABLE")])
async def test_the_process_tracking_item_per_capability_state_over_http(env, db, state, status, blocks, code):
    client, container, h, _ex, root = env
    project_id = await register(client, root)
    async with Held(db, Settings().runner_advisory_lock_key) as held:
        await publish(db, state=state, pid=held.pid, reason="SELF_TEST_FAILED")
        body = (await client.get(f"/projects/{project_id}/diagnostics", params={"refresh": "true"})).json()
    item = item_of(body, "PROCESS_TRACKING")
    assert item["status"] == status and item["blocks_execution"] is blocks and code in item["cause"]
    assert item["blocks_features"] == ["TEST_RUN"]
    assert ("PROCESS_TRACKING" in body["blockers"]) is blocks


# --- TRACKED_EXECUTIONS over HTTP ------------------------------------------------------------------------------------------------
async def attach_task(db, project_id, *, status="FAILED"):
    task = await make_task(db)
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == task.id).values(project_id=uuid.UUID(project_id), status=status))
    return task


async def test_the_tracked_executions_item_over_http_lists_every_cause_and_a_bounded_anomaly_list_without_raw_text(env, db):
    client, container, h, _ex, root = env
    project_id = await register(client, root)
    marker = "ghp_abcdefghijklmnop1234 SECRET-MARKER"
    task = await attach_task(db, project_id)
    open_row = await add_row(db, task, None)
    bad_detail = '{"tracked": false, "note": "' + marker + '"}'
    bad = [await add_row(db, task, None, detail=bad_detail, finished=True) for _ in range(3)]
    body = (await client.get(f"/projects/{project_id}/diagnostics", params={"refresh": "true"})).json()
    item = item_of(body, "TRACKED_EXECUTIONS")
    assert item["status"] == "FAIL" and item["blocks_execution"] is False and "TRACKED_EXECUTIONS" not in body["blockers"]
    assert [c["reason_code"] for c in item["causes"]] == ["TRACKED_EXECUTION_ANOMALY", "TASK_TERMINAL_WITH_UNRESOLVED"]
    anomaly, terminal = item["causes"]
    assert sorted(a["execution_id"] for a in anomaly["anomalies"]) == sorted(str(b) for b in bad)
    assert anomaly["anomalies_returned"] == 3 and anomaly["anomalies_total"] == 3 and anomaly["anomalies_total_is_exact"] is True
    assert anomaly["anomalies_truncated"] is False and anomaly["may_be_permanently_stuck"] is False
    assert set(anomaly["anomalies"][0]) == {"execution_id", "task_id", "project_id", "anomaly_kind", "sub_reason"}
    assert sorted(terminal["related_execution_ids"]) == sorted(str(i) for i in [open_row, *bad]) and terminal["anomalies"] == []
    assert "SECRET-MARKER" not in str(body) and "ghp_" not in str(body) and "'detail'" not in str(body)
    assert item["cause"] == "; ".join(c["cause"] for c in item["causes"])                         # the joined list


async def test_the_anomaly_list_is_bounded_by_the_constant_and_marks_the_truncation(env, db):
    client, container, h, _ex, root = env
    project_id = await register(client, root)
    task = await attach_task(db, project_id)
    async with db.transaction() as s:
        await s.execute(text(
            "INSERT INTO executions (id, task_id, role, provider, outcome, pid, started_at, finished_at, detail) "
            "SELECT gen_random_uuid(), :t, 'TESTER', 'CLAUDE', 'STARTING', 1, now(), now(), "
            "CAST(:d AS jsonb) FROM generate_series(1, :n)"),
            {"t": task.id, "d": '{"tracked": false}', "n": P.DIAGNOSTIC_MAX_ANOMALIES + 5})
    body = (await client.get(f"/projects/{project_id}/diagnostics", params={"refresh": "true"})).json()
    anomaly = next(c for c in item_of(body, "TRACKED_EXECUTIONS")["causes"] if c["reason_code"] == "TRACKED_EXECUTION_ANOMALY")
    assert len(anomaly["anomalies"]) == anomaly["anomalies_returned"] == P.DIAGNOSTIC_MAX_ANOMALIES
    assert anomaly["anomalies_truncated"] is True and anomaly["anomalies_total"] is None
    assert anomaly["anomalies_total_is_exact"] is False and len(anomaly["related_execution_ids"]) <= P.DIAGNOSTIC_MAX_RELATED_IDS


async def test_a_clean_project_has_a_pass_tracked_item_and_a_stranger_projects_rows_never_appear(env, db):
    client, container, h, _ex, root = env
    mine = await register(client, root, "mine")
    other = await register(client, root, "other")
    task = await attach_task(db, other)
    await add_row(db, task, None)
    body = (await client.get(f"/projects/{mine}/diagnostics")).json()
    item = item_of(body, "TRACKED_EXECUTIONS")
    assert item["status"] == "PASS" and item["causes"] == []


async def test_the_diagnostics_are_read_only_for_the_tracked_rows(env, db):
    client, container, h, _ex, root = env
    project_id = await register(client, root)
    task = await attach_task(db, project_id)
    await add_row(db, task, None)

    async def snapshot():
        async with db.transaction() as s:
            return (await s.execute(text("SELECT id::text, detail::text, finished_at, outcome FROM executions ORDER BY id"))).all()
    before = await snapshot()
    await client.get(f"/projects/{project_id}/diagnostics", params={"refresh": "true"})
    assert await snapshot() == before
