"""Architecture rules of B2 phase 6 (design 14.1, 14.2, 6.2): the dependency direction of the runtime and diagnostics objects and
the read-only, bounded shape of the new diagnostics readers. Mechanical (AST and text) like the other architecture tests."""
import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
SERVICES = APP / "services"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def imported(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(source(path))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


@pytest.mark.parametrize("name", ["runner.py", "sweeper_stall.py", "background_supervisor.py", "runtime_status.py"])
def test_the_runner_side_modules_import_no_api_dto(name):
    """14.2: the Runner side owns the source state and never imports `app.schemas` (the dependency direction)."""
    assert not [m for m in imported(SERVICES / name) if m == "app.schemas" or m.startswith("app.schemas.")]


@pytest.mark.parametrize("name", ["diagnostics_service.py", "tracked_diagnostics.py", "capability_reader.py",
                                  "report_service.py"])
def test_no_diagnostics_or_report_service_imports_an_api_dto(name):
    assert not [m for m in imported(SERVICES / name) if m == "app.schemas" or m.startswith("app.schemas.")]


def test_the_diagnostics_service_takes_its_readers_as_plain_callables_and_touches_no_database():
    mods = imported(SERVICES / "diagnostics_service.py")
    assert not [m for m in mods if m.startswith(("sqlalchemy", "app.repositories", "app.db"))], mods
    src = source(SERVICES / "diagnostics_service.py")
    assert "read_capability" in src and "read_tracked" in src and "Protocol" not in src


def test_the_tracked_reader_reaches_the_project_wide_rows_only_through_the_common_entry_point():
    src = source(SERVICES / "tracked_diagnostics.py")
    assert "has_unresolved_or_anomalous_for_project" in src
    for forbidden in ("unresolved_tracked_executions_for_project", "anomalous_tracked_executions_for_project",
                      "unresolved_for_task", "FOR UPDATE", "JOIN", "OFFSET", ".detail", "detail_text", "set_owner", "release("):
        assert forbidden not in src, forbidden


def test_the_new_repository_queries_for_the_diagnostics_read_ids_and_never_join_tasks_with_executions():
    for name, marker in (("task_repository.py", "async def execution_facts"), ("task_repository.py", "async def task_statuses"),
                         ("task_repository.py", "async def runs_by_ids"),
                         ("process_risk_repository.py", "async def open_for_project")):
        text = source(APP / "repositories" / name)
        body = text.split(marker)[1].split("    async def ")[0]
        assert not re.search(r"\.join\(|\bJOIN\b|OFFSET|\.offset\(|with_for_update|FOR UPDATE", body, re.I), marker
    facts = source(APP / "repositories" / "task_repository.py").split("async def execution_facts")[1].split("async def ")[0]
    assert "Execution.detail" not in facts and "stdout" not in facts


def test_the_capability_reader_applies_the_whole_trust_rule_and_reads_the_pg_locks_holder():
    src = source(SERVICES / "capability_reader.py")
    for needle in ("pg_locks", "locktype = 'advisory'", "granted", "current_database()", "objsubid = 1",
                   "lock_backend_pid", "CAPABILITY_TTL_SECONDS"):
        assert needle in src, needle
    assert "INSERT" not in src and "upsert" not in src                      # a reader never writes the capability


def test_the_new_policy_constants_live_in_policies_and_have_a_central_validator():
    policies, validators = source(APP / "validation" / "policies.py"), source(APP / "validation" / "validators.py")
    for name in ("DIAGNOSTIC_MAX_RELATED_IDS", "DIAGNOSTIC_TRACKED_PROJECT_ROWS"):
        assert re.search(rf"^{name} = \d+", policies, re.M) and f'"{name}"' in validators
    assert re.search(r"^DIAGNOSTICS_POLICY_VERSION = 2\b", policies, re.M)


def test_the_reports_router_keeps_no_logic_after_phase_6():
    tree = ast.parse(source(APP / "routers" / "reports.py"))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp,
                                                              ast.GeneratorExp))]


def test_the_services_define_no_more_than_two_protocols_after_phase_6():
    count = sum(source(p).count("(Protocol)") for p in APP.rglob("*.py"))
    assert count <= 2, count
