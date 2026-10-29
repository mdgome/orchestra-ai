import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import Container
from app.infra.event_bus import EventBus
from app.main import create_app
from app.routers import events as events_router
from tests.harness import GATE, PLAN, Harness, structured


def build_container(h: Harness) -> Container:
    return Container(settings=Settings(), db=h.db, guard=h.guard, executor=None, events=h.events, tasks=h.tasks,
                     gates=h.gates, runner=h.runner, providers={"CLAUDE": h.claude, "CODEX": h.codex})


@pytest.fixture
async def api(db, tmp_path):
    h = Harness(db, tmp_path)
    app = create_app(container=build_container(h), run_preflight=False)
    app.state.container = build_container(h)      # ASGITransport does not run lifespan: wire manually
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, h


def assert_envelope(resp, status, code=None):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}
    if code:
        assert body["error"]["code"] == code
    return body["error"]


async def test_full_flow_over_http(api):
    client, h = api
    conv = (await client.post("/conversations")).json()
    assert (await client.post(f"/conversations/{conv['id']}/messages",
                              json={"role": "USER", "content": "add two numbers"})).status_code == 201

    created = await client.post("/tasks", json={"requirements": "implement add(a, b)", "conversation_id": conv["id"]})
    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "PENDING" and h.claude.calls == []             # creating is not executing

    queued = await client.post(f"/tasks/{task['id']}/execution-requests")
    assert queued.status_code == 202 and queued.json()["status"] == "PENDING" and h.claude.calls == []
    again = await client.post(f"/tasks/{task['id']}/execution-requests")
    assert again.status_code == 202 and again.json()["id"] == queued.json()["id"]   # the existing request

    assert await h.run_until_idle() == ["COMPLETED"]                         # the Runner, not the HTTP request
    detail = (await client.get(f"/tasks/{task['id']}")).json()
    assert detail["status"] == "COMPLETED" and detail["lease"] is None
    assert detail["checkpoint"]["step_key"] == "CODE_REVIEW" and detail["checkpoint"]["status"] == "SUCCEEDED"
    assert detail["pending_approvals"] == []

    execs = (await client.get(f"/tasks/{task['id']}/executions")).json()
    assert {e["role"] for e in execs} >= {"PLANNER", "DEVELOPER", "TESTER", "DESIGN_REVIEWER", "CODE_REVIEWER"}
    transitions = (await client.get(f"/tasks/{task['id']}/state-transitions")).json()
    assert [t["to_state"] for t in transitions if t["kind"] == "TASK_STATE"][-1] == "COMPLETED"
    assert (await client.get(f"/tasks/{task['id']}/approvals")).json() == []
    assert (await client.get("/tasks", params={"status": "COMPLETED", "limit": 10})).json()[0]["id"] == task["id"]


async def test_native_session_ids_never_leave_the_api(api):
    client, h = api
    task = (await client.post("/tasks", json={"requirements": "implement add(a, b)"})).json()
    await client.post(f"/tasks/{task['id']}/execution-requests")
    await h.run_until_idle()
    texts = [(await client.get(p)).text for p in (
        f"/tasks/{task['id']}", f"/tasks/{task['id']}/executions", f"/tasks/{task['id']}/state-transitions",
        f"/tasks/{task['id']}/approvals")]
    secrets = h.claude.live_ids | h.codex.live_ids
    assert secrets and not any(s in t for s in secrets for t in texts)


async def test_error_responses_share_one_envelope(api):
    client, _ = api
    assert_envelope(await client.post("/tasks", json={"requirements": "x"}), 422, "VALIDATION_FAILED")  # too short
    assert_envelope(await client.post("/tasks", json={}), 422, "VALIDATION_FAILED")                    # missing field
    assert_envelope(await client.post("/tasks", content="{not json", headers={"content-type": "application/json"}),
                    422)
    assert_envelope(await client.get("/tasks/not-a-uuid"), 422, "VALIDATION_FAILED")
    assert_envelope(await client.get(f"/tasks/{uuid.uuid4()}"), 404, "NOT_FOUND")
    assert_envelope(await client.get(f"/approvals/{uuid.uuid4()}"), 404, "NOT_FOUND")
    assert_envelope(await client.get("/conversations/" + str(uuid.uuid4())), 404, "NOT_FOUND")
    assert_envelope(await client.get("/no-such-route"), 404)
    assert_envelope(await client.post("/tasks", json={"requirements": "implement add",
                                                       "limits": {"max_replans": 99}}), 422, "VALIDATION_FAILED")
    assert_envelope(await client.post("/tasks", json={"requirements": "implement add",
                                                       "test_policy": {"commands": ["pytest -q"]}}), 422)


async def test_state_conflicts_are_409(api):
    client, h = api
    task = (await client.post("/tasks", json={"requirements": "implement add(a, b)"})).json()
    assert (await client.patch(f"/tasks/{task['id']}", json={"requirements": "implement sub(a, b)"})).status_code == 200
    await client.post(f"/tasks/{task['id']}/execution-requests")
    await h.run_until_idle()
    assert_envelope(await client.patch(f"/tasks/{task['id']}", json={"requirements": "implement mul"}), 409)
    assert_envelope(await client.post(f"/tasks/{task['id']}/execution-requests"), 409)    # already finished
    assert_envelope(await client.post(f"/tasks/{task['id']}/cancellation-requests"), 409)


