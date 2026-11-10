"""The response size contract (schemas/reports.py: finalize) and the startup check of the limits."""
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from app.infra.redaction import redact_session_ids, session_fingerprint
from app.providers.base import redact
from app.schemas.reports import (ArtifactListOut, SanitizationOut, finalize, render_artifact, render_artifact_list,
                                 render_report)
from app.validation import policies as P
from app.validation import validators as V
from app.validation.exceptions import ResponseSizeUnsatisfiable

NOW = datetime(2026, 11, 4, tzinfo=UTC)


def clean() -> dict:
    return {"truncated": False, "reasons": [], "omitted_items": 0, "omitted_sections": []}


def validity():
    return {"state": "UNVERIFIED", "reasons": ["the live workspace is not compared with this artifact"]}


def item(i: int) -> dict:
    return {"id": uuid.uuid4(), "kind": "DEVELOP", "plan_revision": 0, "workflow_iteration": 0, "step_attempt": i,
            "status": "SUCCEEDED", "is_latest_succeeded": False, "validity": validity(), "created_at": NOW,
            "finished_at": NOW, "summary": "s" * 200}


def list_payload(n: int, total: int | None = None, offset: int = 0) -> dict:
    total = n if total is None else total
    end = offset + n
    return {"items": [item(i) for i in range(n)], "total": total, "returned": n, "omitted_items": 0,
            "has_more": end < total, "size_limited": False, "next_offset": end if end < total else None,
            "limit": 100, "offset": offset, "sanitization": clean()}


def size_of(model) -> int:
    return len(model.model_dump_json().encode())


# --- list: pagination under a size limit --------------------------------------------------------------------------------
def test_a_page_that_fits_is_returned_untouched():
    out = render_artifact_list(list_payload(5, total=12))
    assert (out.returned, out.omitted_items, out.size_limited, out.has_more, out.next_offset) == (5, 0, False, True, 5)
    assert out.sanitization.truncated is False and out.total == 12


def test_an_oversized_page_drops_trailing_items_and_says_so(monkeypatch):
    one = size_of(ArtifactListOut.model_validate(list_payload(1), strict=True))
    monkeypatch.setattr(P, "ARTIFACT_LIST_RESPONSE_MAX_BYTES", one * 6)
    out = render_artifact_list(list_payload(40, total=100, offset=10))
    assert 1 <= out.returned < 40 and size_of(out) <= one * 6
    assert out.omitted_items == 40 - out.returned and out.size_limited is True
    assert out.total == 100                                   # the database count is never reduced
    assert out.next_offset == 10 + out.returned and out.has_more is True    # the omitted items are re-fetchable
    assert "SIZE_LIMIT" in out.sanitization.reasons and out.sanitization.omitted_items == out.omitted_items
    assert out.items[0].step_attempt == 0                     # the first item always stays


def test_the_last_page_has_no_next_offset_even_when_size_limited(monkeypatch):
    one = size_of(ArtifactListOut.model_validate(list_payload(1), strict=True))
    monkeypatch.setattr(P, "ARTIFACT_LIST_RESPONSE_MAX_BYTES", one * 3)
    last = render_artifact_list(list_payload(8, total=8, offset=0))
    assert last.size_limited and last.has_more and last.next_offset == last.returned
    plain = render_artifact_list(list_payload(2, total=2))
    assert plain.has_more is False and plain.next_offset is None


def test_a_page_is_never_empty_when_something_exists(monkeypatch):
    one = size_of(ArtifactListOut.model_validate(list_payload(1), strict=True))
    monkeypatch.setattr(P, "ARTIFACT_LIST_RESPONSE_MAX_BYTES", one + 10)       # room for exactly one item
    out = render_artifact_list(list_payload(30, total=30))
    assert out.returned == 1 and out.has_more and out.next_offset == 1


def test_the_bounded_fallback_is_empty_but_consistent_when_not_even_one_item_fits(monkeypatch):
    monkeypatch.setattr(P, "ARTIFACT_LIST_RESPONSE_MAX_BYTES", 450)
    out = render_artifact_list(list_payload(6, total=6))
    assert out.returned == 0 and out.items == [] and out.size_limited and out.next_offset == 0 and out.has_more
    assert out.omitted_items == 6 and size_of(out) <= 450


