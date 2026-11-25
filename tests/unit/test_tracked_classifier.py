"""B2 phase 3: the pure Python classifier of a tracked row and the mapping of `TestPolicyService` (design 6.1, 7.2). No database:
the equality with the SQL predicates is `tests/integration_pg/test_tracked_project_rules.py`."""
import asyncio
import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.infra.command_executor import CommandResult, TrackedRun
from app.models.states import CliOutcome, ProcessLifecycle, TreeProof
from app.services import tracked_classifier as K
from app.services.step_runner import (
    ProcessTrackingUnavailable, TaskCancelled, TerminationUnconfirmed, TrackedCommandPort,
)
from app.services.test_policy_service import TestPolicyService, _shielded
from app.validation import policies as P
from app.validation.exceptions import FailClosed

ROW = uuid.UUID(int=42)
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def risk(**override) -> dict:
    record = {"approval_id": str(uuid.uuid4()), "execution_id": str(ROW), "pid": 4242, "create_time": 1.5,
              "last_verdict": "UNCLEAR", "requested_at": "2026-01-01T00:00:00Z", "rationale": "gone, checked",
              "reverify_result": "UNKNOWN", "applied_at": "2026-01-01T00:00:01Z"}
    record.update(override)
    return record


def risk_detail(record=None, **top) -> dict:
    detail = {"tracked": True, "v": 1, "disposition": "OPERATOR_RISK_ACCEPTED", "reason_code": "OPERATOR_RISK_ACCEPTED",
              "risk_acceptance": risk() if record is None else record}
    detail.update(top)
    return detail


def classify(detail, *, finished=True, pid=4242):
    text = detail if detail is None or isinstance(detail, str) else json.dumps(detail)
    return K.classify_row(ROW, pid, NOW if finished else None, text)


@pytest.mark.parametrize("detail,klass", [
    (None, K.RowClass.LEGACY), ("null", K.RowClass.LEGACY), ("[1]", K.RowClass.LEGACY), ("5", K.RowClass.LEGACY),
    ({}, K.RowClass.LEGACY), ({"resumed": True, "termination": "x", "permission_requests": 0}, K.RowClass.LEGACY),
    ({"abandoned_by_takeover": True}, K.RowClass.LEGACY), ({"orchestrator_test_run": True, "reason": "x"}, K.RowClass.LEGACY),
    ({"tracked": True}, K.RowClass.UNRESOLVED), ({"tracked": True, "v": 1, "proof": None}, K.RowClass.UNRESOLVED),
    ({"tracked": True, "v": 9}, K.RowClass.UNRESOLVED),
    ({"tracked": True, "v": 1, "proof": "JOB_EMPTY"}, K.RowClass.RESOLVED_PROVEN),
    ({"tracked": True, "v": 1.0, "proof": "NEVER_RAN"}, K.RowClass.RESOLVED_PROVEN),
    (risk_detail(), K.RowClass.RESOLVED_RISK),
])
def test_the_row_classes(detail, klass):
    assert classify(detail).row_class is klass


