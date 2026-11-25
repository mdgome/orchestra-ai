"""B2 phase 5b architecture rules (design 7.5 items 1, 4, 11, 14, 15): who may call the risk scans and the ordered lock, the decision
path has no OS work, the two lock-order statements, and the lock order inside the decision and the worker result transaction."""
import ast

from tests.architecture.test_tracked_rules import APP, calls_of, function_node


def test_the_risk_scans_and_the_target_lock_have_pinned_callers():
    assert calls_of("risk_batch_targets") == {("services/dirty_gate_options.py", "compute_gate_options")}
    assert calls_of("lock_risk_targets") == {("services/project_recovery_effect.py", "ProjectDirtyRecoveryEffect._create_risk_request")}
    assert calls_of("lock_executions_ordered") >= {("services/lease_service.py", "LeaseService._close_abandoned"),
                                                   ("services/process_risk_worker.py", "ProcessRiskWorker._result")}
    assert calls_of("lock_fenced") == {("services/process_risk_worker.py", "ProcessRiskWorker._result")}


def test_the_decision_and_options_modules_do_no_os_work():
    for relative in ("services/dirty_gate_options.py", "services/project_recovery_effect.py", "services/risk_followup.py"):
        text = (APP / relative).read_text(encoding="utf-8")
        for forbidden in ("psutil", "process_control", "to_thread", "subprocess", "terminate_tree", "check_alive"):
            assert forbidden not in text, (relative, forbidden)


def test_a_dynamic_gate_skips_the_generic_option_check_and_the_effect_validates_every_option_under_the_project_lock():
    decide = ast.unparse(function_node("services/human_gate_service.py", "HumanGateService._decide_once"))
    assert "allowed_options" in decide and "not dynamic" in decide
    apply = ast.unparse(function_node("services/project_recovery_effect.py", "ProjectDirtyRecoveryEffect.apply"))
    assert apply.index("projects.lock") < apply.index("self.allowed_options") < apply.index("_create_risk_request")
    assert apply.index("StaleProjectGate") < apply.index("self.allowed_options")          # the link check comes first


def test_the_risk_request_is_inserted_after_the_locked_snapshot_and_never_with_an_empty_snapshot():
    body = ast.unparse(function_node("services/project_recovery_effect.py", "ProjectDirtyRecoveryEffect._create_risk_request"))
    assert body.index("lock_risk_targets") < body.index("requests.insert")
    assert "if not survivors" in body and body.index("if not survivors") < body.index("requests.insert")
    assert "resume=True" not in body                                                       # the decision never creates a RESUME


def test_the_worker_result_transaction_locks_task_then_project_then_request_then_rows():
    body = ast.unparse(function_node("services/process_risk_worker.py", "ProcessRiskWorker._result"))
    assert body.index("_context_code") < body.index("lock_fenced") < body.index("lock_executions_ordered")
    context = ast.unparse(function_node("services/process_risk_worker.py", "ProcessRiskWorker._context_code"))
    assert context.index("tasks.lock") < context.index("projects.lock")                    # Task before Project


def test_the_follow_up_never_updates_an_approval_row_or_ends_the_task():
    text = (APP / "services/risk_followup.py").read_text(encoding="utf-8")
    for forbidden in ("mark_decided", "update(Approval", "change_task_state", "TaskState.FAILED"):
        assert forbidden not in text, forbidden
    assert {path for path, _ in calls_of("RiskFollowUp")} == {"container.py"}


def test_the_new_effect_signature_keeps_prepare_and_the_two_option_functions():
    effect = ast.parse((APP / "services/project_recovery_effect.py").read_text(encoding="utf-8"))
    methods = {n.name for c in ast.walk(effect) if isinstance(c, ast.ClassDef) and c.name == "ProjectDirtyRecoveryEffect"
               for n in c.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"prepare", "apply", "allowed_options", "allowed_options_batch"} <= methods


# --- phase 5c: items 10, 11, 14 and 15 ------------------------------------------------------------------------------------------------------------
def test_no_b2_write_updates_an_existing_approval_other_than_the_decision_and_the_permission_use():
    import re
    from tests.architecture.test_tracked_rules import trees
    found = set()
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "update" and node.args \
                    and "Approval" in ast.unparse(node.args[0]) and "ApprovalDecision" not in ast.unparse(node.args[0]):
                found.add((path.relative_to(APP).as_posix(), ast.unparse(node.args[0])))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r"UPDATE\s+approvals\b", node.value, re.I):
                found.add((path.relative_to(APP).as_posix(), "sql"))
    assert {p for p, _ in found} == {"repositories/approval_repository.py"}                 # mark_decided and consume_permission only
    repo = ast.parse((APP / "repositories/approval_repository.py").read_text(encoding="utf-8"))
    updaters = {fn.name for fn in ast.walk(repo) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and "update(Approval" in ast.unparse(fn)}
    assert updaters == {"mark_decided", "consume_permission"}


