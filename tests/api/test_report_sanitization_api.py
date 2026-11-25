"""The response-level `sanitization` of the artifact / report views, and the report's tests section built from RECORDS.

One real (mock-provider) completed task is run, then the STORED rows are edited in the database so that a structured string
is too long or holds a secret, or so that the orchestrator's own test-run record is absent: the views must report what the
sanitiser did (plan B-2) and must show a test run only as far as it was recorded."""
import json
import uuid

from sqlalchemy import select

from app.models.tables import Execution, TaskStep
from app.validation import policies as P
from tests.api.test_reports_api import artifacts, run

TOKEN = "ghp_abcdefghijklmnop1234"
LONG = "x" * 700


async def set_output(db, task_id, kind: str, **fields) -> None:
    async with db.transaction() as s:
        step = (await s.execute(select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step_key == kind))).scalars().one()
        step.output = {**step.output, **fields}


async def orchestrator_row(db, task_id) -> Execution:
    async with db.transaction() as s:
        rows = (await s.execute(select(Execution).where(Execution.task_id == task_id))).scalars()
        return next(r for r in rows if (r.detail or {}).get("orchestrator_test_run") is True)


async def edit_run(db, task_id, **fields) -> None:
    row = await orchestrator_row(db, task_id)
    async with db.transaction() as s:
        live = await s.get(Execution, row.id)
        for key, value in fields.items():
            setattr(live, key, value)


async def drop_run(db, task_id) -> None:
    row = await orchestrator_row(db, task_id)
    async with db.transaction() as s:
        await s.delete(await s.get(Execution, row.id))


def of(body) -> dict:
    return body["sanitization"]


