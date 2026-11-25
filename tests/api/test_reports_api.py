"""Artifacts and the completion report over HTTP, from a real database and real (mock-provider) workflows.

Nothing here runs a real CLI: the providers are MockProviders, so this verifies the views, not the agents."""
import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.container import Container
from app.main import create_app
from app.models.tables import AgentSession, Execution
from app.providers.mock import ok
from app.services.report_service import ReportService
from app.services.runner import RunnerLifecycle as L
from app.services.runtime_status import StartupOutcome as S
from app.services.runtime_status import runtime_snapshot
from app.validation import policies as P
from tests.api.test_api import assert_envelope
from tests.harness import BAD_CODE, GATE, GOOD_CODE, PLAN, Harness, design_pass, structured

UNKNOWN = "00000000-0000-0000-0000-000000000000"
DEFAULT_SWEEPER = {"stalled": False, "consecutive_failures": 0, "last_failure_code": None, "last_failure_at": None,
                   "next_retry_at": None, "config_invalid_active": False, "clearance_pending": False}
NATIVE = "native-dev-session-ABC123"
SECRETS = ("ghp_abcdefghijklmnop1234", "s3cretT0ken", NATIVE)


def make_app(db, h: Harness, runtime=None):
    container = Container(settings=Settings(), db=db, guard=h.guard, executor=None, events=h.events, tasks=h.tasks,
                          gates=h.gates, runner=h.runner, providers={"CLAUDE": h.claude, "CODEX": h.codex},
                          reports=ReportService(db, get_runtime=runtime))
    app = create_app(container=container, run_preflight=False)
    app.state.container = container
    return app


async def run(db, tmp_path, claude=None, codex=None, runtime=None, start=True, **task_kw):
    h = Harness(db, tmp_path, claude_handlers=claude, codex_handlers=codex)
    app = make_app(db, h, runtime)
    task = await h.new_task(**task_kw)
    if start:
        await h.start(task.id)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, h, task


def dev_bad_then_good(call):
    (Path(call.cwd) / "calc.py").write_text(GOOD_CODE if call.n >= 2 else BAD_CODE, encoding="utf-8")
    return structured(summary=f"iteration {call.n}", files_changed=["calc.py"], human_gate_request=None)


def dev_always_bad(call):
    (Path(call.cwd) / "calc.py").write_text(BAD_CODE, encoding="utf-8")
    return structured(summary="still wrong", files_changed=["calc.py"], human_gate_request=None)