@pytest.mark.parametrize("detail,finished,kind,sub", [
    ({"tracked": "true"}, True, "OTHER_SCHEMA_INVALID", "tracked_type"),
    ({"tracked": None}, True, "OTHER_SCHEMA_INVALID", "tracked_type"),
    ({"v": 1}, True, "OTHER_SCHEMA_INVALID", "tracked_missing"),
    ({"tracked": True, "v": 1, "proof": "JOB_EMPTY"}, False, "OTHER_SCHEMA_INVALID", "finished_at_missing"),
    ({"tracked": True, "v": 1, "proof": "BOGUS"}, True, "INVALID_PROOF", None),
    ({"tracked": True, "v": 1, "proof": 5}, True, "INVALID_PROOF", None),
    ({"tracked": True, "v": 1, "disposition": "NOPE"}, True, "INVALID_DISPOSITION_KIND", None),
    ({"tracked": True, "v": 1, "proof": "JOB_EMPTY", "disposition": "OPERATOR_RISK_ACCEPTED"}, True,
     "CONTRADICTORY_PROOF_DISPOSITION", None),
    ({"tracked": True, "proof": "JOB_EMPTY"}, True, "UNKNOWN_SCHEMA_VERSION", None),
    ({"tracked": True, "v": "1", "proof": "JOB_EMPTY"}, True, "UNKNOWN_SCHEMA_VERSION", None),
    ({"tracked": True, "v": True, "proof": "JOB_EMPTY"}, True, "UNKNOWN_SCHEMA_VERSION", None),
    # precedence: a wrong marker AND an unknown version is kind 1, the marker defect stays visible in the sub-reason
    ({"tracked": "x", "proof": "JOB_EMPTY"}, True, "UNKNOWN_SCHEMA_VERSION", "tracked_type"),
    ({"proof": "JOB_EMPTY", "v": 1}, True, "OTHER_SCHEMA_INVALID", "tracked_missing"),
])
def test_the_anomaly_kinds_and_their_precedence(detail, finished, kind, sub):
    got = classify(detail, finished=finished)
    assert got.is_anomaly and (got.anomaly_kind, got.sub_reason) == (kind, sub)


@pytest.mark.parametrize("detail,sub", [
    (risk_detail(record=[1]), "risk_acceptance_not_object"),
    (risk_detail(risk(approval_id=None)), "key_null:approval_id"),
    (risk_detail(risk(pid="4242")), "type:pid"),
    (risk_detail(risk(pid=4243)), "identity:pid"),
    (risk_detail(risk(execution_id=str(uuid.uuid4()))), "identity:execution_id"),
    (risk_detail(risk(rationale="   ")), "format:rationale"),
    (risk_detail(risk(requested_by="a b")), "format:requested_by"),
    (risk_detail(risk(requested_by=None)), "type:requested_by"),
    (risk_detail(reason_code="OTHER"), "reason_code"),
    (risk_detail(reason_code=None), "reason_code"),
])
def test_an_incomplete_risk_acceptance_names_the_first_failing_check(detail, sub):
    got = classify(detail)
    assert got.is_anomaly and got.anomaly_kind == "INCOMPLETE_RISK_ACCEPTANCE" and got.sub_reason == sub


def test_every_required_key_missing_is_named_by_its_key_and_the_values_never_leak():
    for key in K.REQUIRED_KEYS:
        record = risk()
        record.pop(key)
        got = classify(risk_detail(record))
        assert got.sub_reason == f"key_missing:{key}"
    got = classify(risk_detail(risk(rationale="secret text \u0007")))
    assert "secret" not in repr(got)


def test_a_trailing_newline_cannot_slip_through_a_dollar_anchor():
    assert classify(risk_detail(risk(approval_id=str(uuid.uuid4()) + "\n"))).sub_reason == "format:approval_id"
    assert classify(risk_detail(risk(applied_at="2026-01-01T00:00:01Z\n"))).sub_reason == "format:applied_at"


def test_the_decode_is_strict_and_a_failure_is_a_decode_error_anomaly():
    for text in ('{"tracked": true, "v": NaN}', '{"tracked": true, "v": Infinity}', '{"tracked": true', "{'tracked': 1}"):
        got = classify(text)
        assert got.is_anomaly and got.anomaly_kind == "OTHER_SCHEMA_INVALID" and got.sub_reason == "decode_error"
    assert K.decode_detail('{"a": 1.5, "b": 10000000000000000000000}') == {"a": Decimal("1.5"), "b": Decimal(10 ** 22)}


def test_a_risk_acceptance_without_a_pid_column_is_not_valid():
    assert classify(risk_detail(), pid=None).anomaly_kind == "INCOMPLETE_RISK_ACCEPTANCE"


def test_tracked_only_keys_are_disjoint_from_every_key_a_legacy_writer_produces():
    legacy = {"resumed", "termination", "permission_requests", "subtype", "api_error_status", "reason", "abandoned_by_takeover",
              "orchestrator_test_run", "lifecycle", "tree", "survivors"}          # step_runner, providers, lease, workflow (6.1)
    assert not P.TRACKED_ONLY_KEYS & legacy and len(P.TRACKED_ONLY_KEYS) == 9


