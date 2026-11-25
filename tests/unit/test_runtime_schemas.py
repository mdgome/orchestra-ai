"""B2 phase 6 schema contracts (design 14.1, 14.2, section 13 'Report runtime schema', 'Contract details', 'Bounded diagnostics
DTO'): RuntimeOut / SweeperStatusOut strict validation, the diagnostics DTO field allow-lists and conversions."""
import uuid
from dataclasses import fields as dc_fields
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.schemas import api as A
from app.schemas.reports import ReportOut, RuntimeOut, SweeperStatusOut, render_report
from app.services.diagnostics_service import DiagnosticCause, DiagnosticItem, Status
from app.repositories.task_repository import ExecutionFact
from app.services.project_terminal import TrackedAnomaly, TrackedBlock
from app.services.runtime_status import BackgroundComponentStatus
from app.services.sweeper_stall import SweeperConfigInvalidSnapshot, SweeperStallSnapshot
from app.services.tracked_diagnostics import evaluate
from tests.unit.test_response_contract import NOW, report_payload

DEFAULT_SWEEPER = {"stalled": False, "consecutive_failures": 0, "last_failure_code": None, "last_failure_at": None,
                   "next_retry_at": None, "config_invalid_active": False, "clearance_pending": False}


def runtime_of(payload_runtime: dict):
    payload = report_payload()
    payload["runtime"] = payload_runtime
    return render_report(payload).runtime


# --- the report runtime part (section 13 r24 items 1 to 8) ---------------------------------------------------------------------
def test_item_1_a_service_dict_with_warnings_and_a_stalled_sweeper_is_preserved():
    sweeper = {**DEFAULT_SWEEPER, "stalled": True, "consecutive_failures": 5, "last_failure_code": "SWEEP_QUERY_FAILED",
               "last_failure_at": NOW, "next_retry_at": NOW}
    out = runtime_of({"state": "KNOWN", "ready": True, "blockers": [], "warnings": ["SWEEPER_STALLED"], "sweeper": sweeper,
                      "background_degraded": True})
    assert out.warnings == ["SWEEPER_STALLED"] and out.sweeper.stalled and out.sweeper.consecutive_failures == 5
    assert out.sweeper.last_failure_code == "SWEEP_QUERY_FAILED" and out.background_degraded is True


def test_item_2_a_legacy_dict_gets_the_defaults_of_14_2():
    out = runtime_of({"state": "KNOWN", "ready": True, "blockers": []})
    assert out.warnings == [] and out.background_degraded is False
    assert out.sweeper.model_dump() == DEFAULT_SWEEPER


def test_item_3_the_warnings_and_blockers_lists_of_two_responses_are_different_instances():
    a, b = RuntimeOut(state="UNKNOWN"), RuntimeOut(state="UNKNOWN")
    assert a.warnings == b.warnings == [] and a.warnings is not b.warnings and a.blockers is not b.blockers
    a.warnings.append("X")
    a.blockers.append("Y")
    assert b.warnings == [] and b.blockers == [] and a.sweeper is not b.sweeper


@pytest.mark.parametrize("bad", [
    {"warnings": "SWEEPER_STALLED"}, {"sweeper": {**DEFAULT_SWEEPER, "stalled": "false"}},
    {"sweeper": {**DEFAULT_SWEEPER, "last_failure_at": NOW.isoformat()}},
    {"sweeper": {**DEFAULT_SWEEPER, "consecutive_failures": 1.5}}, {"sweeper": {**DEFAULT_SWEEPER, "consecutive_failures": "3"}},
    {"background_degraded": "true"}], ids=["str-warnings", "str-stalled", "iso-datetime", "float-count", "str-count", "str-bool"])
def test_item_4_strict_validation_still_rejects_wrongly_typed_payloads(bad):
    payload = report_payload()
    payload["runtime"] = {"state": "KNOWN", "ready": True, "blockers": [], "warnings": [], "sweeper": dict(DEFAULT_SWEEPER),
                          "background_degraded": False, **bad}
    with pytest.raises(ValidationError):
        render_report(payload)