async def test_cancellation_request_only_records_the_request(api):
    client, h = api
    task = (await client.post("/tasks", json={"requirements": "implement add(a, b)"})).json()
    resp = await client.post(f"/tasks/{task['id']}/cancellation-requests")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "PENDING" and body["cancel_requested_at"] is not None   # not CANCELLED by the HTTP call


async def test_approval_decision_idempotency_over_http(api):
    client, h = api
    h.claude.handlers["PLANNER"] = lambda call: structured(**(dict(PLAN, human_gate_request=GATE) if call.n == 1 else PLAN))
    task = (await client.post("/tasks", json={"requirements": "implement add(a, b)"})).json()
    await client.post(f"/tasks/{task['id']}/execution-requests")
    assert await h.run_until_idle() == ["WAITING_APPROVAL"]

    detail = (await client.get(f"/tasks/{task['id']}")).json()
    assert detail["status"] == "WAITING_APPROVAL" and detail["lease"] is None and len(detail["pending_approvals"]) == 1
    approval_id = detail["pending_approvals"][0]
    approval = (await client.get(f"/approvals/{approval_id}")).json()
    assert approval["type"] == "DESIGN" and approval["status"] == "PENDING"
    assert [o["id"] for o in approval["request_payload"]["options"]] == ["A", "B"]

    url = f"/approvals/{approval_id}/decisions"
    body = {"decision": "APPROVED", "selected_option": "A", "rationale": "simplest"}
    assert_envelope(await client.post(url, json=body), 422, "VALIDATION_FAILED")        # Idempotency-Key is required
    first = await client.post(url, json=body, headers={"Idempotency-Key": "k-1"})
    assert first.status_code == 201
    replay = await client.post(url, json=body, headers={"Idempotency-Key": "k-1"})
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    other_key = await client.post(url, json=body, headers={"Idempotency-Key": "k-2"})
    assert other_key.status_code == 200 and other_key.json()["id"] == first.json()["id"]
    assert_envelope(await client.post(url, json={**body, "selected_option": "B"}, headers={"Idempotency-Key": "k-3"}),
                    409, "CONFLICT")
    assert_envelope(await client.post(url, json={"decision": "REJECTED"}, headers={"Idempotency-Key": "k-1"}),
                    409, "IDEMPOTENCY_KEY_CONFLICT")
    assert_envelope(await client.post(url, json={"decision": "MAYBE"}, headers={"Idempotency-Key": "k-9"}), 422)
    assert_envelope(await client.post(f"/approvals/{uuid.uuid4()}/decisions", json=body,
                                      headers={"Idempotency-Key": "k"}), 404)

    # the decision stored a RESUME request in the same transaction: the Runner continues without any further call
    assert await h.run_until_idle() == ["COMPLETED"]
    assert (await client.get(f"/tasks/{task['id']}")).json()["status"] == "COMPLETED"


async def test_message_on_a_closed_conversation_is_409(api):
    client, h = api
    conv = (await client.post("/conversations")).json()
    await h.tasks.close_conversation(uuid.UUID(conv["id"]))
    assert_envelope(await client.post(f"/conversations/{conv['id']}/messages", json={"content": "late"}), 409)


async def test_unhandled_errors_are_wrapped_without_leaking_details(db, tmp_path):
    h = Harness(db, tmp_path)
    c = build_container(h)

    async def boom(*a, **k):
        raise RuntimeError("secret internals /etc/passwd")
    c.tasks.get_task = boom
    app = create_app(container=c, run_preflight=False)
    app.state.container = c
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://test") as client:
        resp = await client.patch(f"/tasks/{uuid.uuid4()}", json={"requirements": "implement x"})
        # patch uses update_task; call a route that hits get_task through the container
        resp = await client.get(f"/tasks/{uuid.uuid4()}/executions")
    err = assert_envelope(resp, 500, "INTERNAL_ERROR")
    assert "passwd" not in resp.text and err["message"] == "internal server error"


# ---- WebSocket (stub container: the handler only needs the event bus and the task snapshot) ---------------------
def test_websocket_streams_a_snapshot_then_live_events():
    bus = EventBus()
    tid = uuid.uuid4()

    class FakeTasks:
        async def detail(self, task_id):
            return SimpleNamespace(task=SimpleNamespace(status="PLANNING"))

    app = FastAPI()
    app.include_router(events_router.router)
    app.state.container = SimpleNamespace(events=bus, tasks=FakeTasks())
    with TestClient(app) as client, client.websocket_connect(f"/tasks/{tid}/event-stream") as ws:
        assert ws.receive_json() == {"event": "snapshot", "status": "PLANNING"}
        bus.publish(tid, {"event": "step.started", "step": "PLAN"})
        assert ws.receive_json() == {"event": "step.started", "step": "PLAN"}
    assert not bus._subs[str(tid)]                      # unsubscribed on disconnect