def test_the_proof_allow_list_is_the_one_of_the_sql_text():
    from app.repositories import tracked_sql as S
    assert all(f"'{p}'" in S.RESOLVED_VALID_SQL for p in P.POSITIVE_PROOFS)
    assert set(P.POSITIVE_PROOFS) == {"JOB_EMPTY", "JOB_KILL_ON_CLOSE_ASSUMED", "NEVER_RAN"}


# --- TestPolicyService: the mapping of 7.2 on a stub executor and a stub port ---------------------------------------------------------
def result(outcome=CliOutcome.SUCCESS, lifecycle=ProcessLifecycle.EXITED, **kw) -> CommandResult:
    base = dict(argv=["x"], outcome=outcome, exit_code=0, stdout="", stderr="", duration_seconds=0.0, lifecycle=lifecycle,
                tree=TreeProof.EMPTY)
    base.update(kw)
    return CommandResult(**base)


class StubExecutor:
    tracking_enabled = True

    def __init__(self, *results, error=None):
        self.results, self.error, self.specs, self.cancelled = list(results), error, [], []

    async def run(self, spec, *, run_key=None, on_started=None, tracked=None):
        self.specs.append((spec, tracked, on_started))
        if tracked is not None:
            tracked.spawned = True
        if self.error is not None:
            raise self.error
        out = self.results.pop(0)
        if tracked is not None:
            tracked.result = out
        return out

    def cancel(self, key):
        self.cancelled.append(key)

    @contextmanager
    def tracked_scope(self, key):
        self.scopes = getattr(self, "scopes", []) + [key]
        yield


class StubPort(TrackedCommandPort):
    scope_key, checkpoint_step_key = "run:tracked:s", "TEST"

    def __init__(self):
        self.began, self.finished, self.capability_checked = [], [], 0

    def check_capability(self):
        self.capability_checked += 1

    async def begin_command(self):
        tracked = TrackedRun(uuid.uuid4(), run_key=f"run:tracked:s:{len(self.began)}")
        self.began.append(tracked)
        return tracked, (lambda identity: None)

    async def finish_command(self, tracked, raised):
        await asyncio.sleep(0.05)                       # slow enough for a cancellation to arrive meanwhile
        self.finished.append((tracked, raised))

    async def watch_cancel(self, cancel):
        await asyncio.sleep(3600)


POLICY = {"commands": [["python", "-c", "pass"], ["python", "-c", "pass"]], "timeout_seconds": 5, "junit_path": "r.xml",
          "min_tests_executed": 1, "env_allow": []}


async def test_unconfirmed_raises_with_the_checkpoint_after_the_row_is_settled_and_stops_the_sequence(tmp_path):
    executor, port = StubExecutor(result(CliOutcome.TIMEOUT, ProcessLifecycle.TERMINATION_UNCONFIRMED, termination="STILL_ALIVE")), StubPort()
    with pytest.raises(TerminationUnconfirmed) as info:
        await TestPolicyService(executor).run(tmp_path, POLICY, tracked=port)
    assert info.value.checkpoint_step_key == "TEST" and info.value.execution_id == port.began[0].execution_id
    assert len(port.finished) == 1 and len(executor.specs) == 1 and port.capability_checked == 1


@pytest.mark.parametrize("outcome,lifecycle,spawn_failed", [
    (CliOutcome.CANCELLED, ProcessLifecycle.TERMINATED, False), (CliOutcome.CANCELLED, ProcessLifecycle.NOT_STARTED, False),
    (CliOutcome.CANCELLED, ProcessLifecycle.SPAWN_FAILED, True)])
async def test_a_cancelled_command_raises_task_cancelled_and_nothing_is_parsed(tmp_path, outcome, lifecycle, spawn_failed):
    executor, port = StubExecutor(result(outcome, lifecycle, spawn_failed=spawn_failed)), StubPort()
    with pytest.raises(TaskCancelled):
        await TestPolicyService(executor).run(tmp_path, POLICY, tracked=port)
    assert len(port.finished) == 1


