"""Structural rules of the project write lock / terminal path (obvious violations only, like the other rules).

These are SUPPORT for the integration tests: an AST check cannot prove that a terminal path is correct, it can only
stop the shortcuts that would bypass the common path.
"""
import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def parse(rel: str) -> ast.Module:
    return ast.parse((APP / rel).read_text(encoding="utf-8"))


def imports(rel: str) -> set[str]:
    mods = set()
    for node in ast.walk(parse(rel)):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
            mods.update(f"{node.module}.{a.name}" for a in node.names)
    return mods


def all_modules() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py"))


# --- dependency direction --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("module, forbidden", [
    ("services/state_service.py", ("app.services.project_lock_service", "app.services.task_finalizer")),
    ("services/project_lock_service.py", ("app.services.state_service", "app.services.task_finalizer")),
    ("services/project_terminal.py", ("app.services.state_service", "app.services.project_lock_service",
                                      "app.services.task_finalizer")),
    ("services/recovery_effects.py", ("app.services.project_lock_service", "app.services.project_terminal",
                                      "app.repositories.project_repository")),
    ("services/human_gate_service.py", ("app.services.project_lock_service", "app.services.project_terminal",
                                        "app.repositories.project_repository", "app.services.project_recovery_effect")),
])
def test_dependencies_point_one_way(module, forbidden):
    found = imports(module)
    assert not [f for f in forbidden if f in found], f"{module} must not import {forbidden}"


def test_lock_service_and_terminal_helper_do_not_change_task_state_or_open_transactions():
    for rel in ("services/project_lock_service.py", "services/project_terminal.py"):
        source = (APP / rel).read_text(encoding="utf-8")
        assert "change_task_state" not in source, f"{rel} must not change task state"
    terminal = (APP / "services/project_terminal.py").read_text(encoding="utf-8")
    assert "db.transaction" not in terminal and "compute_fingerprint" not in terminal, \
        "project_terminal opens no transaction and measures nothing"
    state = (APP / "services/state_service.py").read_text(encoding="utf-8")
    assert "compute_fingerprint" not in state and "transaction(" not in state


def test_the_lock_service_has_no_general_release_function():
    names = {n.name for n in ast.walk(parse("services/project_lock_service.py")) if isinstance(n, ast.AsyncFunctionDef)}
    assert "release_if_owner" not in names and "release" not in names and "release_on_terminal" not in names
    assert "reconcile" in names and "ensure_for_step" in names


def test_terminal_cleanup_has_exactly_one_owner():
    defining = [p for p in all_modules() if "def release_on_terminal" in p.read_text(encoding="utf-8")]
    assert [p.name for p in defining] == ["project_terminal.py"]
    callers = [p.name for p in all_modules()
               if "release_on_terminal(" in p.read_text(encoding="utf-8") and p.name != "project_terminal.py"]
    assert callers == ["state_service.py"]


# --- task status is changed in one place ----------------------------------------------------------------------------------------
def test_task_status_is_only_written_by_change_task_state():
    offenders = []
    for path in all_modules():
        if path.name == "state_service.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "values":
                if any(kw.arg == "status" for kw in node.keywords) and "Task" in ast.unparse(node.func.value) \
                        and "TaskRun" not in ast.unparse(node.func.value) and "TaskStep" not in ast.unparse(node.func.value):
                    offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "status"
                                                    and ast.unparse(t.value) in ("task", "locked", "t")
                                                    for t in node.targets):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"task status written outside change_task_state: {offenders}"


def test_every_terminal_transition_names_its_project_proof():
    """A terminal change_task_state call that omits project_proof could never prove CLEAN; make that visible."""
    missing = []
    for path in all_modules():
        if path.name == "state_service.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) ==
                    "change_task_state"):
                continue
            targets = {a.attr for a in node.args if isinstance(a, ast.Attribute)}
            if targets & TERMINAL and not any(kw.arg == "project_proof" for kw in node.keywords):
                missing.append(f"{path.name}:{node.lineno}")
    assert not missing, f"terminal transitions without project_proof=: {missing}"


