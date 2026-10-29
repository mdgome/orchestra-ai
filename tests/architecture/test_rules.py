"""Architecture rules, checked mechanically.

SCOPE AND LIMITS: these tests detect *obvious* violations only (URI verbs, forbidden imports, loops/raises in
routers, DDD vocabulary, Generic Repository). They cannot judge design quality: whether real business logic has
leaked into a router in a subtle way, whether an abstraction is needless, or whether validation is semantically
duplicated. That judgement belongs to the CODE_REVIEWER (Codex) and to human review.
"""
import ast
import re
from pathlib import Path

import pytest

from app.main import create_app

APP = Path(__file__).resolve().parents[2] / "app"
VERBS = {"get", "create", "update", "delete", "remove", "execute", "run", "cancel", "resume", "start", "stop",
         "fetch", "set", "add", "post", "put", "patch", "do", "make", "send"}


def py_files(sub: str):
    return sorted((APP / sub).rglob("*.py"))


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
            mods.update(f"{node.module}.{a.name}" for a in node.names)
    return mods


def words(segment: str) -> list[str]:
    parts = re.split(r"[-_]", segment)
    out = []
    for p in parts:
        out += [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", p)]
    return out


# ---- REST naming ------------------------------------------------------------------------------------------
def test_uris_contain_nouns_only_and_actions_are_http_methods():
    schema = create_app(run_preflight=False).openapi()
    offenders = []
    for path in schema["paths"]:
        for segment in path.strip("/").split("/"):
            if segment.startswith("{"):
                continue
            offenders += [(path, w) for w in words(segment) if w in VERBS]
    assert not offenders, f"verbs in URIs: {offenders}"


def test_http_methods_follow_their_semantics():
    schema = create_app(run_preflight=False).openapi()
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            if method == "get":
                assert "requestBody" not in op, f"GET {path} must not take a body"
            if method == "post":
                codes = set(op["responses"])
                assert codes & {"200", "201", "202"}, f"POST {path} must define a success status"
            assert method in {"get", "post", "patch", "put", "delete"}


def test_expected_resource_endpoints_exist():
    paths = create_app(run_preflight=False).openapi()["paths"]
    for path, method in [("/tasks", "post"), ("/tasks/{task_id}", "get"), ("/tasks/{task_id}", "patch"),
                         ("/tasks/{task_id}/execution-requests", "post"),
                         ("/tasks/{task_id}/cancellation-requests", "post"),
                         ("/tasks/{task_id}/executions", "get"), ("/approvals/{approval_id}/decisions", "post"),
                         ("/conversations", "post"), ("/conversations/{conversation_id}/messages", "post")]:
        assert method in paths[path], (method, path)
    # the verb-style URIs from the original draft must not exist
    for banned in ("/tasks/{task_id}/resume", "/tasks/{task_id}/cancel", "/tasks/create"):
        assert banned not in paths


# ---- controller thinness ------------------------------------------------------------------------------------
FORBIDDEN_IN_ROUTERS = ("app.repositories", "app.models.tables", "sqlalchemy", "app.infra", "app.providers",
                        "app.validation.validators", "app.validation.policies", "app.db")


@pytest.mark.parametrize("path", py_files("routers"), ids=lambda p: p.name)
def test_routers_have_no_persistence_validation_or_business_logic(path):
    mods = imports_of(path)
    bad = [m for m in mods if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_IN_ROUTERS)]
    assert not bad, f"{path.name} imports {bad}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.For, ast.AsyncFor, ast.While)), f"loop in router {path.name}:{node.lineno}"
        assert not isinstance(node, (ast.ListComp, ast.DictComp, ast.SetComp)), f"comprehension in {path.name}"
        assert not isinstance(node, ast.Raise), f"router {path.name}:{node.lineno} raises (validation belongs elsewhere)"
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        assert len(fn.body) <= 10, f"{path.name}:{fn.name} is too big to be a thin controller"
        ifs = [n for n in ast.walk(fn) if isinstance(n, ast.If)]
        assert len(ifs) <= 1, f"{path.name}:{fn.name} has branching logic"