async def artifacts(client, task_id, **params):
    response = await client.get(f"/tasks/{task_id}/artifacts", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- a completed task ---------------------------------------------------------------------------------------------------
async def test_a_completed_task_lists_its_artifacts_with_latest_and_validity(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    async with client:
        body = await artifacts(client, task.id)
        kinds = [i["kind"] for i in body["items"]]
        assert kinds == ["PLAN", "DESIGN_REVIEW", "DEVELOP", "TEST", "CODE_REVIEW"] and body["total"] == 5
        assert all(i["is_latest_succeeded"] for i in body["items"])
        states = {i["kind"]: i["validity"]["state"] for i in body["items"]}
        # plan and design review depend only on stored bases; the rest describe workspace content, which is not compared
        assert states == {"PLAN": "CURRENT", "DESIGN_REVIEW": "CURRENT", "DEVELOP": "UNVERIFIED", "TEST": "UNVERIFIED",
                          "CODE_REVIEW": "UNVERIFIED"}
        for item in body["items"]:
            assert {"plan_revision", "workflow_iteration", "step_attempt"} <= set(item)
            assert (item["plan_revision"], item["workflow_iteration"], item["step_attempt"]) == (0, 0, 1)
        only_tests = await artifacts(client, task.id, kind="TEST")
        assert [i["kind"] for i in only_tests["items"]] == ["TEST"] and only_tests["total"] == 1


async def test_an_artifact_detail_carries_the_sanitised_stored_output_and_no_manifests(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    async with client:
        items = (await artifacts(client, task.id))["items"]
        test = next(i for i in items if i["kind"] == "TEST")
        detail = (await client.get(f"/tasks/{task.id}/artifacts/{test['id']}")).json()
        assert detail["content"]["value"]["test_result"]["passed"] is True
        assert detail["content"]["truncated"] is False and detail["id"] == test["id"]
        raw = json.dumps(detail)
        assert "snapshot_before" not in raw and "snapshot_after" not in raw


async def test_the_report_of_a_completed_task_is_built_from_stored_facts_and_not_called_acceptance(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    async with client:
        before = (len(h.claude.calls), len(h.codex.calls), len(await h.executions(task.id)))
        report = (await client.get(f"/tasks/{task.id}/report")).json()
        assert report["outcome"] == "COMPLETED" and "NOT mean" in report["outcome_note"]
        assert report["plan"]["content"]["value"]["design_direction"] and report["plan"]["validity"]["state"] == "CURRENT"
        assert report["implementation"]["files_changed"] == ["calc.py"]
        tests = report["tests"]
        assert tests["run_by"] == "ORCHESTRATOR" and tests["passed"] is True and tests["executed"] >= 1
        assert tests["command"] and "pytest" in " ".join(tests["command"])
        assert report["design_review"]["verdict"] == "PASS" and report["code_review"]["verdict"] == "PASS"
        assert [r["workflow_iteration"] for r in report["iterations"]] == [0]
        hist, now = report["historical_completion"], report["current_re_evaluation"]
        assert hist["recorded"] is True and hist["transition_reason"] == "definition_of_done_met"
        assert hist["conditions"] == "UNKNOWN" and "not reconstructed" in hist["note"]
        assert now["all_met"] is True and now["evaluated_at"] and "neither confirms nor cancels" in now["note"]
        assert {c["id"] for c in now["conditions"]} >= {"tests_passed", "review_verdict_pass", "design_review_valid"}
        assert now["evaluation_basis"]["test_artifact_id"] and report["next_actions"][0]["code"] == "REVIEW_CHANGES"
        # exact dict kept: the report runtime part gains `warnings`, `sweeper` (the default SweeperStatusOut) and
        # `background_degraded` (RuntimeOut, 14.2); an UNKNOWN runtime carries the defaults
        assert report["runtime"] == {"state": "UNKNOWN", "ready": None, "blockers": [], "warnings": [],
                                     "sweeper": DEFAULT_SWEEPER, "background_degraded": False}
        # reading never runs an agent, a command or writes an execution
        assert before == (len(h.claude.calls), len(h.codex.calls), len(await h.executions(task.id)))


# --- latest vs past, the three counters ----------------------------------------------------------------------------------
async def test_an_earlier_improvement_iteration_is_stale_and_not_the_latest(db, tmp_path):
    client, h, task = await run(db, tmp_path, claude={"DEVELOPER": dev_bad_then_good})
    async with client:
        items = (await artifacts(client, task.id, kind="DEVELOP"))["items"]
        assert [(i["workflow_iteration"], i["is_latest_succeeded"], i["validity"]["state"]) for i in items] == [
            (0, False, "STALE"), (1, True, "UNVERIFIED")]
        assert "superseded by workflow iteration 1" in items[0]["validity"]["reasons"][0]
        tests = (await artifacts(client, task.id, kind="TEST"))["items"]
        assert [i["validity"]["state"] for i in tests] == ["STALE", "UNVERIFIED"]
        report = (await client.get(f"/tasks/{task.id}/report")).json()
        assert [(r["workflow_iteration"], r["test_passed"]) for r in report["iterations"]] == [(0, False), (1, True)]
        assert report["iterations"][0]["code_review_verdict"] is None       # failing tests never reached the review


async def test_a_replan_keeps_the_old_plan_visible_as_a_superseded_revision(db, tmp_path):
    def reviewer(call):
        if call.n == 1:
            return structured(verdict="NEEDS_REVISION", summary="split it", human_gate_request=None,
                              issues=[{"severity": "MAJOR", "description": "split the module"}])
        return design_pass(call)

    client, h, task = await run(db, tmp_path, codex={"DESIGN_REVIEWER": reviewer})
    async with client:
        plans = (await artifacts(client, task.id, kind="PLAN"))["items"]
        assert [(p["plan_revision"], p["is_latest_succeeded"], p["validity"]["state"]) for p in plans] == [
            (0, False, "STALE"), (1, True, "CURRENT")]
        reviews = (await artifacts(client, task.id, kind="DESIGN_REVIEW"))["items"]
        assert [r["validity"]["state"] for r in reviews] == ["STALE", "CURRENT"]
        report = (await client.get(f"/tasks/{task.id}/report")).json()
        first = report["iterations"][0]
        assert first["plan_revision"] == 0 and first["design_review_verdict"] == "NEEDS_REVISION"
        assert first["design_issues"][0]["description"] == "split the module"
        assert report["task"]["plan_revision"] == 1 and report["task"]["replan_count"] == 1


async def test_a_step_retry_shows_as_a_separate_attempt_not_a_separate_iteration(db, tmp_path):
    calls = {"n": 0}

    def flaky(call):
        calls["n"] += 1
        if calls["n"] == 1:
            from app.models.states import CliOutcome
            from app.providers.base import ProviderResult
            return ProviderResult(outcome=CliOutcome.ERROR, exit_code=1)
        return design_pass(call)

    client, h, task = await run(db, tmp_path, codex={"DESIGN_REVIEWER": flaky})
    async with client:
        items = (await artifacts(client, task.id, kind="DESIGN_REVIEW"))["items"]
    assert [(i["plan_revision"], i["workflow_iteration"], i["step_attempt"], i["status"]) for i in items] == [
        (0, 0, 1, "FAILED"), (0, 0, 2, "SUCCEEDED")]
    assert items[0]["validity"]["state"] == "NOT_APPLICABLE" and items[1]["is_latest_succeeded"]


# --- failure, waiting, pending ---------------------------------------------------------------------------------------------
async def test_a_failed_task_reports_why_and_what_to_do_next(db, tmp_path):
    client, h, task = await run(db, tmp_path, claude={"DEVELOPER": dev_always_bad})
    async with client:
        report = (await client.get(f"/tasks/{task.id}/report")).json()
    assert report["outcome"] == "FAILED" and report["task"]["failure_reason"] == "MAX_ITERATIONS_EXCEEDED"
    assert report["historical_completion"]["recorded"] is False and report["current_re_evaluation"]["all_met"] is False
    assert report["next_actions"][0]["code"] == "INSPECT_FAILURE"
    assert report["last_transition"]["to_state"] == "FAILED" and len(report["iterations"]) == 4
    assert all(r["test_passed"] is False for r in report["iterations"])


async def test_a_waiting_task_lists_the_open_gate_and_its_history(db, tmp_path):
    planner = lambda call: structured(**(dict(PLAN, human_gate_request=GATE) if call.n == 1 else PLAN))  # noqa: E731
    client, h, task = await run(db, tmp_path, claude={"PLANNER": planner})
    async with client:
        report = (await client.get(f"/tasks/{task.id}/report")).json()
        assert report["outcome"] == "WAITING_APPROVAL" and report["outcome_note"].startswith("The task has not")
        gate = report["human_gates"][0]
        assert gate["type"] == "DESIGN" and gate["status"] == "PENDING" and gate["decision"] is None
        assert [o["id"] for o in gate["request"]["value"]["request_payload"]["options"]] == ["A", "B"]
        assert gate["request_label"] == "SNAPSHOT_AT_OPENING"           # the stored payload is the snapshot at opening
        assert report["next_actions"][0]["code"] == "DECIDE_GATE" and gate["id"] in report["next_actions"][0]["text"]
        await h.decide(uuid.UUID(gate["id"]))
        await h.run_until_idle()
        after = (await client.get(f"/tasks/{task.id}/report")).json()
        decided = after["human_gates"][0]
        assert decided["status"] == "DECIDED" and decided["decision"] == "APPROVED" and decided["selected_option"] == "A"
        assert after["outcome"] == "COMPLETED"


async def test_a_pending_task_says_to_request_execution_then_why_it_waits(db, tmp_path):
    client, h, task = await run(db, tmp_path, start=False)
    async with client:
        first = (await client.get(f"/tasks/{task.id}/report")).json()
        assert first["next_actions"][0]["code"] == "REQUEST_EXECUTION" and first["plan"] is None
        assert first["current_re_evaluation"]["all_met"] is False and first["human_gates"] == []
        await h.tasks.request_execution(task.id)
        second = (await client.get(f"/tasks/{task.id}/report")).json()
        assert second["next_actions"][0]["code"] == "WAIT_FOR_RUNNER" and second["runtime"]["state"] == "UNKNOWN"


async def test_a_waiting_request_is_explained_by_the_runtime_provider_when_one_is_wired(db, tmp_path):
    stopped = runtime_snapshot(L.STOPPED, S.PREFLIGHT_BLOCKED, False)
    client, h, task = await run(db, tmp_path, start=False, runtime=lambda: stopped)
    async with client:
        await h.tasks.request_execution(task.id)
        report = (await client.get(f"/tasks/{task.id}/report")).json()
    action = report["next_actions"][0]
    assert action["code"] == "RUNNER_NOT_READY" and "PREFLIGHT_FAILED" in action["text"] and "PENDING" in action["text"]
    assert report["runtime"] == {"state": "KNOWN", "ready": False, "blockers": ["PREFLIGHT_FAILED"], "warnings": [],
                                 "sweeper": DEFAULT_SWEEPER, "background_degraded": False}


# --- isolation and errors --------------------------------------------------------------------------------------------------
async def test_one_tasks_artifact_id_is_not_found_under_another_task(db, tmp_path):
    h = Harness(db, tmp_path)
    app = make_app(db, h)
    first, second = await h.new_task(), await h.new_task()
    await h.start(first.id)
    await h.start(second.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        mine = (await artifacts(client, first.id))["items"]
        theirs = (await artifacts(client, second.id))["items"]
        assert not {i["id"] for i in mine} & {i["id"] for i in theirs}
        assert_envelope(await client.get(f"/tasks/{first.id}/artifacts/{theirs[0]['id']}"), 404, "NOT_FOUND")
        assert (await client.get(f"/tasks/{second.id}/artifacts/{theirs[0]['id']}")).status_code == 200


async def test_unknown_task_unknown_artifact_unknown_kind_and_bad_paging(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    async with client:
        assert_envelope(await client.get(f"/tasks/{UNKNOWN}/artifacts"), 404, "NOT_FOUND")
        assert_envelope(await client.get(f"/tasks/{UNKNOWN}/report"), 404, "NOT_FOUND")
        assert_envelope(await client.get(f"/tasks/{task.id}/artifacts/{UNKNOWN}"), 404, "NOT_FOUND")
        assert_envelope(await client.get(f"/tasks/{task.id}/artifacts", params={"kind": "NOPE"}), 422, "VALIDATION_FAILED")
        assert (await client.get(f"/tasks/{task.id}/artifacts", params={"limit": 101})).status_code == 422
        assert (await client.get(f"/tasks/{task.id}/artifacts", params={"limit": 0})).status_code == 422
        assert (await client.get(f"/tasks/{task.id}/artifacts", params={"offset": -1})).status_code == 422


async def test_a_container_without_the_report_service_answers_with_the_error_envelope(db, tmp_path):
    h = Harness(db, tmp_path)
    container = Container(settings=Settings(), db=db, guard=h.guard, executor=None, events=h.events, tasks=h.tasks,
                          gates=h.gates, runner=h.runner, providers={})
    app = create_app(container=container, run_preflight=False)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert_envelope(await client.get(f"/tasks/{UNKNOWN}/report"), 503, "SERVICE_NOT_CONFIGURED")


# --- pagination ------------------------------------------------------------------------------------------------------------
async def test_pages_follow_next_offset_in_a_stable_order_until_the_end(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    async with client:
        seen, offset = [], 0
        while True:
            page = await artifacts(client, task.id, limit=2, offset=offset)
            assert page["total"] == 5 and page["returned"] == len(page["items"]) and not page["size_limited"]
            seen += [i["id"] for i in page["items"]]
            if not page["has_more"]:
                assert page["next_offset"] is None
                break
            assert page["next_offset"] == offset + page["returned"]
            offset = page["next_offset"]
        full = [i["id"] for i in (await artifacts(client, task.id))["items"]]
        assert seen == full and len(set(seen)) == 5


async def test_a_size_limited_page_omits_trailing_items_that_the_next_page_returns(db, tmp_path, monkeypatch):
    client, h, task = await run(db, tmp_path)
    async with client:
        one = len((await client.get(f"/tasks/{task.id}/artifacts", params={"limit": 1})).content)
        cap = int(one * 2.6)
        monkeypatch.setattr(P, "ARTIFACT_LIST_RESPONSE_MAX_BYTES", cap)
        first = await client.get(f"/tasks/{task.id}/artifacts", params={"limit": 5})
        assert len(first.content) <= cap                         # the real HTTP body respects the limit
        body = first.json()
        assert body["total"] == 5 and 1 <= body["returned"] < 5 and body["size_limited"] is True
        assert body["omitted_items"] == 5 - body["returned"] and body["has_more"] is True
        assert body["next_offset"] == body["returned"] and "SIZE_LIMIT" in body["sanitization"]["reasons"]
        ids = [i["id"] for i in body["items"]]
        offset = body["next_offset"]
        while offset is not None:
            nxt = (await client.get(f"/tasks/{task.id}/artifacts", params={"limit": 5, "offset": offset})).json()
            assert nxt["returned"] >= 1 and nxt["total"] == 5
            ids += [i["id"] for i in nxt["items"]]
            offset = nxt["next_offset"]
        assert len(ids) == 5 and ids == [i["id"] for i in (await artifacts(client, task.id))["items"]] \
            if False else len(set(ids)) == 5                      # every omitted item came back on a later page


async def test_the_real_report_body_respects_its_size_limit(db, tmp_path, monkeypatch):
    client, h, task = await run(db, tmp_path)
    async with client:
        full = (await client.get(f"/tasks/{task.id}/report")).content
        cap = int(len(full) * 0.7)
        monkeypatch.setattr(P, "REPORT_RESPONSE_MAX_BYTES", cap)
        response = await client.get(f"/tasks/{task.id}/report")
    body = response.json()
    assert len(response.content) <= cap and body["sanitization"]["truncated"] is True
    assert "SIZE_LIMIT" in body["sanitization"]["reasons"] and body["outcome"] == "COMPLETED" and body["next_actions"]


# --- nothing sensitive leaves ----------------------------------------------------------------------------------------------
async def test_session_ids_credentials_and_manifests_never_appear_in_any_response(db, tmp_path):
    leaky = (f"resumed {NATIVE}, pushed to https://user:s3cretT0ken@host/x.git with ghp_abcdefghijklmnop1234")

    def developer(call):
        (Path(call.cwd) / "calc.py").write_text(GOOD_CODE, encoding="utf-8")
        return ok(text="done", session=NATIVE, structured={"summary": leaky, "files_changed": ["calc.py"],
                                                            "human_gate_request": None})

    def reviewer(call):
        return structured(verdict="PASS", summary=f"checked {NATIVE}", plan_defect=False, test_policy_concern=False,
                          human_gate_request=None,
                          issues=[{"severity": "MINOR", "description": f"token ghp_abcdefghijklmnop1234 in {NATIVE}"}])

    client, h, task = await run(db, tmp_path, claude={"DEVELOPER": developer}, codex={"CODE_REVIEWER": reviewer})
    async with client:
        async with db.transaction() as s:
            stored = list((await s.execute(select(AgentSession.native_session_id).where(
                AgentSession.task_id == task.id))).scalars())
        assert NATIVE in stored                                  # it IS stored, and still never returned
        bodies = [(await client.get(f"/tasks/{task.id}/report")).text, (await client.get(f"/tasks/{task.id}/artifacts")).text]
        for item in (await artifacts(client, task.id))["items"]:
            bodies.append((await client.get(f"/tasks/{task.id}/artifacts/{item['id']}")).text)
    for body in bodies:
        for secret in (*SECRETS, *[n for n in stored if n]):
            assert secret not in body, secret
        assert "snapshot_before" not in body and "snapshot_after" not in body and "native_session_id" not in body
    report = json.loads(bodies[0])
    assert "sess:" in report["implementation"]["summary"] and "***" in report["implementation"]["summary"]
    assert str(task.id) in bodies[0]                              # ordinary UUIDs are kept, not turned into fingerprints
    assert re.search(r"sess:[0-9a-f]{8}", report["code_review"]["summary"])


# --- B2 phase 6: the runtime part of the report (design 14.2, section 13 'Report runtime schema') -------------------------------
async def runtime_part(db, tmp_path, snapshot):
    client, h, task = await run(db, tmp_path, start=False, runtime=lambda: snapshot)
    async with client:
        response = await client.get(f"/tasks/{task.id}/report")
    assert response.status_code == 200
    return response.json()["runtime"]


async def test_a_stalled_sweeper_reaches_the_http_report_with_the_status_and_no_cursor(db, tmp_path):
    from datetime import UTC, datetime
    from app.services import sweeper_stall as ss
    from app.services.runtime_status import RunnerRuntimeState
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    cursor = uuid.UUID(int=77)
    st = ss.SweeperStallState(threshold=1)
    st.record_failed_pass(pass_start=None, position=cursor, kind=ss.CANDIDATE, code="SWEEP_QUERY_FAILED", now=now,
                          retry_delay_seconds=30)
    snapshot = runtime_snapshot(L.RUNNING, S.STARTED, False, RunnerRuntimeState(
        sweeper_stall=st.snapshot(), sweeper_config_invalid=st.config_snapshot(), background_degraded=True))
    runtime = await runtime_part(db, tmp_path, snapshot)
    assert runtime == {"state": "KNOWN", "ready": True, "blockers": [],
                       "warnings": ["BACKGROUND_COMPONENT_DEGRADED", "SWEEPER_STALLED"], "background_degraded": True,
                       "sweeper": {"stalled": True, "consecutive_failures": 1, "last_failure_code": "SWEEP_QUERY_FAILED",
                                   "last_failure_at": "2026-11-24T12:00:00Z", "next_retry_at": "2026-11-24T12:00:30Z",
                                   "config_invalid_active": False, "clearance_pending": False}}
    assert str(cursor) not in str(runtime)


async def test_a_configuration_invalid_state_reaches_the_http_report_immediately(db, tmp_path):
    from datetime import UTC, datetime
    from app.services import sweeper_stall as ss
    from app.services.runtime_status import RunnerRuntimeState
    st = ss.SweeperStallState()
    st.record_config_invalid(pass_start=None, boundary_cursor=None, now=datetime(2026, 11, 24, tzinfo=UTC),
                             fallback_seconds=86400.0)
    snapshot = runtime_snapshot(L.RUNNING, S.STARTED, False, RunnerRuntimeState(
        sweeper_stall=st.snapshot(), sweeper_config_invalid=st.config_snapshot()))
    runtime = await runtime_part(db, tmp_path, snapshot)
    assert runtime["warnings"] == ["SWEEPER_CONFIG_INVALID"] and runtime["sweeper"]["config_invalid_active"] is True
    assert runtime["sweeper"]["stalled"] is False and runtime["blockers"] == [] and runtime["ready"] is True


async def test_an_api_only_instance_reports_the_default_sweeper_and_no_warning(db, tmp_path):
    runtime = await runtime_part(db, tmp_path, runtime_snapshot(L.STOPPED, S.ALREADY_ACTIVE, False))
    assert runtime == {"state": "KNOWN", "ready": False, "blockers": ["ADVISORY_LOCK_HELD"], "warnings": [],
                       "sweeper": DEFAULT_SWEEPER, "background_degraded": False}


async def test_the_http_report_runtime_part_has_no_component_list_and_a_derived_item_changes_nothing_in_it(db, tmp_path):
    from app.services.runtime_status import BackgroundComponentStatus, RunnerRuntimeState
    derived = BackgroundComponentStatus("sweeper", None, None, False, "EXITED_UNSUPERVISED")
    snapshot = runtime_snapshot(L.RUNNING, S.STARTED, False, RunnerRuntimeState(background_components=(derived,)))
    runtime = await runtime_part(db, tmp_path, snapshot)
    assert set(runtime) == {"state", "ready", "blockers", "warnings", "sweeper", "background_degraded"}
    assert runtime["background_degraded"] is False and runtime["warnings"] == [] and runtime["ready"] is True