def test_an_unsatisfiable_limit_is_an_explicit_error(monkeypatch):
    monkeypatch.setattr(P, "ARTIFACT_LIST_RESPONSE_MAX_BYTES", 10)
    with pytest.raises(ResponseSizeUnsatisfiable):
        render_artifact_list(list_payload(3))


# --- the generic loop -----------------------------------------------------------------------------------------------------
class Tiny(BaseModel):
    must: str
    note: str = ""
    sanitization: SanitizationOut


def tiny(note: str) -> dict:
    return {"must": "x", "note": note, "sanitization": clean()}


def test_a_cut_that_breaks_the_schema_ends_in_the_fallback():
    def break_schema(payload):
        payload["must"] = None                       # the "cut" nulls a required field
        return True

    out = finalize(Tiny, tiny("n" * 500), 300, [break_schema], lambda p: tiny(""))
    assert out.must == "x" and out.note == ""


def test_the_cut_rounds_are_bounded(monkeypatch):
    monkeypatch.setattr(P, "RESPONSE_SHRINK_MAX_ROUNDS", 3)
    calls = []

    def barely(payload):
        calls.append(1)
        payload["note"] = payload["note"][:-1]       # always progresses, never enough
        return True

    out = finalize(Tiny, tiny("n" * 500), 200, [barely], lambda p: tiny("fallback"))
    assert len(calls) == 3 and out.note == "fallback"


def test_a_payload_the_service_built_wrongly_is_a_bug_not_a_silent_fallback():
    with pytest.raises(Exception) as info:
        finalize(Tiny, {"must": 5, "sanitization": clean()}, 10_000, [], lambda p: tiny(""))
    assert "must" in str(info.value)


def test_strict_validation_requires_real_types():
    bad = list_payload(1)
    bad["items"][0]["id"] = str(bad["items"][0]["id"])                 # a string where a UUID object is required
    with pytest.raises(Exception):
        render_artifact_list(bad)


# --- detail and report ----------------------------------------------------------------------------------------------------
def detail_payload(content_bytes: int) -> dict:
    return {**item(0), "content": {"value": {"text": "t" * content_bytes}, "truncated": False, "reasons": [],
                                   "omitted_paths": []}, "error": "e" * 2000, "sanitization": clean()}


def test_an_oversized_artifact_is_cut_through_its_reserved_fields(monkeypatch):
    monkeypatch.setattr(P, "ARTIFACT_ITEM_MAX_BYTES", 4000)
    out = render_artifact(detail_payload(60_000))
    assert size_of(out) <= 4000 and out.content is not None and out.content.truncated
    assert "SIZE_LIMIT" in out.content.reasons and "SIZE_LIMIT" in out.sanitization.reasons
    assert "content" in out.sanitization.omitted_sections


def test_an_artifact_that_fits_is_unchanged():
    out = render_artifact(detail_payload(100))
    assert out.content.truncated is False and out.sanitization.truncated is False and len(out.error) == 2000


def report_payload(plan_bytes=100, gates=0, iterations=0) -> dict:
    task = {"id": uuid.uuid4(), "project_id": None, "status": "COMPLETED", "plan_revision": 0, "workflow_iteration": 0,
            "replan_count": 0, "cli_invocations": 5, "run_seconds": 1.5, "failure_reason": None, "created_at": NOW,
            "updated_at": NOW}
    return {
        "task": task, "outcome": "COMPLETED", "outcome_note": "met, not accepted",
        "plan": {"artifact_id": uuid.uuid4(), "plan_revision": 0, "validity": {"state": "CURRENT", "reasons": []},
                 "content": {"value": {"p": "p" * plan_bytes}, "truncated": False, "reasons": [], "omitted_paths": []}},
        "design_review": None, "implementation": None, "tests": None, "code_review": None,
        "iterations": [{"plan_revision": 0, "workflow_iteration": i, "step_attempts": {"DEVELOP": 1},
                        "design_issues": [{"severity": "MAJOR", "description": "d" * 300}] * 3} for i in range(iterations)],
        "human_gates": [{"id": uuid.uuid4(), "type": "DESIGN", "status": "DECIDED", "checkpoint_step_key": "PLAN",
                         "created_at": NOW, "request": {"value": {"r": "r" * 2000}, "truncated": False, "reasons": [],
                                                         "omitted_paths": []}} for _ in range(gates)],
        "historical_completion": {"recorded": True, "completed_at": NOW, "transition_reason": "definition_of_done_met",
                                  "conditions": "UNKNOWN", "note": "n"},
        "current_re_evaluation": {"evaluated_at": NOW, "evaluation_basis": {"plan_revision": 0}, "all_met": True,
                                  "conditions": [{"id": "x", "met": True, "message": "m"}], "note": "n"},
        "last_transition": None, "next_actions": [{"code": "REVIEW_CHANGES", "text": "review"}],
        "runtime": {"state": "UNKNOWN", "ready": None, "blockers": []}, "sanitization": clean()}


