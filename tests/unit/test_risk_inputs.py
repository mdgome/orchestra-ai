"""B2 phase 5b (design 7.5 item 3): the decision inputs `rationale` and `requested_by`, the API bounds and the fingerprint golden."""
import pytest
from pydantic import ValidationError

from app.schemas.api import DecisionCreate
from app.services.human_gate_service import request_fingerprint
from app.validation import policies as P
from app.validation import validators as V
from app.validation.exceptions import ValidationFailed

RISK = "accept_unproven_risk"


def test_the_fingerprint_without_requested_by_is_byte_identical_to_the_pre_b2_value():
    assert request_fingerprint("APPROVED", "continue") == "ec8479484f66d6ea4c782e9f574cf077331e6e3b665e7c00530c0ca707628a0b"
    assert request_fingerprint("REJECTED", None) == "c101fcd16e513f6df5f5921223a228a43574c913af2e068330c695b7e467abb3"
    assert request_fingerprint("APPROVED", "continue", None) == request_fingerprint("APPROVED", "continue")


def test_a_present_requested_by_is_part_of_the_fingerprint():
    base = request_fingerprint("APPROVED", RISK)
    assert request_fingerprint("APPROVED", RISK, "op@example") != base
    assert request_fingerprint("APPROVED", RISK, "op@example") != request_fingerprint("APPROVED", RISK, "other")


@pytest.mark.parametrize("option", [RISK, "recheck_processes"])
def test_a_risk_option_requires_a_rationale_that_is_not_blank(option):
    for bad in (None, "", "   ", "\n\t "):
        with pytest.raises(ValidationFailed):
            V.validate_rationale(bad, option)
    assert V.validate_rationale("  the process is gone  ", option) == "the process is gone"


def test_other_options_keep_an_optional_unchanged_rationale():
    assert V.validate_rationale(None, "continue") is None
    assert V.validate_rationale("  keep spaces  ", "continue") == "  keep spaces  "


def test_the_rationale_length_is_counted_in_code_points_after_nfc_and_trimming():
    assert V.validate_rationale("x" * P.PROCESS_RISK_RATIONALE_MAX_LENGTH, RISK)
    with pytest.raises(ValidationFailed):
        V.validate_rationale("x" * (P.PROCESS_RISK_RATIONALE_MAX_LENGTH + 1), RISK)
    decomposed = "é" * 1000                              # 2000 code points that NFC folds to 1000
    assert len(V.validate_rationale(decomposed, RISK)) == 1000
    assert V.validate_rationale("\U0001F600" * 2000, RISK)     # an astral character is one code point


@pytest.mark.parametrize("bad", ["a\x00b", "a\ud800b", "a\udfffb"], ids=["nul", "high-surrogate", "low-surrogate"])
def test_nul_and_lone_surrogates_are_rejected_for_every_decision(bad):
    for option in (RISK, "continue", None):
        with pytest.raises(ValidationFailed):
            V.validate_rationale(bad, option)
    with pytest.raises(ValidationFailed):
        V.validate_requested_by(bad, RISK)


@pytest.mark.parametrize("bad", ["a\rb", "a\x07b", "a\x7fb", "a\x85b"])
def test_control_characters_other_than_newline_and_tab_are_rejected_in_a_risk_rationale(bad):
    with pytest.raises(ValidationFailed):
        V.validate_rationale(bad, RISK)
    assert V.validate_rationale("line one\nline\ttwo", RISK) == "line one\nline\ttwo"


def test_requested_by_rules():
    assert V.validate_requested_by(None, RISK) is None
    assert V.validate_requested_by(" op@example.com ", RISK) == "op@example.com"
    assert V.validate_requested_by("a" * P.REQUESTED_BY_MAX_LENGTH, "recheck_processes")
    for bad in ("", "   ", "has space", "a" * (P.REQUESTED_BY_MAX_LENGTH + 1), "café", "semi;colon"):
        with pytest.raises(ValidationFailed) as info:
            V.validate_requested_by(bad, RISK)
        assert info.value.details["field"] == "requested_by"
    for option in ("continue", "abort", None):                   # only the two risk options accept it
        with pytest.raises(ValidationFailed):
            V.validate_requested_by("op", option)


def test_the_api_schema_bounds_use_the_same_constants_and_count_raw_code_points():
    assert DecisionCreate(decision="APPROVED", rationale="x" * P.PROCESS_RISK_RATIONALE_MAX_LENGTH).requested_by is None
    with pytest.raises(ValidationError):
        DecisionCreate(decision="APPROVED", rationale="x" * (P.PROCESS_RISK_RATIONALE_MAX_LENGTH + 1))
    with pytest.raises(ValidationError):
        DecisionCreate(decision="APPROVED", requested_by="a" * (P.REQUESTED_BY_MAX_LENGTH + 1))
    # the raw value above the bound is rejected even though NFC would shorten it (one unit, one number)
    with pytest.raises(ValidationError):
        DecisionCreate(decision="APPROVED", rationale="é" * 1001)
    assert DecisionCreate(decision="APPROVED").requested_by is None