# --- Gap 3: structured strings aggregate into the response's `sanitization` -------------------------------------------------
async def test_a_clean_response_reports_no_truncation_and_no_reason(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    async with client:
        listing = await artifacts(client, task.id)
        report = (await client.get(f"/tasks/{task.id}/report")).json()
    for body in (listing, report):
        assert of(body)["truncated"] is False and of(body)["reasons"] == []


async def test_a_long_structured_string_in_a_list_item_is_reported_as_truncated(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    await set_output(db, task.id, "DESIGN_REVIEW", summary=LONG)
    async with client:
        body = await artifacts(client, task.id)
    assert of(body)["truncated"] is True and "STRING_TRUNCATED" in of(body)["reasons"]
    review = next(i for i in body["items"] if i["kind"] == "DESIGN_REVIEW")
    assert review["summary"].endswith("...[truncated]") and len(review["summary"]) < 400


async def test_a_secret_in_a_list_item_is_redacted_listed_and_absent_from_the_body(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    await set_output(db, task.id, "DESIGN_REVIEW", summary=f"token {TOKEN} leaked")
    async with client:
        response = await client.get(f"/tasks/{task.id}/artifacts")
    body = response.json()
    assert TOKEN not in response.text
    assert of(body)["reasons"] == ["SECRET_REDACTED"] and of(body)["truncated"] is False   # masking is not a cut


async def test_a_long_or_secret_structured_string_in_the_report_is_aggregated(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    await set_output(db, task.id, "CODE_REVIEW", summary=f"see {TOKEN}", issues=[{"severity": "MINOR", "description": LONG}])
    async with client:
        response = await client.get(f"/tasks/{task.id}/report")
    body = response.json()
    assert TOKEN not in response.text
    assert of(body)["truncated"] is True and {"STRING_TRUNCATED", "SECRET_REDACTED"} <= set(of(body)["reasons"])
    assert len(of(body)["reasons"]) == len(set(of(body)["reasons"]))


async def test_a_free_form_json_reason_reaches_the_response_level_too(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    await set_output(db, task.id, "PLAN", design_direction=LONG * 20)
    async with client:
        body = (await client.get(f"/tasks/{task.id}/report")).json()
    assert body["plan"]["content"]["truncated"] is True
    assert of(body)["truncated"] is True and "STRING_TRUNCATED" in of(body)["reasons"]


async def test_size_limit_and_structured_truncation_are_merged_without_duplicates(db, tmp_path, monkeypatch):
    client, h, task = await run(db, tmp_path)
    await set_output(db, task.id, "DESIGN_REVIEW", summary=LONG)
    await set_output(db, task.id, "CODE_REVIEW", summary=f"{TOKEN} {LONG}")
    async with client:
        one = len((await client.get(f"/tasks/{task.id}/artifacts", params={"limit": 1})).content)
        monkeypatch.setattr(P, "ARTIFACT_LIST_RESPONSE_MAX_BYTES", int(one * 2.6))
        body = (await client.get(f"/tasks/{task.id}/artifacts", params={"limit": 5})).json()
    reasons = of(body)["reasons"]
    assert body["size_limited"] is True and of(body)["omitted_items"] == body["omitted_items"] and body["omitted_items"] > 0
    assert {"SIZE_LIMIT", "STRING_TRUNCATED"} <= set(reasons) and len(reasons) == len(set(reasons))
    assert of(body)["truncated"] is True


async def test_the_artifact_detail_keeps_aggregating_its_content_and_error(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    async with client:
        test = next(i for i in (await artifacts(client, task.id))["items"] if i["kind"] == "TEST")
        async with db.transaction() as s:
            step = await s.get(TaskStep, uuid.UUID(test["id"]))
            step.error = f"{TOKEN} " + LONG * 4
        detail = (await client.get(f"/tasks/{task.id}/artifacts/{test['id']}")).json()
    assert TOKEN not in json.dumps(detail)
    assert of(detail)["truncated"] is True and {"STRING_TRUNCATED", "SECRET_REDACTED"} <= set(of(detail)["reasons"])


# --- Gap 5: the tests section is built from the recorded orchestrator run -----------------------------------------------------
async def test_a_recorded_orchestrator_run_is_shown_as_recorded(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    row = await orchestrator_row(db, task.id)
    async with client:
        tests = (await client.get(f"/tasks/{task.id}/report")).json()["tests"]
    run_ = tests["orchestrator_run"]
    assert run_["recorded"] is True and run_["run_by"] == "ORCHESTRATOR" == tests["run_by"]
    assert run_["outcome"] == row.outcome == "SUCCESS" and run_["exit_code"] == row.exit_code
    assert run_["exit_codes"] == [0] and run_["finished_at"] and run_["output_tail"]
    assert tests["passed"] is True and tests["executed"] >= 1
    assert tests["policy_command"] and tests["command"] == tests["policy_command"]    # the step recorded this very policy


async def test_the_command_is_not_claimed_when_the_step_recorded_another_policy(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    await set_output(db, task.id, "TEST", test_policy_hash="0" * 64)
    async with client:
        tests = (await client.get(f"/tasks/{task.id}/report")).json()["tests"]
    assert tests["command"] is None and tests["policy_command"]


async def test_without_a_recorded_run_the_section_says_so_and_invents_nothing(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    await drop_run(db, task.id)
    async with client:
        tests = (await client.get(f"/tasks/{task.id}/report")).json()["tests"]
    assert tests["orchestrator_run"] == {"recorded": False, "run_by": "UNKNOWN", "outcome": None, "exit_code": None,
                                         "exit_codes": [], "finished_at": None, "reason": None, "tracking": None,
                                         "output_tail": None}
    assert tests["run_by"] == "UNKNOWN" and tests["command"] is None      # the policy command is not presented as a run
    assert tests["policy_command"] and "pytest" in " ".join(tests["policy_command"])


async def test_a_secret_or_a_very_long_output_tail_is_redacted_truncated_and_aggregated(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    await edit_run(db, task.id, stdout_tail=f"{TOKEN}\n" + "y" * 3000 + "\n== 3 passed ==")
    async with client:
        response = await client.get(f"/tasks/{task.id}/report")
    body = response.json()
    tail = body["tests"]["orchestrator_run"]["output_tail"]
    assert TOKEN not in response.text and tail.endswith("== 3 passed ==")      # the END of the output is what is kept
    assert tail.startswith("...[truncated]") and len(tail) <= 2000 + len("...[truncated]")
    assert of(body)["truncated"] is True and "STRING_TRUNCATED" in of(body)["reasons"]


async def test_a_secret_inside_the_tail_is_redacted_and_listed(db, tmp_path):
    client, h, task = await run(db, tmp_path)
    await edit_run(db, task.id, stdout_tail=f"auth failed for {TOKEN}")
    async with client:
        response = await client.get(f"/tasks/{task.id}/report")
    body = response.json()
    assert TOKEN not in response.text and "SECRET_REDACTED" in of(body)["reasons"]
    assert of(body)["truncated"] is False