def test_item_5_an_unknown_key_is_ignored_the_new_fields_survive_and_no_cursor_reaches_the_report():
    cursor = str(uuid.uuid4())
    out = runtime_of({"state": "KNOWN", "ready": True, "blockers": [], "extra": 1, "warnings": ["SWEEPER_STALLED"],
                      "sweeper": {**DEFAULT_SWEEPER, "stalled": True, "current_failure_cursor": cursor, "first_failure_cursor": cursor},
                      "background_degraded": False})
    dumped = out.model_dump()
    assert dumped["warnings"] == ["SWEEPER_STALLED"] and dumped["sweeper"]["stalled"] is True
    assert set(dumped["sweeper"]) == set(SweeperStatusOut.model_fields) and cursor not in repr(dumped)
    assert set(dumped) == {"state", "ready", "blockers", "warnings", "sweeper", "background_degraded"}


def test_item_6_an_unknown_runtime_state_carries_the_default_object_and_no_warning():
    out = runtime_of({"state": "UNKNOWN", "ready": None, "blockers": []})
    assert out.warnings == [] and out.sweeper == SweeperStatusOut() and out.background_degraded is False


def test_item_8_the_report_payload_fixture_keeps_passing_unedited_and_shows_the_new_defaults():
    out = render_report(report_payload())
    assert out.runtime.warnings == [] and out.runtime.sweeper.model_dump() == DEFAULT_SWEEPER
    assert out.runtime.background_degraded is False and isinstance(out, ReportOut)


def test_the_sweeper_status_out_field_set_has_no_cursor_sql_exception_or_process_field():
    forbidden = ("cursor", "sql", "detail", "exception", "error", "pid", "rationale", "session")
    for model in (SweeperStatusOut, RuntimeOut):
        for name in model.model_fields:
            assert not any(word in name for word in forbidden), (model.__name__, name)


# --- the diagnostics DTOs ---------------------------------------------------------------------------------------------------------
def a_cause(**kw) -> DiagnosticCause:
    return DiagnosticCause(**{"reason_code": "LEASE_EXPIRED", "severity": Status.WARN, "blocks_execution": False,
                              "cause": "c", "recommendation": "r", **kw})


def convert(item: DiagnosticItem) -> dict:
    return A.DiagnosticItemOut.model_validate(item).model_dump()


def test_the_internal_causes_are_a_tuple_and_the_api_causes_a_list_golden():
    item = DiagnosticItem("TRACKED_EXECUTIONS", Status.WARN, NOW, causes=(a_cause(), a_cause(reason_code="NO_OWNING_RUN")))
    assert isinstance(item.causes, tuple) and isinstance(DiagnosticItem("X", Status.PASS, NOW).causes, tuple)
    out = A.DiagnosticItemOut.model_validate(item)
    assert isinstance(out.causes, list) and [c.reason_code for c in out.causes] == ["LEASE_EXPIRED", "NO_OWNING_RUN"]
    assert A.DiagnosticItemOut(code="x", status="PASS", checked_at=NOW, cause="", recommendation="", blocks_execution=False
                               ).causes == []


def test_the_item_key_set_is_additive_and_blocks_features_appears_only_when_it_names_a_feature():
    plain = convert(DiagnosticItem("X", Status.PASS, NOW))
    assert set(plain) == {"code", "status", "checked_at", "cause", "recommendation", "blocks_execution", "causes"}
    featured = convert(DiagnosticItem("PROCESS_TRACKING", Status.WARN, NOW, blocks_features=("TEST_RUN",)))
    assert featured["blocks_features"] == ["TEST_RUN"] and set(featured) == set(plain) | {"blocks_features"}


def test_a_cause_that_is_not_an_anomaly_has_the_neutral_fields_in_the_dto_too():
    out = A.DiagnosticCauseOut.model_validate(a_cause())
    assert (out.anomalies, out.anomalies_returned, out.anomalies_truncated, out.anomalies_total,
            out.anomalies_total_is_exact, out.may_be_permanently_stuck, out.sweeper, out.config_invalid,
            out.components, out.related_execution_ids, out.related_request_ids) == ([], 0, False, None, False, False, None,
                                                                                    None, [], [], [])


def test_an_anomaly_cause_converts_its_internal_tuple_to_a_list_and_serialises_may_be_permanently_stuck():
    shown = tuple(TrackedAnomaly(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "OTHER_SCHEMA_INVALID", "decode_error")
                  for _ in range(3))
    cause = a_cause(reason_code="TRACKED_EXECUTION_ANOMALY", severity=Status.FAIL, anomalies=shown, anomalies_returned=3,
                    anomalies_total=3, anomalies_total_is_exact=True, may_be_permanently_stuck=True)
    out = A.DiagnosticCauseOut.model_validate(cause).model_dump()
    assert isinstance(out["anomalies"], list) and len(out["anomalies"]) == 3 and out["may_be_permanently_stuck"] is True
    assert set(out["anomalies"][0]) == {"execution_id", "task_id", "project_id", "anomaly_kind", "sub_reason"}
    assert out["anomalies_returned"] == 3 and out["anomalies_total"] == 3 and out["anomalies_total_is_exact"] is True


