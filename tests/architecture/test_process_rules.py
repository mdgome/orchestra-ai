"""Architecture rules of B2 phase 1 (design 7.4 'Single ownership of the SQL text', 9.3, 14). Mechanical, obvious violations only."""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
B2_REPOSITORIES = ("runtime_capability_repository.py", "process_risk_repository.py")


def imported_modules(path: Path) -> set[str]:
    mods = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_the_sql_text_module_is_text_only_and_depends_on_the_policy_module_alone():
    mods = imported_modules(APP / "repositories" / "tracked_sql.py")
    assert mods == {"app.validation"}, mods            # `from app.validation import policies`: no sqlalchemy, no models, no session


def test_the_b2_validators_live_in_validators_py_and_the_runner_calls_the_linked_set():
    tree = ast.parse((APP / "validation" / "validators.py").read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"validate_process_timing_limits", "validate_process_input_constants", "validate_process_risk_limits",
            "validate_process_policy"} <= defined
    runner = (APP / "services" / "runner.py").read_text(encoding="utf-8")
    # R31-9: the construction is refused on any violated relation, through the ONE policy factory that runs the linked set
    assert "build_background_policy(" in runner
    factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_background_policy")
    assert "validate_process_policy()" in ast.unparse(factory)
    assert "validate_process_timing_limits()" in (APP / "main.py").read_text(encoding="utf-8")


def test_the_b2_repositories_are_concrete_classes_over_a_session_and_never_own_a_transaction():
    for name in B2_REPOSITORIES:
        path = APP / "repositories" / name
        src = path.read_text(encoding="utf-8")
        classes = [n for n in ast.parse(src).body if isinstance(n, ast.ClassDef)]
        assert len(classes) == 1 and not classes[0].bases, name          # no base class, no Generic, no Protocol
        assert "session.begin" not in src and ".commit(" not in src and ".rollback(" not in src, name
        assert "app.services" not in imported_modules(path) and "app.routers" not in imported_modules(path), name
