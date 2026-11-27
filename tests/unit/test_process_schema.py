"""B2 phase 1: the frozen SQL text of migration 0005, the model and app/repositories/tracked_sql.py are one text.

`alembic check` does NOT compare the predicate of a partial index, so these string-equality tests are the protection (design
7.4, 'Single ownership of the SQL text'). The migration file name starts with digits and cannot be imported by name, so it is
loaded by path. No database is needed here; the integration tests compare the text the database really stores."""
import importlib.util
import re
from pathlib import Path

import pytest

from app.models import tables as T
from app.repositories import tracked_sql as S
from app.validation import policies as P

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0005_process_lifecycle_hardening.py"
FROZEN = ("TRACKED_ISH_SQL", "U_SQL", "RA_VALID_SQL", "RESOLVED_VALID_SQL", "ANOMALY_SQL")


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("migration_0005", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def predicate(index) -> str:
    return index.dialect_options["postgresql"]["where"].text


def index_named(table, name):
    return next(i for i in table.indexes if i.name == name)


def check_texts(table) -> dict:
    return {c.name: str(c.sqltext) for c in table.constraints if c.name and c.name.startswith("ck_")}


def test_migration_chain_is_0004_to_0005_and_a_later_migration_follows_it(migration):
    assert (migration.revision, migration.down_revision) == ("0005", "0004")
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    scripts = ScriptDirectory.from_config(Config(str(MIGRATION.parents[2] / "alembic.ini")))
    assert scripts.get_heads() == ["0006"]                                   # 0006 (test-only tasks) is the head now
    assert scripts.get_revision("0006").down_revision == "0005"             # and it follows this migration directly


@pytest.mark.parametrize("name", FROZEN)
def test_frozen_migration_text_equals_the_current_rendering(migration, name):
    assert getattr(migration, name) == getattr(S, name)


def test_model_index_predicates_are_the_canonical_text():
    assert predicate(index_named(T.Execution.__table__, "ix_executions_tracked_unresolved")) == S.U_SQL
    assert predicate(index_named(T.Execution.__table__, "ix_executions_tracked_anomaly")) == S.ANOMALY_SQL


def test_the_migration_creates_the_indexes_with_the_frozen_predicates(migration):
    source = MIGRATION.read_text(encoding="utf-8")
    assert "postgresql_where=sa.text(U_SQL)" in source and "postgresql_where=sa.text(ANOMALY_SQL)" in source
    assert source.count("'ix_executions_tracked_unresolved'") == 2 and source.count("'ix_executions_tracked_anomaly'") == 2
    assert migration.ANOMALY_SQL == ("(" + migration.TRACKED_ISH_SQL + ") AND ((" + migration.U_SQL + ") IS NOT TRUE) AND (("
                                     + migration.RESOLVED_VALID_SQL + ") IS NOT TRUE)")


def test_the_lineage_recheck_and_discovery_indexes_are_the_same_in_model_and_migration():
    source = MIGRATION.read_text(encoding="utf-8")
    lineage = index_named(T.Approval.__table__, "uq_approvals_lineage_successor")
    assert lineage.unique and predicate(lineage) == "(target ->> 'supersedes_approval_id') IS NOT NULL"
    assert predicate(lineage) in source and "[sa.text(\"(target ->> 'supersedes_approval_id')\")]" in source
    recheck = index_named(T.ProcessRiskRequest.__table__, "uq_process_risk_recheck_active")
    assert recheck.unique and [c.name for c in recheck.columns] == ["project_id", "dirty_epoch"]
    assert predicate(recheck) == "kind = 'RECHECK' AND status IN ('REQUESTED','VERIFYING','RETRYABLE')"
    assert predicate(recheck) in source
    finished = index_named(T.ProcessRiskRequest.__table__, "ix_process_risk_requests_project_finished")
    assert [c.name for c in finished.columns] == ["project_id", "finished_at"]
    assert predicate(finished) == "finished_at IS NOT NULL"
    status = index_named(T.ProcessRiskRequest.__table__, "ix_process_risk_requests_status_next_attempt")
    assert [c.name for c in status.columns] == ["status", "next_attempt_at"] and not status.unique


def test_schema_fixed_limits_are_the_same_numbers_in_policies_model_migration_and_sql():
    source = MIGRATION.read_text(encoding="utf-8")
    checks = check_texts(T.ProcessRiskRequest.__table__)
    assert checks["ck_process_risk_requests_requested_by"] == (
        "requested_by IS NULL OR char_length(requested_by) BETWEEN 1 AND 128")
    assert checks["ck_process_risk_requests_rationale"] == "char_length(rationale) BETWEEN 1 AND 2000"
    assert checks["ck_process_risk_requests_target_count"] == "cardinality(target_execution_ids) BETWEEN 1 AND 500"
    for text in checks.values():
        assert text in source, text                      # the migration carries the same literal, not a runtime constant
    assert (P.REQUESTED_BY_MAX_LENGTH, P.PROCESS_RISK_RATIONALE_MAX_LENGTH, P.PROCESS_RISK_TARGETS_DB_LIMIT) == (128, 2000, 500)
    assert "char_length(detail -> 'risk_acceptance' ->> 'rationale') BETWEEN 1 AND 2000" in S.RA_VALID_SQL
    assert "{1,128}" in P.RISK_ACCEPTANCE_REQUESTED_BY_RE and f"'{P.RISK_ACCEPTANCE_REQUESTED_BY_RE}'" in S.RA_VALID_SQL
    for name in ("RISK_ACCEPTANCE_UUID_RE", "RISK_ACCEPTANCE_TS_RE", "RISK_ACCEPTANCE_RATIONALE_FORBIDDEN"):
        assert f"'{getattr(P, name)}'" in S.RA_VALID_SQL, name


def test_the_check_constraint_value_sets_are_the_design_sets():
    checks = check_texts(T.ProcessRiskRequest.__table__)
    assert checks["ck_process_risk_requests_kind"] == "kind IN ('ACCEPT','RECHECK')"
    assert checks["ck_process_risk_requests_status"] == (
        "status IN ('REQUESTED','VERIFYING','APPLIED','REFUSED_ALIVE','RETRYABLE','NEEDS_REDECISION')")


def test_the_tracked_keys_and_required_keys_are_rendered_from_the_constants_and_the_pid_fragment_is_literal():
    keys = re.search(r"jsonb_exists_any\(detail, ARRAY\[(.*?)\]\)", S.TRACKED_ISH_SQL).group(1)
    assert set(re.findall(r"'([a-z_]+)'", keys)) == P.TRACKED_ONLY_KEYS and keys.count("'") == 18
    required = re.search(r"\?& ARRAY\[(.*?)\]", S.RA_VALID_SQL).group(1)
    assert set(re.findall(r"'([a-z_]+)'", required)) == P.RISK_ACCEPTANCE_REQUIRED_KEYS
    assert "requested_by" not in required
    assert S.PID_JSONB_SQL == "(pid::text)::jsonb" and S.PID_JSONB_SQL in S.RA_VALID_SQL
    assert S.PROJECT_SCOPE_SQL == "task_id IN (SELECT id FROM tasks WHERE project_id = :project_id)"


@pytest.mark.parametrize("name", FROZEN)
def test_index_predicate_blocks_contain_no_bind_and_no_astext(name):
    text = getattr(S, name)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]\w*", text), "a bind parameter inside an index predicate"
    assert "astext" not in text


def test_anomaly_uses_is_not_true_never_not():
    assert ") IS NOT TRUE" in S.ANOMALY_SQL and S.ANOMALY_SQL.count(" IS NOT TRUE") == 2
    # the only plain NOT is the key-existence test of the optional requested_by (a boolean that is never NULL)
    plain_nots = re.findall(r"(?<!IS )\bNOT\b[^\n]*", S.ANOMALY_SQL)
    assert plain_nots and all("? 'requested_by'" in n for n in plain_nots), plain_nots
    assert S.ANOMALY_SQL.startswith("(" + S.TRACKED_ISH_SQL + ") AND ((" + S.U_SQL + ") IS NOT TRUE)")


def test_no_column_for_execution_facts_is_added():
    source = MIGRATION.read_text(encoding="utf-8")
    assert "add_column" not in source and "alter_column" not in source and "drop_column" not in source
    assert re.findall(r"op\.create_table\('(\w+)'", source) == ["runtime_capabilities", "process_risk_requests"]
