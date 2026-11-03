"""Test policy presets: validation, one-executable resolution, fresh JUnit results, environment and permissions."""
import json
import os
import sys
from pathlib import Path

import pytest

from app.infra.command_executor import CommandExecutor, CommandSpec
from app.infra.workspace_guard import WorkspaceGuard
from app.providers.base import ProviderRequest
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.permissions import ROLE_PERMISSIONS, is_test_path, permissions_for
from app.services import test_policy_resolver as resolver
from app.services.test_policy_resolver import resolve_test_policy
from app.services.test_policy_service import (
    TestPolicyService, fresh_results, parse_junit, parse_junit_files, result_snapshot,
)
from app.validation import policies as P
from app.validation.exceptions import ValidationFailed
from app.validation.validators import validate_test_policy

WINDOWS = os.name == "nt"
PASSING = '<testsuite tests="2"><testcase classname="a.B" name="one"/><testcase classname="a.B" name="two"/></testsuite>'


@pytest.fixture
def ws(tmp_path):
    return tmp_path


@pytest.fixture
def svc(ws):
    return TestPolicyService(CommandExecutor(WorkspaceGuard(ws)))


def policy_writing(files: dict[str, str], *, extra: str = "", junit="**/target/surefire-reports/TEST-*.xml", **kw):
    """A custom policy whose single command writes the given result files (stands in for `mvn test`)."""
    code = ("import os, sys\n" + "".join(
        f"os.makedirs(os.path.dirname({rel!r}) or '.', exist_ok=True); open({rel!r}, 'w').write({body!r})\n"
        for rel, body in files.items()) + extra)
    return validate_test_policy({"commands": [["python", "-c", code]], "junit_path": junit, **kw})


# --- validation --------------------------------------------------------------------------------------------------------------
def test_preset_form_is_accepted_and_normalised():
    assert validate_test_policy({"preset": "maven"}) == {"preset": "maven", "min_tests_executed": 1,
                                                          "timeout_seconds": 600}
    assert validate_test_policy({"preset": "gradle", "min_tests_executed": 3})["min_tests_executed"] == 3


@pytest.mark.parametrize("policy", [
    {"preset": "ant"}, {"preset": "maven", "commands": [["rm", "-rf", "/"]]}, {"preset": "maven", "junit_path": "x"},
    {"preset": "maven", "min_tests_executed": 0}, {"commands": [["mvnw.cmd", "test"]]},
    {"commands": [["C:\\proj\\run.BAT"]]}])
def test_unsafe_or_unknown_policies_are_rejected(policy):
    with pytest.raises(ValidationFailed):
        validate_test_policy(policy)


@pytest.mark.parametrize("junit", ["/etc/x.xml", "C:/x.xml", "../x.xml", "a/../../x.xml", "~/x.xml", "", "a//b.xml"])
def test_junit_path_must_stay_inside_the_workspace(junit):
    with pytest.raises(ValidationFailed):
        validate_test_policy({"commands": [["python", "-V"]], "junit_path": junit})


@pytest.mark.parametrize("names", [["MAVEN_*"], ["GITHUB_TOKEN"], ["DATABASE_URL"], ["JAVA_HOME", "ANTHROPIC_API_KEY"],
                                   "JAVA_HOME", [1]])
def test_env_allow_takes_exact_known_names_only(names):
    with pytest.raises(ValidationFailed):
        validate_test_policy({"commands": [["python", "-V"]], "env_allow": names})


def test_the_default_policy_is_unchanged_for_existing_callers():
    assert validate_test_policy(None) == {**P.DEFAULT_TEST_POLICY, "timeout_seconds": 600}


# --- resolution to ONE executable --------------------------------------------------------------------------------------------
def write_wrapper(ws: Path, name: str) -> Path:
    path = ws / name
    path.write_text("@echo off\r\n" if name.endswith(".cmd") else "#!/bin/sh\nexit 0\n", encoding="utf-8")
    if not WINDOWS:
        path.chmod(0o755)
    return path


