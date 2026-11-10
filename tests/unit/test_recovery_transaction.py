"""The pure decisions of the recovery transaction: how an UNCERTAIN commit is reconciled (applied / fenced / unknown),
and the shared timeout validator. No database."""
import uuid

import pytest

from app.config import Settings
from app.services.recovery_transaction import (
    Candidate, Intent, RecoveryEvidence, Verdict, applied_candidates, judge_rows, reconcile_precheck)
from app.services.runner import Runner
from app.validation import policies as P
from app.validation.validators import validate_recovery_timeouts


def cand(intent=Intent.REQUEUE, applied: bool | None = True) -> Candidate:
    return Candidate(uuid.uuid4(), uuid.uuid4(), intent, update_applied=applied)


def evidence(*cands: Candidate, complete=True) -> RecoveryEvidence:
    return RecoveryEvidence(candidates=list(cands), candidates_complete=complete)


def original_row(c: Candidate):
    return (c.request_id, "CLAIMED", c.original_claim_token)


def intended_row(c: Candidate):
    return (c.request_id, "PENDING", None) if c.intent is Intent.REQUEUE else (c.request_id, "DONE", None)


# --- precheck: explicit branches, fixed order ---------------------------------------------------------------------
def test_no_candidates_at_all_is_no_changes():
    assert reconcile_precheck(evidence()) is Verdict.NO_CHANGES


def test_all_candidates_fenced_is_no_changes_and_never_reaches_the_all_classification():
    ev = evidence(cand(applied=False), cand(applied=False))
    assert applied_candidates(ev) == []
    assert reconcile_precheck(ev) is Verdict.NO_CHANGES


def test_an_incomplete_candidate_list_is_unresolved():
    assert reconcile_precheck(evidence(cand(), complete=False)) is Verdict.UNRESOLVED
    assert reconcile_precheck(evidence(complete=False)) is Verdict.UNRESOLVED


def test_a_single_unknown_candidate_is_unresolved_even_when_the_rest_is_fenced_or_applied():
    assert reconcile_precheck(evidence(cand(applied=None), cand(applied=False))) is Verdict.UNRESOLVED
    assert reconcile_precheck(evidence(cand(applied=None), cand(applied=True))) is Verdict.UNRESOLVED


def test_applied_candidates_need_a_query():
    assert reconcile_precheck(evidence(cand(applied=True), cand(applied=False))) is None


def test_the_empty_applied_set_is_never_judged_by_rows():
    assert judge_rows(evidence(cand(applied=False)), []) is Verdict.UNRESOLVED


# --- judging the rows of the single SELECT -------------------------------------------------------------------------
def test_all_applied_rows_original_means_the_commit_did_not_land():
    a, b = cand(), cand(Intent.FINISH_DONE)
    assert judge_rows(evidence(a, b), [original_row(a), original_row(b)]) is Verdict.ROLLED_BACK


def test_all_applied_rows_intended_means_the_commit_landed():
    a, b = cand(), cand(Intent.FINISH_DONE)
    assert judge_rows(evidence(a, b), [intended_row(a), intended_row(b)]) is Verdict.COMMITTED


def test_mixed_rows_are_unresolved():
    a, b = cand(), cand()
    assert judge_rows(evidence(a, b), [original_row(a), intended_row(b)]) is Verdict.UNRESOLVED


def test_a_row_in_any_other_state_is_unresolved():
    a, b = cand(), cand()
    other = (b.request_id, "CLAIMED", uuid.uuid4())                       # re-claimed by someone else
    assert judge_rows(evidence(a, b), [intended_row(a), other]) is Verdict.UNRESOLVED
    assert judge_rows(evidence(a), [(a.request_id, "DONE", None)]) is Verdict.UNRESOLVED   # not the intended state


def test_missing_duplicate_or_foreign_rows_are_unresolved():
    a, b = cand(), cand()
    ev = evidence(a, b)
    assert judge_rows(ev, [intended_row(a)]) is Verdict.UNRESOLVED                         # missing
    assert judge_rows(ev, [intended_row(a), intended_row(a)]) is Verdict.UNRESOLVED        # duplicate, one missing
    assert judge_rows(ev, [intended_row(a), intended_row(b), intended_row(a)]) is Verdict.UNRESOLVED
    assert judge_rows(ev, [intended_row(a), (uuid.uuid4(), "PENDING", None)]) is Verdict.UNRESOLVED   # foreign