@pytest.mark.parametrize("reason", sorted(P.TRACKED_INFRA_SPAWN_REASONS))
async def test_an_infrastructure_spawn_failure_is_fail_closed_with_the_capability_code(tmp_path, reason):
    executor = StubExecutor(result(CliOutcome.ERROR, ProcessLifecycle.SPAWN_FAILED, spawn_failed=True, reason=reason))
    with pytest.raises(ProcessTrackingUnavailable) as info:
        await TestPolicyService(executor).run(tmp_path, POLICY, tracked=StubPort())
    assert isinstance(info.value, FailClosed) and info.value.message == "PROCESS_TRACKING_UNAVAILABLE"


async def test_an_oserror_spawn_failure_and_a_timeout_keep_their_meaning_as_a_failed_run(tmp_path):
    executor = StubExecutor(result(CliOutcome.ERROR, ProcessLifecycle.SPAWN_FAILED, spawn_failed=True, reason="SPAWN_OSERROR",
                                   exit_code=None))
    out = await TestPolicyService(executor).run(tmp_path, POLICY, tracked=StubPort())
    assert not out.passed and out.outcomes == ["ERROR"]
    timeout = StubExecutor(result(CliOutcome.TIMEOUT, ProcessLifecycle.TERMINATED, exit_code=P.JOB_TERMINATE_EXIT_CODE))
    out = await TestPolicyService(timeout).run(tmp_path, POLICY, tracked=StubPort())
    assert out.outcomes == ["TIMEOUT"] and out.exit_codes == [P.JOB_TERMINATE_EXIT_CODE]


async def test_collect_ids_keeps_none_for_a_timeout_and_the_node_ids_for_a_success(tmp_path):
    policy = dict(POLICY, commands=[["python", "-m", "pytest", "-q"]])
    ok = StubExecutor(result(stdout="a.py::t1\nnoise\nb.py::t2\n"))
    assert await TestPolicyService(ok).collect_ids(tmp_path, policy, tracked=StubPort()) == {"a.py::t1", "b.py::t2"}
    slow = StubExecutor(result(CliOutcome.TIMEOUT, ProcessLifecycle.TERMINATED))
    assert await TestPolicyService(slow).collect_ids(tmp_path, policy, tracked=StubPort()) is None


async def test_the_row_is_closed_even_when_the_run_raises_or_the_caller_is_cancelled(tmp_path):
    port = StubPort()
    with pytest.raises(RuntimeError):
        await TestPolicyService(StubExecutor(error=RuntimeError("boom"))).run(tmp_path, POLICY, tracked=port)
    assert len(port.finished) == 1 and isinstance(port.finished[0][1], RuntimeError)

    port = StubPort()
    work = asyncio.create_task(TestPolicyService(StubExecutor(error=asyncio.CancelledError())).run(tmp_path, POLICY, tracked=port))
    with pytest.raises(asyncio.CancelledError):
        await work
    assert len(port.finished) == 1 and isinstance(port.finished[0][1], asyncio.CancelledError)


async def test_shielded_finishes_the_work_when_cancelled_meanwhile_and_then_reraises():
    done = []

    async def work():
        await asyncio.sleep(0.1)
        done.append(1)

    outer = asyncio.create_task(_shielded(work()))
    await asyncio.sleep(0.01)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert done == [1]


async def test_without_a_port_the_commands_run_exactly_as_before(tmp_path):
    executor = StubExecutor(result(), result())
    out = await TestPolicyService(executor).run(tmp_path, POLICY)
    assert out.outcomes == ["SUCCESS", "SUCCESS"] and out.tracking is None
    assert all(tracked is None and on_started is None for _, tracked, on_started in executor.specs)
    assert not hasattr(executor, "scopes")                                          # no scope, no watcher for an untracked group


async def test_the_group_runs_inside_the_cancel_scope_of_its_port(tmp_path):
    executor = StubExecutor(result(), result())
    await TestPolicyService(executor).run(tmp_path, POLICY, tracked=StubPort())
    assert executor.scopes == ["run:tracked:s"]