def test_terminal_transitions_only_happen_in_the_known_orchestration_modules():
    allowed = {"workflow_service.py", "human_gate_service.py", "runner.py", "project_recovery_effect.py", "task_finalizer.py"}
    where = set()
    for path in all_modules():
        if path.name == "state_service.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "change_task_state":
                if {a.attr for a in node.args if isinstance(a, ast.Attribute)} & TERMINAL:
                    where.add(path.name)
    assert where <= allowed, f"unexpected terminal transitions in {sorted(where - allowed)}"


# --- workspace modules: snapshot and fingerprint are independent siblings above two common modules -----------------------------
def test_snapshot_and_fingerprint_do_not_import_each_other_and_common_modules_import_neither():
    """AST check (environment independent): the dependency direction is policy/digest -> snapshot/fingerprint."""
    assert not [m for m in imports("infra/workspace_snapshot.py") if m.startswith("app.infra.workspace_fingerprint")]
    assert not [m for m in imports("infra/workspace_fingerprint.py") if m.startswith("app.infra.workspace_snapshot")]
    for common in ("infra/workspace_policy.py", "infra/workspace_digest.py"):
        found = imports(common)
        assert not [m for m in found if m.startswith(("app.infra.workspace_snapshot", "app.infra.workspace_fingerprint"))], \
            f"{common} must not import a higher workspace module"
    digest = imports("infra/workspace_digest.py")
    assert not [m for m in digest if m.startswith("app.")], "workspace_digest is standard library only"
    assert not [m for m in imports("infra/workspace_policy.py") if m.startswith("app.") and m != "app.validation"
                and not m.startswith("app.validation.policies")]


def test_common_workspace_modules_know_no_domain_exceptions():
    forbidden = {"SnapshotError", "FingerprintError", "FailClosed"}
    for rel in ("infra/workspace_policy.py", "infra/workspace_digest.py"):
        used = set()
        for node in ast.walk(parse(rel)):                      # code only: docstrings may mention the names
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                used.update(a.name.split(".")[-1] for a in node.names)
        assert not used & forbidden, f"{rel} references a domain exception: {used & forbidden}"


@pytest.mark.parametrize("module", ["app.infra.workspace_snapshot", "app.infra.workspace_fingerprint",
                                    "app.infra.workspace_policy", "app.infra.workspace_digest"])
def test_each_workspace_module_imports_on_its_own(module):
    """Isolated interpreter with the project root put on sys.path explicitly (`-I` ignores PYTHONPATH and the cwd), so
    a missing package is not mistaken for a circular import. A failure says which kind it is."""
    import subprocess
    import sys
    root = str(APP.parent)
    code = f"import sys; sys.path.insert(0, {root!r}); import {module}"
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    if done.returncode == 0:
        return
    trace = done.stderr
    if "circular import" in trace or "partially initialized module" in trace:
        pytest.fail(f"ARCHITECTURE failure (circular import) importing {module}:\n{trace}")
    if "ModuleNotFoundError" in trace or "ImportError" in trace:
        pytest.fail(f"ENVIRONMENT or import failure importing {module} (not a cycle):\n{trace}")
    pytest.fail(f"unexpected failure importing {module}:\n{trace}")


def _attribute_calls(name_prefix: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in all_modules():
        rel = path.relative_to(APP).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr.startswith(name_prefix):
                found.setdefault(rel, []).append(node.func.attr)
    return found


def test_request_recovery_helpers_are_only_called_inside_the_runner():
    assert set(_attribute_calls("_recover_requests")) <= {"services/runner.py"}


def test_runner_recovery_is_only_called_from_the_runner_itself():
    assert set(_attribute_calls("recover")) <= {"services/runner.py"}
