import pytest

from app.infra.command_executor import CommandExecutor
from app.infra.workspace_guard import WorkspaceGuard
from app.services.test_policy_service import TestPolicyService, compute_test_signals, parse_junit
from app.validation import policies as P
from app.validation.validators import validate_test_policy


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "tests").mkdir()
    return tmp_path


@pytest.fixture
def svc(ws):
    return TestPolicyService(CommandExecutor(WorkspaceGuard(ws)))


POLICY = validate_test_policy(None)


def write(ws, name, body):
    (ws / "tests" / name).write_text(body, encoding="utf-8")


async def test_passing_tests_pass(svc, ws):
    write(ws, "test_ok.py", "def test_a():\n    assert 1 + 1 == 2\n")
    r = await svc.run(ws, POLICY)
    assert r.passed and r.executed == 1 and r.junit_ok and r.exit_codes == [0]


async def test_failing_test_is_reported_with_its_id(svc, ws):
    write(ws, "test_bad.py", "def test_a():\n    assert False\n")
    r = await svc.run(ws, POLICY)
    assert not r.passed and r.failures and "test_a" in r.failures[0]


async def test_all_skipped_is_never_a_pass(svc, ws):
    write(ws, "test_skip.py", "import pytest\n\n@pytest.mark.skip\ndef test_a():\n    pass\n")
    r = await svc.run(ws, POLICY)
    assert not r.passed and r.skipped == 1 and "executed" in r.reason


async def test_zero_tests_is_never_a_pass(svc, ws):
    write(ws, "test_none.py", "x = 1\n")
    r = await svc.run(ws, POLICY)
    assert not r.passed          # pytest exits 5 (no tests collected)


async def test_missing_junit_is_never_a_pass(svc, ws):
    policy = {**POLICY, "commands": [["python", "-c", "print('tests ok, trust me')"]]}
    r = await svc.run(ws, policy)
    assert not r.passed and "JUnit" in r.reason and r.exit_codes == [0]


async def test_unparseable_junit_is_never_a_pass(svc, ws):
    code = "import os; os.makedirs('reports', exist_ok=True); open('reports/junit.xml','w').write('<not xml')"
    r = await svc.run(ws, {**POLICY, "commands": [["python", "-c", code]]})
    assert not r.passed and "JUnit" in r.reason


async def test_stale_junit_from_a_previous_run_is_ignored(svc, ws):
    write(ws, "test_ok.py", "def test_a():\n    assert True\n")
    assert (await svc.run(ws, POLICY)).passed
    policy = {**POLICY, "commands": [["python", "-c", "print('did nothing')"]]}
    assert not (await svc.run(ws, policy)).passed          # old reports/junit.xml must not count


async def test_nonzero_exit_fails_even_if_junit_looks_fine(svc, ws):
    code = ("import os,sys; os.makedirs('reports', exist_ok=True); "
            "open('reports/junit.xml','w').write('<testsuite><testcase classname=\"a\" name=\"b\"/></testsuite>'); sys.exit(1)")
    r = await svc.run(ws, {**POLICY, "commands": [["python", "-c", code]]})
    assert not r.passed and r.exit_codes == [1]


async def test_collect_ids_and_removal_signal(svc, ws):
    write(ws, "test_two.py", "def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    before = await svc.collect_ids(ws, POLICY)
    assert {i.split("::")[-1] for i in before} == {"test_a", "test_b"}
    write(ws, "test_two.py", "def test_a():\n    pass\n")      # Tester quietly removed a test
    after = await svc.collect_ids(ws, POLICY)
    r = await svc.run(ws, POLICY)
    sig = compute_test_signals(before, after, r, ["tests/test_two.py"])
    assert r.passed                                             # changing tests is not itself a failure ...
    assert sig["removed_test_ids"] == [i for i in sorted(before) if i.endswith("test_b")]   # ... but is reported


def test_parse_junit_handles_testsuites_root(tmp_path):
    f = tmp_path / "j.xml"
    f.write_text('<testsuites><testsuite><testcase classname="c" name="n"><failure/></testcase></testsuite></testsuites>')
    assert parse_junit(f) == (1, 0, 1, ["c::n"])
    assert parse_junit(tmp_path / "missing.xml") is None