def test_a_report_is_cut_in_priority_order_and_keeps_its_core(monkeypatch):
    monkeypatch.setattr(P, "REPORT_RESPONSE_MAX_BYTES", 6000)
    out = render_report(report_payload(plan_bytes=30_000, gates=6, iterations=20))
    assert size_of(out) <= 6000
    assert out.outcome == "COMPLETED" and out.next_actions and out.historical_completion.conditions == "UNKNOWN"
    assert out.current_re_evaluation.conditions and "SIZE_LIMIT" in out.sanitization.reasons
    assert out.sanitization.omitted_sections                      # what was left out is named


def test_a_report_that_fits_is_complete():
    out = render_report(report_payload(gates=2, iterations=3))
    assert len(out.human_gates) == 2 and len(out.iterations) == 3 and out.sanitization.truncated is False


def test_a_hopeless_report_falls_back_to_the_core(monkeypatch):
    monkeypatch.setattr(P, "REPORT_RESPONSE_MAX_BYTES", 2600)
    out = render_report(report_payload(plan_bytes=50_000, gates=5, iterations=30))
    assert out.plan is None and out.human_gates == [] and out.iterations == [] and out.next_actions
    assert size_of(out) <= 2600


# --- startup validation of the limits -----------------------------------------------------------------------------------------
def test_the_default_limits_are_valid():
    V.validate_response_limits()


@pytest.mark.parametrize("name,value", [
    ("ARTIFACT_LIST_RESPONSE_MAX_BYTES", P.RESPONSE_ENVELOPE_MAX_OVERHEAD_BYTES + P.ARTIFACT_ITEM_MAX_BYTES - 1),
    ("ARTIFACT_ITEM_MAX_BYTES", P.SANITIZED_JSON_MAX_BYTES + P.RESPONSE_ENVELOPE_MAX_OVERHEAD_BYTES - 1),
    ("REPORT_RESPONSE_MAX_BYTES", 10), ("SANITIZE_MAX_DEPTH", 0), ("RESPONSE_SHRINK_MAX_ROUNDS", 0)])
def test_a_limit_that_cannot_hold_one_answer_refuses_to_start(monkeypatch, name, value):
    monkeypatch.setattr(P, name, value)
    with pytest.raises(ValueError):
        V.validate_response_limits()


def test_the_application_refuses_to_start_with_an_invalid_limit(monkeypatch):
    from app.main import create_app
    monkeypatch.setattr(P, "ARTIFACT_LIST_RESPONSE_MAX_BYTES", 1)
    with pytest.raises(ValueError):
        create_app(run_preflight=False)


# --- the redaction primitives moved to infra --------------------------------------------------------------------------------------
def test_the_provider_redact_wrapper_keeps_its_exact_output():
    sid = "abc123-native-session"
    text = "resumed " + sid + " and 123e4567-e89b-42d3-a456-426614174000 plus " + sid + " again"
    assert redact(text, (sid,)) == "resumed sess:5d3a9c67 and sess:320159eb plus sess:5d3a9c67 again"
    assert redact(text) == "resumed abc123-native-session and sess:320159eb plus abc123-native-session again"
    assert session_fingerprint(sid) == "sess:5d3a9c67"
    long = "x" * (P.LOG_TAIL_CHARS + 50) + sid
    out = redact(long, (sid,))
    assert len(out) == P.LOG_TAIL_CHARS and out.endswith("sess:5d3a9c67")
    assert redact(None) == "" and redact("") == ""


def test_the_infra_primitive_distinguishes_known_ids_from_ordinary_uuids():
    other = "123e4567-e89b-42d3-a456-426614174000"
    text = f"native-1 and {other}"
    assert redact_session_ids(text, ("native-1",), all_uuids=False) == f"{session_fingerprint('native-1')} and {other}"
    assert other not in redact_session_ids(text, ("native-1",), all_uuids=True)