def test_the_supersede_and_the_fenced_redecision_have_pinned_callers_and_the_successor_lookup_uses_links_only():
    assert calls_of("supersede_applied") == {("services/workflow_service.py", "WorkflowService._row_project_mismatch"),
                                             ("services/workflow_service.py", "WorkflowService._risk_rows")}
    assert calls_of("redecide_if_status") == {("services/workflow_service.py", "WorkflowService._row_project_mismatch"),
                                              ("services/workflow_service.py", "WorkflowService._risk_rows")}
    lookup = ast.unparse(function_node("repositories/approval_repository.py", "ApprovalRepository.successors_of"))
    assert "supersedes_approval_id" in lookup and "order_by" not in lookup and "created_at" not in lookup


def test_the_handler_locks_task_then_project_then_writes_the_request_and_evaluates_the_tiers_in_the_common_order():
    resume = ast.unparse(function_node("services/workflow_service.py", "WorkflowService._resume_from_approval"))
    assert resume.index("TaskRepository(s).lock") < resume.index("_row_project_mismatch") < resume.index("_restore_lineage") \
        < resume.index("TASK_NOT_WAITING_APPROVAL") < resume.index("_recovery_unsafe") < resume.index("_risk_rows")   # 1, 6, L, 2, 3, ...
    rows = ast.unparse(function_node("services/workflow_service.py", "WorkflowService._risk_rows"))
    order = ["pending_for_task", "RESUME_REQUEST_MISMATCH", "RISK_REQUEST_NOT_APPLICABLE", "ProjectRepository(s).lock", "RISK_REQUEST_NOT_APPLIED",
             "DIRTY_EPOCH_CHANGED", "EXECUTION_IDENTITY_CHANGED", "LIVE_PROCESS_PRESENT", "NO_LIVE_PROCESS_UNPROVEN", "successors_of",
             "APPROVAL_LINEAGE_CONFLICT", "_proceed_resume", "redecide_if_status", "supersede_applied", "open_followup_gate"]
    positions = [rows.index(token) for token in order]
    assert positions == sorted(positions), dict(zip(order, positions))                     # 5, 4, 7, 8, 12, 13, 9, 10, 11, T9, 14
    assert rows.index("successors_of") < rows.index("open_followup_gate") and rows.index("successors_of") < rows.index("redecide_if_status")


def test_step_l_is_read_only_and_never_creates_a_lineage_root():
    text = ast.unparse(function_node("services/workflow_service.py", "WorkflowService._restore_lineage"))
    for forbidden in ("execute(", "update(", "add(", "open_recovery", "release", ".lock("):
        assert forbidden not in text, forbidden
    assert "ctx.resumed_from_approval_id = approval.id" in text and "LINEAGE_RESTORE_REFUSED" in text


def test_run_task_acts_on_the_outcome_after_the_commit_and_the_resume_never_parks_inside_the_method():
    run = ast.unparse(function_node("services/workflow_service.py", "WorkflowService.run_task"))
    assert "outcome = await self._resume_from_approval" in run and "ctx.parked = True" in run and "FailClosed(outcome.message" in run
    resume = ast.unparse(function_node("services/workflow_service.py", "WorkflowService._resume_from_approval"))
    assert "ctx.parked" not in resume


def test_the_lineage_race_is_detected_only_by_sqlstate_and_the_exact_constraint_name():
    from app.services.approval_lineage import is_lineage_violation
    from types import SimpleNamespace as NS
    cause = NS(sqlstate="23505", constraint_name="uq_approvals_lineage_successor")
    assert is_lineage_violation(NS(orig=NS(sqlstate="23505", __cause__=cause))) is True
    assert is_lineage_violation(NS(orig=NS(sqlstate="23505", __cause__=NS(sqlstate="23505", constraint_name="uq_approvals_project_dirty_pending")))) is False
    assert is_lineage_violation(NS(orig=NS(sqlstate="23505", __cause__=NS(sqlstate="23505")))) is False          # no constraint name
    assert is_lineage_violation(NS(orig=NS(sqlstate="23505", __cause__=None))) is False
    assert is_lineage_violation(NS(orig=NS(sqlstate="23503", __cause__=cause))) is False                         # a foreign key
    assert is_lineage_violation(NS(orig=None)) is False and is_lineage_violation(ValueError("x")) is False