def test_a_fenced_candidate_is_not_part_of_the_commit_decision():
    applied, fenced = cand(), cand(applied=False)
    assert judge_rows(evidence(applied, fenced), [intended_row(applied)]) is Verdict.COMMITTED
    assert judge_rows(evidence(applied, fenced), [original_row(applied)]) is Verdict.ROLLED_BACK


# --- the timeout validator is one function used by Settings and Runner ---------------------------------------------
def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_defaults_are_the_documented_numbers():
    s = make()
    assert (s.recovery_statement_timeout_seconds, s.runner_recovery_timeout_seconds,
            s.runner_recovery_shutdown_wait_seconds, s.db_pool_timeout_seconds) == (15.0, 20.0, 5.0, 30.0)
    assert P.DEFAULT_RECOVERY_STATEMENT_TIMEOUT_SECONDS == 15.0 and P.DEFAULT_DB_POOL_TIMEOUT_SECONDS == 30.0


BAD = [
    (0, 20, 5), (-1, 20, 5), (15, 0, 5), (15, 20, 0), (15, 20, -1),     # not positive
    (20, 20, 5), (25, 20, 5),                                           # statement must be below the operation timeout
    (3_000_000, 4_000_000, 5),                                          # milliseconds exceed PostgreSQL's int range
    (0.0004, 20, 5),                                                    # rounds to 0 ms
]
INF, NINF, NAN = float("inf"), float("-inf"), float("nan")
NOT_FINITE = [(bad, 20, 5) for bad in (INF, NINF, NAN)] + [(15, bad, 5) for bad in (INF, NINF, NAN)] \
    + [(15, 20, bad) for bad in (INF, NINF, NAN)]                          # every position, every non-finite value
BAD += NOT_FINITE


@pytest.mark.parametrize("statement,operation,wait", BAD)
def test_the_validator_rejects_bad_values(statement, operation, wait):
    with pytest.raises(ValueError):
        validate_recovery_timeouts(statement, operation, wait)


@pytest.mark.parametrize("statement,operation,wait", BAD)
def test_settings_reject_bad_values(statement, operation, wait):
    with pytest.raises(ValueError):                                       # pydantic's ValidationError is a ValueError
        make(recovery_statement_timeout_seconds=statement, runner_recovery_timeout_seconds=operation,
             runner_recovery_shutdown_wait_seconds=wait)


@pytest.mark.parametrize("statement,operation,wait", BAD)
def test_a_runner_built_without_settings_rejects_the_same_values(statement, operation, wait):
    with pytest.raises(ValueError):
        Runner(None, None, None, None, [], advisory_key=1, recovery_statement_timeout_seconds=statement,
               recovery_timeout_seconds=operation, recovery_shutdown_wait_seconds=wait)


def test_the_boundaries_just_inside_are_accepted():
    validate_recovery_timeouts(0.001, 0.002, 0.001)
    validate_recovery_timeouts(2_147_483.647, 3_000_000, 1)


@pytest.mark.parametrize("statement,operation,wait", NOT_FINITE)
def test_a_non_finite_value_names_the_setting_that_must_be_finite(statement, operation, wait):
    with pytest.raises(ValueError, match=r"timeout must be a finite number greater than 0"):
        validate_recovery_timeouts(statement, operation, wait)


@pytest.mark.parametrize("name,env", [("operation", "RUNNER_RECOVERY_TIMEOUT_SECONDS"),
                                      ("wait", "RUNNER_RECOVERY_SHUTDOWN_WAIT_SECONDS"),
                                      ("statement", "RECOVERY_STATEMENT_TIMEOUT_SECONDS")])
@pytest.mark.parametrize("text", ["inf", "-inf", "nan"])
def test_settings_reject_non_finite_values_that_arrive_as_environment_text(monkeypatch, name, env, text):
    monkeypatch.setenv(env, text)
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_the_documented_defaults_pass_the_validator():
    validate_recovery_timeouts(15, 20, 5)


def test_pool_timeout_must_be_positive():
    with pytest.raises(ValueError):
        make(db_pool_timeout_seconds=0)


@pytest.mark.parametrize("bad", [INF, NINF, NAN, -1.0, 0.0])
def test_pool_timeout_must_be_a_finite_positive_number(bad):
    with pytest.raises(ValueError):                                        # a pool wait of "forever" is no bound at all
        make(db_pool_timeout_seconds=bad)


def test_a_finite_positive_pool_timeout_is_accepted_and_not_tied_to_the_recovery_timeouts():
    assert make(db_pool_timeout_seconds=0.5).db_pool_timeout_seconds == 0.5      # may be below / above the recovery waits
    assert make(db_pool_timeout_seconds=600.0).db_pool_timeout_seconds == 600.0