def test_the_sweeper_cause_carries_the_stall_object_and_the_component_causes_carry_their_items():
    stall = SweeperStallSnapshot(True, NOW, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), 5, "SWEEP_QUERY_FAILED", NOW, NOW)
    config = SweeperConfigInvalidSnapshot(True, NOW, uuid.uuid4(), NOW, ("SWEEP_POLICY_INVALID",), False, NOW, False)
    comp = (BackgroundComponentStatus("sweeper", None, None, False, "EXITED_UNSUPERVISED"),
            BackgroundComponentStatus("risk-worker", "BUDGET", NOW, True, "DEGRADED"))
    out = A.DiagnosticCauseOut.model_validate(a_cause(sweeper=stall, config_invalid=config, components=comp)).model_dump()
    assert set(out["sweeper"]) == {f.name for f in dc_fields(SweeperStallSnapshot)}
    assert set(out["config_invalid"]) == set(A.SweeperConfigInvalidOut.model_fields)
    assert out["config_invalid"]["active"] is True and out["config_invalid"]["reasons"] == ["SWEEP_POLICY_INVALID"]
    assert out["components"][0] == {"component": "sweeper", "reason_code": None, "since": None, "restart_exhausted": False,
                                    "display_state": "EXITED_UNSUPERVISED"}
    assert out["components"][1]["reason_code"] == "BUDGET" and out["components"][1]["since"] == NOW


def test_the_default_stall_object_has_every_boolean_false_every_count_zero_and_the_rest_null():
    out = A.SweeperStallOut().model_dump()
    assert out["stalled"] is False and out["consecutive_failures"] == 0
    assert all(v is None for k, v in out.items() if k not in ("stalled", "consecutive_failures"))


def test_no_diagnostics_dto_has_a_field_for_sql_detail_an_exception_a_rationale_or_a_session():
    forbidden = ("sql", "detail", "exception", "traceback", "rationale", "requested_by", "native", "session", "token", "pid",
                 "process", "text", "stdout", "stderr")
    models = (A.DiagnosticItemOut, A.DiagnosticCauseOut, A.TrackedAnomalyOut, A.SweeperStallOut, A.SweeperConfigInvalidOut,
              A.BackgroundComponentOut)
    for model in models:
        for name in model.model_fields:
            assert not any(word in name for word in forbidden), (model.__name__, name)
    for cls in (DiagnosticCause, DiagnosticItem, TrackedAnomaly):
        for f in dc_fields(cls):
            assert not any(word in f.name for word in forbidden), (cls.__name__, f.name)


def test_the_internal_cause_attribute_names_are_the_dto_field_names():
    assert {f.name for f in dc_fields(DiagnosticCause)} == set(A.DiagnosticCauseOut.model_fields)
    assert {f.name for f in dc_fields(TrackedAnomaly)} == set(A.TrackedAnomalyOut.model_fields)


def test_the_response_is_never_sanitiser_dependent_marker_strings_in_the_service_never_reach_the_dto():
    """The cause objects hold identifiers and fixed codes only (a mutation that disables the response sanitiser still leaks
    nothing because the DTO never contains raw text): a marker placed in the tracked row cannot reach a cause field."""
    marker = "ghp_abcdefghijklmnop1234 SECRET-MARKER"
    eid, tid = uuid.uuid4(), uuid.uuid4()
    fact = ExecutionFact(eid, tid, None, marker, NOW - timedelta(seconds=5), None)            # the runner_id carries the marker
    verdict = evaluate(TrackedBlock(unresolved_ids=(eid,)), {eid: fact}, {tid: "TESTING"}, {}, {tid: "RUNNING"}, [], None,
                       None, NOW)
    dumped = repr([A.DiagnosticCauseOut.model_validate(c).model_dump() for c in verdict.causes])
    assert "SECRET-MARKER" not in dumped and "ghp_" not in dumped and verdict.causes


def test_the_runtime_out_field_set_is_exactly_the_report_runtime_keys():
    assert set(RuntimeOut.model_fields) == {"state", "ready", "blockers", "warnings", "sweeper", "background_degraded"}
    assert RuntimeOut.model_fields["sweeper"].annotation is SweeperStatusOut