@pytest.mark.parametrize("preset, win_name, posix_name", [("maven", "mvnw.cmd", "mvnw"),
                                                         ("gradle", "gradlew.bat", "gradlew")])
def test_the_projects_own_wrapper_wins_over_a_system_install(ws, monkeypatch, preset, win_name, posix_name):
    wrapper = write_wrapper(ws, win_name if WINDOWS else posix_name)
    monkeypatch.setattr(resolver.shutil, "which", lambda _t: "/usr/bin/system-tool")
    policy = resolve_test_policy({"preset": preset}, ws)
    argv = policy["commands"][0]
    assert argv[0] == str(wrapper.resolve()) and argv[1:] == P.TEST_PRESETS[preset]["args"]
    assert len(policy["commands"]) == 1 and "system-tool" not in json.dumps(policy)      # no "mvn or mvnw" alternatives
    assert policy["junit_path"] == P.TEST_PRESETS[preset]["junit_path"] and "preset" in policy
    assert policy["env_allow"] == P.TEST_PRESETS[preset]["env_allow"]


@pytest.mark.parametrize("preset, tool", [("maven", "mvn"), ("gradle", "gradle")])
def test_system_tool_is_used_when_there_is_no_wrapper(ws, monkeypatch, preset, tool):
    monkeypatch.setattr(resolver.shutil, "which", lambda name: f"/opt/{name}" if name == tool else None)
    assert resolve_test_policy({"preset": preset}, ws)["commands"][0][0] == f"/opt/{tool}"


@pytest.mark.parametrize("preset", ["maven", "gradle"])
def test_missing_toolchain_is_refused_when_the_task_is_created(ws, monkeypatch, preset):
    monkeypatch.setattr(resolver.shutil, "which", lambda _name: None)
    with pytest.raises(ValidationFailed) as err:
        resolve_test_policy({"preset": preset}, ws)
    assert err.value.details["preset"] == preset


def test_pytest_preset_resolves_to_the_interpreter_name(ws):
    policy = resolve_test_policy({"preset": "pytest"}, ws)
    assert policy["commands"] == [["python", "-m", "pytest", "-q", "--junitxml=reports/junit.xml"]]
    assert policy["env_allow"] == []


