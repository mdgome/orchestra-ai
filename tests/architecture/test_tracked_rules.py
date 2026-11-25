"""Architecture rules of B2 phase 3 (design 7.4, 7.5 item 15, 9.3, 14): the writers of `workspace_state='CLEAN'`, the callers of the
project-wide tracked-row queries, the ordered row locks, the validator ownership and the definition of TRACKED_ONLY_KEYS.
Mechanical and obvious violations only (AST and text), like the other architecture tests."""
import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"


def trees():
    for path in sorted(APP.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def calls_of(name: str, *, keyword: str | None = None) -> set[tuple[str, str]]:
    """(relative path, enclosing `Class.function`) of every call of an attribute or function called `name`."""
    found = set()

    def visit(node, scope, path):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = scope + [node.name]
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if called == name and (keyword is None or any(k.arg == keyword for k in node.keywords)):
                found.add((path.relative_to(APP).as_posix(), ".".join(scope)))
        for child in ast.iter_child_nodes(node):
            visit(child, scope, path)

    for path, tree in trees():
        visit(tree, [], path)
    return found


def function_node(relative: str, qualname: str) -> ast.AST:
    tree = ast.parse((APP / relative).read_text(encoding="utf-8"))
    scope = tree
    for part in qualname.split("."):
        scope = next(n for n in ast.walk(scope) if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == part)
    return scope


def first_line_of_call(node: ast.AST, name: str) -> int:
    lines = [c.lineno for c in ast.walk(node) if isinstance(c, ast.Call)
             and (getattr(c.func, "attr", None) == name or getattr(c.func, "id", None) == name)]
    assert lines, name
    return min(lines)


# --- every writer of workspace_state='CLEAN' is pinned (I6) -----------------------------------------------------------------------
def test_the_release_of_the_project_write_lock_has_exactly_two_callers():
    assert calls_of("release", keyword="state") == {
        ("services/project_terminal.py", "release_on_terminal"),
        ("services/project_lock_service.py", "ProjectLockService._reconcile_once")}


def test_accept_baseline_has_one_caller_behind_the_tracked_guard():
    assert calls_of("accept_baseline") == {("services/project_recovery_effect.py", "ProjectDirtyRecoveryEffect.apply")}
    apply = function_node("services/project_recovery_effect.py", "ProjectDirtyRecoveryEffect.apply")
    assert first_line_of_call(apply, "assert_no_unresolved_or_anomalous_tracked") < first_line_of_call(apply, "accept_baseline")


def test_set_owner_is_called_only_from_the_acquisition_path_and_only_behind_the_guard():
    assert calls_of("set_owner") == {("services/project_lock_service.py", "ProjectLockService._acquire_once")}
    acquire = function_node("services/project_lock_service.py", "ProjectLockService._acquire_once")
    guard = first_line_of_call(acquire, "has_unresolved_or_anomalous_for_project")
    assert guard < first_line_of_call(acquire, "set_owner")
    assert guard > first_line_of_call(acquire, "_open_dirty_gate")                  # after the owned / non-CLEAN branches
    assert first_line_of_call(acquire, "mark_dirty") > guard                        # a refused owner marks the project dirty


def test_the_release_paths_ask_the_common_entry_point_before_they_release():
    for relative, qual in (("services/project_terminal.py", "release_on_terminal"),
                           ("services/project_lock_service.py", "ProjectLockService._reconcile_once")):
        node = function_node(relative, qual)
        assert first_line_of_call(node, "has_unresolved_or_anomalous_for_project") < first_line_of_call(node, "release")


def test_the_only_literal_clean_writers_are_the_pinned_ones():
    """Every `\"CLEAN\"` literal of the application outside a comparison: the model default, `set_owner`, `accept_baseline`, the
    two release decisions and the initial INSERT of ProjectService.create (the single creation exception)."""
    writers = set()
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "CLEAN":
                writers.add(path.relative_to(APP).as_posix())
    assert writers == {"models/tables.py", "repositories/project_repository.py", "services/diagnostics_service.py",
                       "services/project_lock_service.py", "services/project_recovery_effect.py",
                       "services/project_service.py", "services/project_terminal.py", "validation/policies.py"}
    repo = (APP / "repositories" / "project_repository.py").read_text(encoding="utf-8")
    assert repo.count('workspace_state="CLEAN"') == 2                               # set_owner and accept_baseline only
    assert (APP / "services" / "project_service.py").read_text(encoding="utf-8").count('workspace_state="CLEAN"') == 1


def test_update_fields_accepts_only_the_known_keys_and_never_a_lock_column():
    allowed = {"profile_version", "name", "test_policy", "status", "profile", "description"}
    forbidden = {"workspace_state", "write_owner_task_id", "write_baseline_fingerprint", "baseline_fingerprint", "write_epoch"}
    assert {path for path, _ in calls_of("update_fields")} == {"services/profile_service.py", "services/project_service.py"}
    for path, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "update_fields":
                keys = {k.arg for k in node.keywords if k.arg}
                assert not keys & forbidden, (path.name, keys)
    source = (APP / "services" / "project_service.py").read_text(encoding="utf-8")
    values_keys = set(re.findall(r'values\["(\w+)"\]', source))
    assert values_keys and not values_keys & forbidden and values_keys <= allowed | {"workspace_path"}, values_keys


# --- the project-wide tracked-row queries -------------------------------------------------------------------------------------------
def test_the_two_project_wide_queries_are_called_only_from_the_common_entry_point():
    helper = {("services/project_terminal.py", "has_unresolved_or_anomalous_for_project")}
    assert calls_of("unresolved_tracked_executions_for_project") == helper
    assert calls_of("anomalous_tracked_executions_for_project") == helper
    node = function_node("services/project_terminal.py", "has_unresolved_or_anomalous_for_project")
    assert first_line_of_call(node, "unresolved_tracked_executions_for_project") and \
        first_line_of_call(node, "anomalous_tracked_executions_for_project")          # one function asks BOTH sets


def test_the_common_entry_point_has_exactly_one_project_parameter_and_no_default():
    for name in ("has_unresolved_or_anomalous_for_project", "assert_no_unresolved_or_anomalous_tracked"):
        args = function_node("services/project_terminal.py", name).args
        assert [a.arg for a in args.args] == ["session", "project_id"], name
        assert not args.defaults and not args.kwonlyargs and not args.kw_defaults and not args.vararg and not args.kwarg, name


def test_the_task_scoped_lookup_is_never_used_by_a_clean_decision():
    for relative in ("services/project_terminal.py", "services/project_lock_service.py", "services/project_recovery_effect.py",
                     "services/project_service.py", "repositories/project_repository.py", "services/task_finalizer.py",
                     "services/state_service.py"):
        assert "unresolved_for_task" not in (APP / relative).read_text(encoding="utf-8"), relative
    # phase 4: `Runner._tracked_checkpoint_key` (`_park_uncertain`) reads the checkpoint key of the Task's own unresolved tracked
    # row; it is not a CLEAN decision (design 8) and is the only non-view caller
    assert all(path.startswith(("services/diagnostics", "services/report", "services/runner")) for path, _ in calls_of("unresolved_for_task"))


def test_the_tracked_queries_read_executions_alone_with_no_join_and_no_offset():
    from app.repositories import tracked_sql as S
    for name in ("PROJECT_U_QUERY_SQL", "PROJECT_A_QUERY_SQL", "TASK_U_QUERY_SQL"):
        sql = getattr(S, name)
        assert sql.count("FROM executions") == 1 and not re.search(r"\bJOIN\b|\bOFFSET\b", sql, re.I), name
    for name in ("PROJECT_U_QUERY_SQL", "PROJECT_A_QUERY_SQL"):
        assert S.PROJECT_SCOPE_SQL in getattr(S, name) and "FROM tasks" in S.PROJECT_SCOPE_SQL
    repo = (APP / "repositories" / "task_repository.py").read_text(encoding="utf-8")
    assert not re.search(r"\.offset\(|OFFSET", repo.split("# --- tracked executions")[1], re.I)


# --- ordered row locks (7.5 item 15) ----------------------------------------------------------------------------------------------------
def test_only_the_ordered_lock_method_issues_for_update_on_executions():
    texts = {}
    for path, tree in trees():
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                      and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings                     and "FOR UPDATE" in node.value:
                texts[path.relative_to(APP).as_posix()] = (APP / path).read_text(encoding="utf-8")
    assert set(texts) == {"repositories/tracked_sql.py"}                           # the one statement, as executable text
    assert "FOR UPDATE" in texts["repositories/tracked_sql.py"].split("LOCK_EXECUTIONS_ORDERED_SQL")[1]
    users = {path for path, _ in calls_of("with_for_update")}
    assert users <= {"repositories/task_repository.py", "repositories/project_repository.py",
                     "repositories/approval_repository.py", "repositories/session_store.py",
                     "repositories/postgres_session_store.py", "repositories/profile_repository.py",
                     "repositories/conversation_repository.py", "repositories/baseline_repository.py",
                     "repositories/process_risk_repository.py"}                  # lock_fenced: the REQUEST row, never executions
    for relative in ("repositories/task_repository.py",):
        for node in ast.walk(ast.parse((APP / relative).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "with_for_update":
                assert "Execution" not in ast.unparse(node), "FOR UPDATE on executions only through lock_executions_ordered"
    assert calls_of("lock_executions_ordered") >= {("services/lease_service.py", "LeaseService._close_abandoned")}


def test_the_ordered_lock_method_sorts_and_checks_the_returned_order():
    node = function_node("repositories/task_repository.py", "TaskRepository.lock_executions_ordered")
    source = ast.unparse(node)
    assert "sorted(set(ids))" in source and "locked != sorted(locked)" in source


def test_no_unordered_id_loop_surrounds_an_execution_update_in_the_takeover():
    node = function_node("services/lease_service.py", "LeaseService._close_abandoned")
    loops = [n for n in ast.walk(node) if isinstance(n, ast.For)]
    assert [ast.unparse(n.iter) for n in loops] == ["locked"]                       # the loop runs over the ordered result
    acquire = function_node("services/lease_service.py", "LeaseService.acquire")
    assert not any("update(Execution)" in ast.unparse(n) or "UPDATE executions" in ast.unparse(n)
                   for n in ast.walk(acquire) if isinstance(n, ast.For))
    assert "pid IS NULL" in ast.unparse(node)                                       # the guard of the NEVER_RAN closing write


# --- validator ownership (the table of 14, revision 22) ----------------------------------------------------------------------------------
OWNERSHIP = {
    "validate_process_timing_limits": (
        "CANDIDATE_SCAN_MAX_SECONDS", "CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS", "ORPHAN_SWEEP_PASS_SECONDS",
        "ORPHAN_SWEEP_INTERVAL_SECONDS", "ORPHAN_SWEEP_BACKOFF_MIN_SECONDS", "ORPHAN_SWEEP_BACKOFF_MAX_SECONDS",
        "ORPHAN_SWEEP_LOG_THROTTLE_SECONDS", "ORPHAN_SWEEP_MAX_INTERVAL_SECONDS", "ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS",
        "PROCESS_RISK_TARGET_LOCK_SECONDS", "PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS",
        "PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS", "JOB_CLOSE_SETTLE_SECONDS", "BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS",
        "BACKGROUND_RESTART_BACKOFF_BASE_SECONDS", "BACKGROUND_RESTART_BACKOFF_CAP_SECONDS", "BACKGROUND_MAX_RESTARTS",
        "BACKGROUND_STABLE_SECONDS", "BACKGROUND_RESTART_WINDOW_SECONDS", "BACKGROUND_RESTART_WINDOW_MAX"),
    "validate_process_input_constants": ("ORPHAN_SWEEP_STALL_THRESHOLD", "ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP",
                                         "BACKGROUND_COMPONENT_IDS", "REQUESTED_BY_MAX_LENGTH",
                                         "PROCESS_RISK_RATIONALE_MAX_LENGTH", "JOB_TERMINATE_EXIT_CODE",
                                         "DIAGNOSTIC_MAX_ANOMALIES"),
    "validate_process_risk_limits": ("PROCESS_RISK_IDLE_INTERVAL_SECONDS", "PROCESS_RISK_BACKOFF_BASE_SECONDS",
                                     "PROCESS_RISK_MAX_ATTEMPTS", "PROCESS_RISK_BATCH_REQUESTS", "PROCESS_RISK_BATCH_ROWS",
                                     "JOB_CLOSE_SETTLE_SECONDS"),
}


def test_every_constant_has_its_validator_as_the_table_of_ownership_says():
    tree = ast.parse((APP / "validation" / "validators.py").read_text(encoding="utf-8"))
    source = {n.name: ast.unparse(n) for n in tree.body if isinstance(n, ast.FunctionDef)}
    # phase 4: the sweeper pass members were split into `_sweeper_pass_checks` (one definition shared with the pass snapshot)
    source["validate_process_timing_limits"] += source["_sweeper_pass_checks"]
    missing = [(fn, c) for fn, names in OWNERSHIP.items() for c in names if c not in source[fn]]
    assert not missing, missing
    policies = (APP / "validation" / "policies.py").read_text(encoding="utf-8")
    assert all(re.search(rf"^{c}\b", policies, re.M) for names in OWNERSHIP.values() for c in names)
    # the three validators are one linked set: the policy factory calls them together, and nothing else defines them
    assert all(src.count("validate_process_") >= 3 for name, src in source.items() if name == "validate_process_policy")
    assert {n for n in source if n.startswith("validate_process_")} == {
        "validate_process_timing_limits", "validate_process_risk_limits", "validate_process_input_constants",
        "validate_process_policy"}


# --- TRACKED_ONLY_KEYS is defined once --------------------------------------------------------------------------------------------------
def test_tracked_only_keys_is_defined_in_policies_py_alone_and_every_consumer_imports_it():
    definitions = []
    for path, tree in trees():
        for node in ast.walk(tree):
            targets = [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)] if isinstance(node, ast.Assign) \
                else [node.target.id] if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) else []
            if "TRACKED_ONLY_KEYS" in targets:
                definitions.append(path.relative_to(APP).as_posix())
    assert definitions == ["validation/policies.py"]
    for relative in ("repositories/tracked_sql.py", "services/tracked_classifier.py", "services/lease_service.py"):
        assert "P.TRACKED_ONLY_KEYS" in (APP / relative).read_text(encoding="utf-8"), relative


def test_the_python_classifier_is_pure_and_depends_on_the_policy_module_alone():
    imported = set()
    for node in ast.walk(ast.parse((APP / "services" / "tracked_classifier.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module if node.module.startswith("app") else node.module.split(".")[0])
    assert imported == {"json", "re", "uuid", "dataclasses", "datetime", "decimal", "enum", "app.validation"}
