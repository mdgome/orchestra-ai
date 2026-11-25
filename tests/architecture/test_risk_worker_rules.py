"""B2 phase 5 architecture rules: the sweeper is serial and keyset-paged, the risk worker never ends a Task and wakes only after the
commit, and the page blocks keep the single-table shape of design 7.4."""
import ast
import re
from pathlib import Path

from app.repositories import tracked_sql as S

APP = Path(__file__).resolve().parents[2] / "app"


def source(relative: str) -> str:
    return (APP / relative).read_text(encoding="utf-8")


def test_the_page_blocks_read_executions_alone_with_a_keyset_limit_and_no_offset_or_join():
    blocks = {n: getattr(S, n) for n in dir(S) if re.fullmatch(r"(SWEEP_PAGE|RISK_PAGE_OWN|RISK_PAGE_REST|PROJECT_U_PAGE)_(FIRST|NEXT)_SQL", n)}
    assert len(blocks) == 8
    for name, sql in blocks.items():
        assert sql.count("FROM executions") == 1 and "LIMIT :page_size" in sql and "ORDER BY id" in sql, name
        assert not re.search(r"\bJOIN\b|\bOFFSET\b|:bounded_limit", sql, re.I), name
        assert ("id > :last_seen_id" in sql) == name.endswith("_NEXT_SQL"), name           # the first page has no cursor bound
    assert S.U_SQL in S.SWEEP_PAGE_FIRST_SQL and S.ANOMALY_SQL in S.SWEEP_PAGE_FIRST_SQL and S.JUDGEABLE_SQL in S.SWEEP_PAGE_FIRST_SQL
    assert "task_id = :task_id" in S.RISK_PAGE_OWN_FIRST_SQL and "task_id <> :task_id" in S.RISK_PAGE_REST_FIRST_SQL


def test_the_sweeper_processes_serially_and_never_spawns_tasks():
    tree = ast.parse(source("services/orphan_sweeper.py"))
    called = {getattr(n.func, "attr", getattr(n.func, "id", "")) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not called & {"gather", "create_task", "ensure_future", "TaskGroup", "sleep"}
    text = source("services/orphan_sweeper.py")
    assert "OFFSET" not in text.upper()


def test_the_worker_never_performs_a_terminal_transition_and_never_runs_os_work_inside_a_transaction():
    text = source("services/process_risk_worker.py")
    for forbidden in ("change_task_state", "state_service", "release_on_terminal", "task.finished", "finalizer"):
        assert forbidden not in text, forbidden
    tree = ast.parse(text)
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process")
    # the OS verification (_judge) is called outside every `async with self.db.transaction()` block of _process
    for node in ast.walk(func):
        if isinstance(node, ast.AsyncWith) and "transaction" in ast.unparse(node.items[0]) or isinstance(node, ast.AsyncWith) and "_section" in ast.unparse(node.items[0]):
            assert "_judge" not in ast.unparse(node)


def test_the_resume_wakeup_follows_the_commit():
    func = next(n for n in ast.walk(ast.parse(source("services/process_risk_worker.py")))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_result")
    notify = [n for n in ast.walk(func) if isinstance(n, ast.Call) and ast.unparse(n.func) == "self._notify"]
    assert len(notify) == 1
    outer = [n for n in func.body if isinstance(n, ast.AsyncWith)][0]            # the section / transaction block
    assert notify[0].lineno > outer.end_lineno                                    # strictly after the block that commits


def test_the_container_wires_both_bodies_into_the_runner():
    text = source("container.py")
    assert "sweep_pass=sweeper" in text and "risk_pass=risk_worker" in text and "background_db_section" in text