@pytest.mark.skipif(WINDOWS, reason="the executable bit is a POSIX notion")
def test_a_wrapper_without_the_execute_permission_is_refused(ws):
    (ws / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    (ws / "mvnw").chmod(0o644)
    with pytest.raises(ValidationFailed):
        resolve_test_policy({"preset": "maven"}, ws)


def test_a_wrapper_for_another_os_is_ignored(ws, monkeypatch):
    other = "mvnw" if WINDOWS else "mvnw.cmd"
    (ws / other).write_text("x", encoding="utf-8")
    monkeypatch.setattr(resolver.shutil, "which", lambda name: "/opt/mvn")
    assert resolve_test_policy({"preset": "maven"}, ws)["commands"][0][0] == "/opt/mvn"


def test_a_wrapper_that_is_a_link_is_refused(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / ("evil.cmd" if WINDOWS else "evil")
    outside.write_text("x", encoding="utf-8")
    link = ws / ("mvnw.cmd" if WINDOWS else "mvnw")
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    with pytest.raises(ValidationFailed):
        resolve_test_policy({"preset": "maven"}, ws)


async def test_a_resolved_wrapper_runs_without_a_shell_and_its_fresh_result_decides(ws, svc):
    """End to end on this OS: the project's own wrapper is executed directly (no shell string) and its JUnit file counts."""
    name = "mvnw.cmd" if WINDOWS else "mvnw"
    (ws / "prepared-result.src").write_text(PASSING, encoding="utf-8")        # what the "build" produces
    body = ("@echo off\r\nif not exist target\\surefire-reports mkdir target\\surefire-reports\r\n"
            'copy /Y "%~dp0prepared-result.src" target\\surefire-reports\\TEST-a.B.xml >nul\r\n' if WINDOWS else
            "#!/bin/sh\nmkdir -p target/surefire-reports\n"
            'cp "$(dirname "$0")/prepared-result.src" target/surefire-reports/TEST-a.B.xml\n')
    (ws / name).write_text(body, encoding="utf-8", newline="")
    if not WINDOWS:
        (ws / name).chmod(0o755)
    policy = resolve_test_policy({"preset": "maven"}, ws)
    result = await svc.run(ws, policy)
    assert result.passed and result.executed == 2 and result.exit_codes == [0], result.reason


def test_shell_use_stays_forbidden_in_the_test_policy_service():
    source = Path(resolver.__file__).read_text(encoding="utf-8") + \
        (Path(resolver.__file__).parent / "test_policy_service.py").read_text(encoding="utf-8")
    assert "shell=True" not in source and "os.system" not in source and "create_subprocess_shell" not in source


# --- fresh JUnit results ---------------------------------------------------------------------------------------------------------
async def test_results_of_an_earlier_run_never_count_and_are_never_deleted(ws, svc):
    stale = ws / "mod" / "target" / "surefire-reports" / "TEST-old.xml"
    stale.parent.mkdir(parents=True)
    stale.write_text(PASSING, encoding="utf-8")
    before = stale.read_bytes()
    policy = validate_test_policy({"commands": [["python", "-c", "print('did nothing')"]],
                                   "junit_path": "**/target/surefire-reports/TEST-*.xml"})
    result = await svc.run(ws, policy)
    assert not result.passed and "no new or updated" in result.reason
    assert stale.exists() and stale.read_bytes() == before                      # target/ is the user's area


async def test_a_new_file_is_collected_and_old_ones_stay_out(ws, svc):
    stale = ws / "old" / "target" / "surefire-reports" / "TEST-old.xml"
    stale.parent.mkdir(parents=True)
    stale.write_text('<testsuite><testcase classname="x" name="broken"><failure/></testcase></testsuite>')
    result = await svc.run(ws, policy_writing({"new/target/surefire-reports/TEST-new.xml": PASSING}))
    assert result.passed and result.executed == 2                               # the stale failure is not this run's
    assert stale.exists()


async def test_results_of_several_modules_are_summed(ws, svc):
    files = {f"m{i}/target/surefire-reports/TEST-{i}.xml": PASSING for i in range(3)}
    result = await svc.run(ws, policy_writing(files))
    assert result.passed and result.executed == 6


async def test_a_failure_in_any_fresh_module_fails_the_run(ws, svc):
    files = {"a/target/surefire-reports/TEST-a.xml": PASSING,
             "b/target/surefire-reports/TEST-b.xml": '<testsuite><testcase classname="b" name="x"><error/></testcase></testsuite>'}
    result = await svc.run(ws, policy_writing(files))
    assert not result.passed and result.failures == ["b::x"]


async def test_a_rewritten_file_with_identical_content_is_fresh(ws, svc):
    target = ws / "m" / "target" / "surefire-reports" / "TEST-a.xml"
    policy = policy_writing({"m/target/surefire-reports/TEST-a.xml": PASSING})
    assert (await svc.run(ws, policy)).passed
    first = target.stat().st_mtime_ns
    assert (await svc.run(ws, policy)).passed                                    # same bytes, new mtime: this run wrote it
    assert target.stat().st_mtime_ns >= first


async def test_an_unparseable_fresh_file_fails_and_all_skipped_never_passes(ws, svc):
    bad = await svc.run(ws, policy_writing({"m/target/surefire-reports/TEST-a.xml": "<not xml"}))
    assert not bad.passed and "JUnit" in bad.reason
    skipped = '<testsuite><testcase classname="a" name="s"><skipped/></testcase></testsuite>'
    result = await svc.run(ws, policy_writing({"m/target/surefire-reports/TEST-s.xml": skipped}))
    assert not result.passed and result.skipped == 1 and "executed" in result.reason


async def test_a_failing_command_fails_even_with_a_fresh_passing_file(ws, svc):
    policy = policy_writing({"m/target/surefire-reports/TEST-a.xml": PASSING}, extra="sys.exit(3)\n")
    result = await svc.run(ws, policy)
    assert not result.passed and result.exit_codes == [3] and result.executed == 2


def test_nested_suites_are_not_counted_twice(tmp_path):
    nested = tmp_path / "n.xml"
    nested.write_text('<testsuites><testsuite name="o"><testsuite name="i"><testcase classname="c" name="n"/></testsuite>'
                      '</testsuite></testsuites>', encoding="utf-8")
    assert parse_junit(nested) == (1, 0, 0, [])
    assert parse_junit_files([]) is None and parse_junit_files([nested, tmp_path / "missing.xml"]) is None


def test_snapshot_ignores_links_and_reports_changes_by_content(ws):
    f = ws / "t" / "TEST-a.xml"
    f.parent.mkdir()
    f.write_text("one", encoding="utf-8")
    a = result_snapshot(ws, "t/TEST-*.xml")
    f.write_text("two", encoding="utf-8")
    b = result_snapshot(ws, "t/TEST-*.xml")
    assert fresh_results(a, b) == ["t/TEST-a.xml"] and fresh_results(b, b) == []


# --- environment ------------------------------------------------------------------------------------------------------------------
SECRETS = {"GITHUB_TOKEN": "gh-secret", "DATABASE_URL": "pg://secret", "MAVEN_PASSWORD": "mvn-secret",
           "GRADLE_PUBLISH_SECRET": "gradle-secret", "AWS_SECRET_ACCESS_KEY": "aws-secret", "MAVEN_OPTS": "-Dx=y",
           "ANTHROPIC_API_KEY": "anthropic-secret", "OPENAI_API_KEY": "openai-secret", "JAVA_HOME": "/jdk"}
DUMP_ENV = "import json, os; print(json.dumps(sorted(os.environ)))"


def set_env(monkeypatch):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)


async def child_env(spec: CommandSpec, ws: Path) -> set[str]:
    result = await CommandExecutor(WorkspaceGuard(ws)).run(spec)
    return {k.upper() for k in json.loads(result.stdout)}


async def test_providers_get_only_the_allowlist_never_arbitrary_secrets(ws, monkeypatch):
    set_env(monkeypatch)
    seen = await child_env(CommandSpec(argv=[sys.executable, "-c", DUMP_ENV], cwd=ws), ws)
    assert not seen & {"GITHUB_TOKEN", "DATABASE_URL", "MAVEN_PASSWORD", "GRADLE_PUBLISH_SECRET",
                       "AWS_SECRET_ACCESS_KEY", "MAVEN_OPTS", "JAVA_HOME"}
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"} <= seen                       # the CLIs do need their own login


async def test_test_processes_get_no_cli_credentials_and_only_the_named_toolchain_variables(ws, monkeypatch):
    set_env(monkeypatch)
    spec = CommandSpec(argv=[sys.executable, "-c", DUMP_ENV], cwd=ws, drop_cli_auth=True,
                       extra_env={"JAVA_HOME": "/jdk"})
    seen = await child_env(spec, ws)
    assert "JAVA_HOME" in seen
    assert not seen & {"GITHUB_TOKEN", "DATABASE_URL", "MAVEN_PASSWORD", "GRADLE_PUBLISH_SECRET",
                       "AWS_SECRET_ACCESS_KEY", "MAVEN_OPTS", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}


async def test_test_policy_passes_only_env_allow_names_to_the_test_command(ws, svc, monkeypatch):
    set_env(monkeypatch)
    code = ("import json, os; os.makedirs('reports', exist_ok=True)\n"
            "open('env.json','w').write(json.dumps(sorted(os.environ)))\n"
            f"open('reports/junit.xml','w').write({PASSING!r})\n")
    policy = validate_test_policy({"commands": [["python", "-c", code]], "env_allow": ["JAVA_HOME"]})
    assert (await svc.run(ws, policy)).passed
    seen = {k.upper() for k in json.loads((ws / "env.json").read_text(encoding="utf-8"))}
    assert "JAVA_HOME" in seen
    assert not seen & {"GITHUB_TOKEN", "DATABASE_URL", "MAVEN_PASSWORD", "GRADLE_PUBLISH_SECRET", "MAVEN_OPTS",
                       "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY"}
    (ws / "env.json").unlink()
    plain = validate_test_policy({"commands": [["python", "-c", code]]})        # no env_allow: JAVA_HOME is not passed
    assert (await svc.run(ws, plain)).passed
    assert "JAVA_HOME" not in {k.upper() for k in json.loads((ws / "env.json").read_text(encoding="utf-8"))}


def test_no_wildcard_or_credential_name_can_enter_the_toolchain_allowlist():
    assert not [n for n in P.TEST_ENV_ALLOWED_NAMES if "*" in n or not n.isupper()]
    assert not [n for n in P.TEST_ENV_ALLOWED_NAMES if any(w in n for w in ("TOKEN", "PASSWORD", "SECRET", "KEY", "OPTS"))]
    for preset in P.TEST_PRESETS.values():
        assert set(preset["env_allow"]) <= P.TEST_ENV_ALLOWED_NAMES


# --- permissions and test paths -----------------------------------------------------------------------------------------------------
def test_pytest_keeps_the_original_permissions_exactly():
    assert permissions_for("TESTER") is ROLE_PERMISSIONS["TESTER"] and permissions_for("TESTER", "pytest") is ROLE_PERMISSIONS["TESTER"]
    assert permissions_for("DEVELOPER", None) is ROLE_PERMISSIONS["DEVELOPER"]
    assert is_test_path("tests/test_a.py") and is_test_path("pkg/test_b.py") and not is_test_path("src/test/A.java")


@pytest.mark.parametrize("preset", ["maven", "gradle"])
def test_jvm_presets_move_the_tester_to_src_test_and_its_own_test_goal(preset):
    tester = permissions_for("TESTER", preset)
    assert "Edit(src/test/**)" in tester.allowed and "Write(src/test/**)" in tester.allowed
    assert "Edit(tests/**)" not in tester.allowed and "Bash(python -m pytest:*)" not in tester.allowed
    assert set(P.TEST_PRESETS[preset]["tester_bash"]) <= set(tester.allowed)
    developer = permissions_for("DEVELOPER", preset)
    assert set(P.TEST_PRESETS[preset]["developer_bash"]) <= set(developer.allowed)
    assert set(ROLE_PERMISSIONS["DEVELOPER"].allowed) <= set(developer.allowed)
    assert not any(a.startswith("Edit(pom") or "wrapper" in a or a == "Edit" for a in tester.allowed)


@pytest.mark.parametrize("preset", ["maven", "gradle"])
def test_jvm_test_paths_exclude_build_files_and_wrappers(preset):
    for ok in ("src/test/java/com/x/FooTest.java", "mod/src/test/kotlin/A.kt", "src/main/java/com/x/FooTest.java",
               "src/main/java/TestHelper.java", "mod/FooIT.java"):
        assert is_test_path(ok, preset), ok
    for bad in ("pom.xml", "mvnw", "mvnw.cmd", "gradlew", "gradlew.bat", "build.gradle", "build.gradle.kts",
                "settings.gradle", ".mvn/wrapper/maven-wrapper.properties", "gradle/wrapper/gradle-wrapper.properties",
                "src/main/java/com/x/Foo.java", "tests/test_a.py"):
        assert not is_test_path(bad, preset), bad


@pytest.mark.parametrize("preset", ["maven", "gradle"])
def test_the_preset_reaches_the_claude_command_line(preset):
    provider = ClaudeCliProvider(None, "claude")
    argv = provider.build_argv(ProviderRequest(role="TESTER", prompt="p", cwd=Path("."), test_preset=preset), None)
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Edit(src/test/**)" in allowed and "python -m pytest" not in allowed
    default = provider.build_argv(ProviderRequest(role="TESTER", prompt="p", cwd=Path(".")), None)
    assert "Edit(tests/**)" in default[default.index("--allowedTools") + 1]