def test_routers_only_call_services_through_the_container():
    for path in py_files("routers"):
        src = path.read_text(encoding="utf-8")
        assert "validate_" not in src, f"{path.name} must not validate inline"


# ---- layering -------------------------------------------------------------------------------------------------------
LAYER_RULES = {
    "repositories": ("app.routers", "app.services", "app.providers", "app.schemas", "app.container"),
    "infra": ("app.routers", "app.services", "app.repositories", "app.schemas", "app.container"),
    "providers": ("app.routers", "app.services", "app.repositories", "app.schemas", "app.container"),
    "services": ("app.routers", "app.schemas", "app.container"),
    "validation": ("app.routers", "app.services", "app.repositories", "app.providers", "app.container"),
}


@pytest.mark.parametrize("layer", LAYER_RULES)
def test_layers_do_not_depend_upwards(layer):
    for path in py_files(layer):
        mods = imports_of(path)
        bad = [m for m in mods if any(m == f or m.startswith(f + ".") for f in LAYER_RULES[layer])]
        assert not bad, f"{layer}/{path.name} must not import {bad}"


# ---- no DDD, no Generic Repository, no needless abstraction ------------------------------------------------------------
def test_no_ddd_packages_or_vocabulary():
    banned_dirs = {"domain", "aggregates", "entities", "value_objects", "domain_services"}
    assert not [p for p in APP.rglob("*") if p.is_dir() and p.name in banned_dirs]
    pat = re.compile(r"class\s+\w*(Aggregate|ValueObject|DomainEvent|Specification|UnitOfWork)\b")
    for path in APP.rglob("*.py"):
        assert not pat.search(path.read_text(encoding="utf-8")), f"DDD construct in {path}"


def test_no_generic_repository():
    for path in py_files("repositories"):
        src = path.read_text(encoding="utf-8")
        assert "Generic[" not in src and "TypeVar" not in src, f"generic repository in {path.name}"
        assert not re.search(r"class\s+(Base|Generic)\w*Repository", src)


def test_repositories_never_commit():
    for path in py_files("repositories"):
        src = path.read_text(encoding="utf-8")
        assert ".commit(" not in src and ".rollback(" not in src and ".begin(" not in src, path.name


def test_only_two_protocols_exist_for_testability_and_session_store():
    """Abstractions are introduced where a real second implementation exists: providers, the session store."""
    protocols = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                    (isinstance(b, ast.Name) and b.id == "Protocol") for b in node.bases):
                protocols.append(node.name)
    assert sorted(protocols) == ["CliProvider", "SessionStore"], protocols


# ---- central validation -------------------------------------------------------------------------------------------------
ALLOWED_VALIDATE_OUTSIDE = {"app/services/artifacts.py"}     # validates *agent output*, not user input


def test_validate_functions_live_in_the_validation_module():
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP.parent).as_posix()
        if rel.startswith("app/validation/") or rel in ALLOWED_VALIDATE_OUTSIDE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name.startswith("validate_")]
        assert not names, f"{rel} defines validators {names}"


def test_validation_module_structure():
    for name in ("policies.py", "validators.py", "exceptions.py", "error_handlers.py"):
        assert (APP / "validation" / name).exists()


def test_limits_come_from_one_policy_module():
    """No magic limits duplicated in services: they must reference validation.policies."""
    for path in py_files("services"):
        src = path.read_text(encoding="utf-8")
        assert not re.search(r"(max_replans|max_workflow_iterations|max_step_attempts)\s*=\s*\d", src), path.name


def test_no_shell_execution_anywhere():
    for path in APP.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "shell=True" not in src and "create_subprocess_shell" not in src and "os.system(" not in src, path.name
