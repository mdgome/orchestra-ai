"""Project registration, workspace validation, project tasks and running a workflow in a project workspace."""
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.container import Container
from app.infra.command_executor import CommandExecutor, CommandResult
from app.main import create_app
from app.models.states import CliOutcome
from app.models.tables import Project
from app.services.project_service import ProjectService
from tests.api.test_api import assert_envelope
from tests.harness import GOOD_CODE, Harness


class FakeCloneExecutor:
    """Stands in for git: the clone itself is not what is under test, the registration around it is."""

    def __init__(self, mode: str = "ok"):
        self.mode = mode
        self.calls: list = []

    async def run(self, spec, **_kw) -> CommandResult:
        self.calls.append(spec)
        target = Path(spec.argv[-1])
        if self.mode == "ok":
            (target / "README.md").write_text("cloned", encoding="utf-8")
            return CommandResult(spec.argv, CliOutcome.SUCCESS, 0, "", "", 0.1)
        if self.mode == "fail":
            (target / "partial.tmp").write_text("x", encoding="utf-8")
            return CommandResult(spec.argv, CliOutcome.ERROR, 128, "",
                                 "fatal: unable to access 'https://user:s3cretT0ken@host/x.git/'", 0.1)
        return CommandResult(spec.argv, CliOutcome.TIMEOUT, None, "", "", 0.1, termination="STILL_ALIVE")


@pytest.fixture
async def env(db, tmp_path):
    ws, projects, clones = tmp_path / "ws", tmp_path / "projects", tmp_path / "clones"
    projects.mkdir()
    h = Harness(db, ws)
    clone_exec = FakeCloneExecutor()
    service = ProjectService(db, clone_exec, clone_root=clones, protected=(h.guard.root,))
    container = Container(settings=Settings(), db=db, guard=h.guard, executor=None, events=h.events, tasks=h.tasks,
                          gates=h.gates, runner=h.runner, providers={"CLAUDE": h.claude, "CODEX": h.codex},
                          projects=service)
    app = create_app(container=container, run_preflight=False)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, h, projects, clone_exec, clones


def make_dir(parent: Path, name: str) -> Path:
    path = parent / name
    path.mkdir()
    return path


async def register(client, name, path):
    return await client.post("/projects", json={"name": name, "workspace_path": str(path)})


# --- registration of an existing local workspace ---------------------------------------------------------------
async def test_register_get_list_and_patch(env):
    client, _h, projects, *_ = env
    svc = make_dir(projects, "service-a")
    created = await register(client, "Service A", svc)
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "ACTIVE" and body["workspace_state"] == "CLEAN" and body["repository_url"] is None
    assert Path(body["workspace_path"]) == svc.resolve()

    assert (await client.get(f"/projects/{body['id']}")).json()["name"] == "Service A"
    assert [p["id"] for p in (await client.get("/projects")).json()] == [body["id"]]
    patched = await client.patch(f"/projects/{body['id']}", json={"name": "Renamed"})
    assert patched.status_code == 200 and patched.json()["name"] == "Renamed"
    assert (await client.get(f"/projects/{os.urandom(16).hex()[:8]}-x")).status_code == 422
    assert_envelope(await client.get("/projects/00000000-0000-0000-0000-000000000000"), 404, "NOT_FOUND")


async def test_duplicate_and_nested_workspaces_are_rejected(env):
    client, _h, projects, *_ = env
    parent = make_dir(projects, "mono")
    child = make_dir(parent, "svc")
    assert (await register(client, "parent", parent)).status_code == 201
    assert_envelope(await register(client, "same", parent), 409, "CONFLICT")
    assert_envelope(await register(client, "child", child), 409, "CONFLICT")        # below a registered project
    sibling = make_dir(projects, "mono-other")
    assert (await register(client, "sibling", sibling)).status_code == 201           # prefix match is not nesting


async def test_registering_a_parent_of_a_registered_project_is_rejected(env):
    client, _h, projects, *_ = env
    parent = make_dir(projects, "outer")
    child = make_dir(parent, "inner")
    assert (await register(client, "inner", child)).status_code == 201
    assert_envelope(await register(client, "outer", parent), 409, "CONFLICT")


@pytest.mark.parametrize("path", ["relative/dir", "", "  "])
async def test_invalid_paths_are_rejected(env, path):
    client, *_ = env
    assert_envelope(await client.post("/projects", json={"name": "x", "workspace_path": path}), 422)


async def test_missing_path_and_file_are_rejected(env, tmp_path):
    client, _h, projects, *_ = env
    assert_envelope(await register(client, "missing", projects / "nope"), 422)
    a_file = projects / "file.txt"
    a_file.write_text("x")
    assert_envelope(await register(client, "file", a_file), 422)


async def test_protected_and_system_locations_are_rejected(env, tmp_path):
    client, h, projects, *_ = env
    assert_envelope(await register(client, "root", Path(projects.anchor)), 422)
    assert_envelope(await register(client, "workspaces", h.guard.root.parent), 422)     # above WORKSPACE_ROOT
    (h.guard.root).mkdir(parents=True, exist_ok=True)
    assert_envelope(await register(client, "workspaces", h.guard.root), 422)
    assert_envelope(await register(client, "home", Path.home()), 422)
    system = os.environ.get("WINDIR") or os.environ.get("SYSTEMROOT")
    if system:
        assert_envelope(await register(client, "system", Path(system)), 422)


async def test_exactly_one_of_path_or_url_is_required(env):
    client, _h, projects, *_ = env
    assert_envelope(await client.post("/projects", json={"name": "x"}), 422)
    both = {"name": "x", "workspace_path": str(projects), "repository_url": "https://example.com/a.git"}
    assert_envelope(await client.post("/projects", json=both), 422)


async def test_symlink_or_junction_in_the_path_is_rejected(env, tmp_path):
    client, _h, projects, *_ = env
    real = make_dir(projects, "real")
    link = projects / "link"
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True, capture_output=True)
        else:
            os.symlink(real, link, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("cannot create a symlink/junction here")
    assert_envelope(await register(client, "linked", link), 409, "FAIL_CLOSED")
    make_dir(real, "x")
    assert_envelope(await register(client, "below-link", link / "x"), 409, "FAIL_CLOSED")   # a link anywhere in the chain


# --- git clone ---------------------------------------------------------------------------------------------------
async def test_clone_registers_a_project_inside_the_clone_root(env):
    client, _h, _projects, clone_exec, clones = env
    resp = await client.post("/projects", json={"name": "Cloned", "repository_url": "https://example.com/org/a.git"})
    assert resp.status_code == 201
    body = resp.json()
    path = Path(body["workspace_path"])
    assert path.parent == clones.resolve() and (path / "README.md").exists()
    assert body["repository_url"] == "https://example.com/org/a.git"
    assert clone_exec.calls[0].argv[:2] == ["git", "-c"] and clone_exec.calls[0].extra_env["GIT_TERMINAL_PROMPT"] == "0"


async def test_failed_clone_removes_only_its_own_directory_and_leaks_no_credentials(env, db):
    client, _h, _projects, clone_exec, clones = env
    clones.mkdir(exist_ok=True)
    keep = make_dir(clones, "pre-existing")                          # not created by this request
    clone_exec.mode = "fail"
    resp = await client.post("/projects", json={"name": "Bad", "repository_url": "https://example.com/org/a.git"})
    error = assert_envelope(resp, 409, "CLONE_FAILED")
    assert "s3cretT0ken" not in resp.text and "***" in error["details"]["detail"]
    assert [p.name for p in clones.iterdir()] == ["pre-existing"] and keep.exists()
    async with db.transaction() as s:
        assert (await s.execute(select(Project))).scalars().all() == []


async def test_unconfirmed_clone_termination_keeps_the_directory_unregistered(env, db):
    client, _h, _projects, clone_exec, clones = env
    clone_exec.mode = "unconfirmed"
    assert_envelope(await client.post("/projects", json={"name": "Hung", "repository_url": "ssh://git@example.com/a.git"}),
                    409, "CLONE_TERMINATION_UNCONFIRMED")
    assert len(list(clones.iterdir())) == 1                          # left in place: never delete under a live process
    async with db.transaction() as s:
        assert (await s.execute(select(Project))).scalars().all() == []


@pytest.mark.parametrize("url", [
    "https://user:s3cretT0ken@example.com/a.git", "https://s3cretT0ken@example.com/a.git",
    "ssh://bob@example.com/a.git", "file:///etc/passwd", "ftp://example.com/a.git", "-oProxyCommand=evil",
    "https://example.com/a.git?token=s3cretT0ken", "https://exa mple.com/a.git"])
async def test_unsafe_repository_urls_are_rejected_without_echoing_secrets(env, url):
    client, _h, _projects, clone_exec, clones = env
    resp = await client.post("/projects", json={"name": "x", "repository_url": url})
    assert_envelope(resp, 422)
    assert "s3cretT0ken" not in resp.text
    assert clone_exec.calls == [] and (not clones.exists() or list(clones.iterdir()) == [])


# --- tasks of a project ------------------------------------------------------------------------------------------
async def test_project_tasks_share_the_project_workspace(env):
    client, h, projects, *_ = env
    svc = make_dir(projects, "service-a")
    project = (await register(client, "A", svc)).json()
    first = await client.post(f"/projects/{project['id']}/tasks", json={"requirements": "add a feature to A"})
    second = await client.post(f"/projects/{project['id']}/tasks", json={"requirements": "fix a bug in A"})
    assert first.status_code == second.status_code == 201
    assert first.json()["project_id"] == project["id"] and first.json()["status"] == "PENDING"
    assert (await h.task(first.json()["id"])).workspace_path == (await h.task(second.json()["id"])).workspace_path \
        == str(svc.resolve())
    listed = (await client.get(f"/projects/{project['id']}/tasks")).json()
    assert {t["id"] for t in listed} == {first.json()["id"], second.json()["id"]}
    other = (await register(client, "B", make_dir(projects, "service-b"))).json()
    assert (await client.get(f"/projects/{other['id']}/tasks")).json() == []
    assert_envelope(await client.get("/projects/00000000-0000-0000-0000-000000000000/tasks"), 404)
    assert_envelope(await client.post("/projects/00000000-0000-0000-0000-000000000000/tasks",
                                      json={"requirements": "anything at all"}), 404)


async def test_task_test_policy_precedence_is_task_then_project_then_default(env):
    client, h, projects, *_ = env
    policy = {"commands": [["python", "-m", "pytest", "-q", "--junitxml=reports/junit.xml", "tests"]]}
    project = (await client.post("/projects", json={"name": "P", "workspace_path": str(make_dir(projects, "p")),
                                                    "test_policy": policy})).json()
    inherited = (await client.post(f"/projects/{project['id']}/tasks", json={"requirements": "inherit policy"})).json()
    own = {"commands": [["python", "-m", "pytest", "-q", "--junitxml=reports/junit.xml"]], "min_tests_executed": 2}
    explicit = (await client.post(f"/projects/{project['id']}/tasks",
                                  json={"requirements": "explicit policy", "test_policy": own})).json()
    assert (await h.task(inherited["id"])).test_policy["commands"] == policy["commands"]
    assert (await h.task(explicit["id"])).test_policy["min_tests_executed"] == 2
    plain = (await client.post("/tasks", json={"requirements": "legacy task"})).json()
    assert plain["project_id"] is None
    assert (await h.task(plain["id"])).workspace_path.startswith(str(h.guard.root))     # legacy policy unchanged


async def test_archive_rules(env):
    client, h, projects, *_ = env
    project = (await register(client, "A", make_dir(projects, "a"))).json()
    task = (await client.post(f"/projects/{project['id']}/tasks", json={"requirements": "do some work"})).json()
    assert_envelope(await client.patch(f"/projects/{project['id']}", json={"status": "ARCHIVED"}), 409, "CONFLICT")
    assert_envelope(await client.patch(f"/projects/{project['id']}", json={"status": "bogus"}), 422)
    assert await h.start(task["id"]) == ["COMPLETED"]
    archived = await client.patch(f"/projects/{project['id']}", json={"status": "ARCHIVED"})
    assert archived.status_code == 200 and archived.json()["status"] == "ARCHIVED"
    assert_envelope(await client.post(f"/projects/{project['id']}/tasks", json={"requirements": "more work"}),
                    409, "CONFLICT")
    assert (await client.patch(f"/projects/{project['id']}", json={"status": "ACTIVE"})).json()["status"] == "ACTIVE"


async def test_task_creation_fails_closed_when_the_workspace_vanished(env):
    client, _h, projects, *_ = env
    svc = make_dir(projects, "gone")
    project = (await register(client, "G", svc)).json()
    svc.rmdir()
    assert_envelope(await client.post(f"/projects/{project['id']}/tasks", json={"requirements": "do some work"}),
                    409, "FAIL_CLOSED")


# --- the existing workflow runs in the project workspace ---------------------------------------------------------
async def test_workflow_runs_in_the_registered_workspace_and_leaves_workspaces_root_untouched(env):
    client, h, projects, *_ = env
    svc = make_dir(projects, "service-a")
    (svc / "existing.txt").write_text("user file", encoding="utf-8")
    project = (await register(client, "A", svc)).json()
    task = (await client.post(f"/projects/{project['id']}/tasks", json={"requirements": "implement add(a, b)"})).json()
    assert await h.start(task["id"]) == ["COMPLETED"]
    assert (svc / "calc.py").read_text(encoding="utf-8") == GOOD_CODE
    assert (svc / "existing.txt").read_text(encoding="utf-8") == "user file"          # user files are never touched
    assert {c.cwd for c in h.claude.calls + h.codex.calls} == {svc.resolve()}
    assert not h.guard.root.exists() or list(h.guard.root.iterdir()) == []             # no per-task workspace


async def test_two_sequential_tasks_reuse_the_same_workspace(env):
    client, h, projects, *_ = env
    svc = make_dir(projects, "service-a")
    project = (await register(client, "A", svc)).json()
    ids = [(await client.post(f"/projects/{project['id']}/tasks", json={"requirements": f"change number {i}"})).json()["id"]
           for i in range(2)]
    for task_id in ids:
        assert await h.start(task_id) == ["COMPLETED"]
    assert (await client.get(f"/projects/{project['id']}/tasks", params={"status": "COMPLETED"})).json().__len__() == 2


async def test_executor_refuses_a_cwd_outside_both_the_default_root_and_the_given_root(env, tmp_path):
    _client, h, projects, *_ = env
    from app.infra.command_executor import CommandSpec
    from app.validation.exceptions import FailClosed
    executor = CommandExecutor(h.guard)
    inside = make_dir(projects, "inside")
    outside = make_dir(projects, "outside")
    with pytest.raises(FailClosed):
        await executor.run(CommandSpec(argv=[sys.executable, "-c", "pass"], cwd=outside))            # no trusted root given
    with pytest.raises(FailClosed):
        await executor.run(CommandSpec(argv=[sys.executable, "-c", "pass"], cwd=outside, workspace_root=inside))
    ok = await executor.run(CommandSpec(argv=[sys.executable, "-c", "pass"], cwd=inside, workspace_root=inside))
    assert ok.outcome is CliOutcome.SUCCESS


# --- test policy presets on project tasks ------------------------------------------------------------------------
async def test_a_preset_project_resolves_its_wrapper_when_a_task_is_created(env):
    from app.services.test_policy_service import hash_test_policy
    client, h, projects, *_ = env
    java = make_dir(projects, "java-app")
    wrapper = java / ("mvnw.cmd" if os.name == "nt" else "mvnw")
    wrapper.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n", encoding="utf-8")
    if os.name != "nt":
        wrapper.chmod(0o755)
    created = await client.post("/projects", json={"name": "Java", "workspace_path": str(java),
                                                   "test_policy": {"preset": "maven"}})
    assert created.status_code == 201
    assert created.json()["test_policy"]["preset"] == "maven" and "commands" not in created.json()["test_policy"]
    task = (await client.post(f"/projects/{created.json()['id']}/tasks", json={"requirements": "add a service"})).json()
    row = await h.task(task["id"])
    assert row.test_policy["commands"] == [[str(wrapper.resolve()), "-B", "-q", "test"]]       # exactly one command
    assert row.test_policy["junit_path"] == "**/target/surefire-reports/TEST-*.xml"
    assert row.test_policy["env_allow"] == ["JAVA_HOME", "MAVEN_HOME", "M2_HOME"]
    assert row.test_policy_hash == hash_test_policy(row.test_policy)                          # hash of what will run


async def test_a_preset_without_a_toolchain_is_rejected_before_any_task_exists(env, monkeypatch):
    from app.services import test_policy_resolver
    client, h, projects, *_ = env
    monkeypatch.setattr(test_policy_resolver.shutil, "which", lambda _name: None)
    project = (await client.post("/projects", json={"name": "NoJdk", "workspace_path": str(make_dir(projects, "nojdk")),
                                                    "test_policy": {"preset": "gradle"}})).json()
    error = assert_envelope(await client.post(f"/projects/{project['id']}/tasks",
                                              json={"requirements": "needs gradle"}), 422, "VALIDATION_FAILED")
    assert error["details"]["tool"] == "gradle"
    assert (await client.get(f"/projects/{project['id']}/tasks")).json() == []


async def test_invalid_project_policies_are_rejected_at_registration(env):
    client, _h, projects, *_ = env
    for bad in ({"preset": "ant"}, {"commands": [["mvnw.cmd", "test"]]}, {"preset": "maven", "commands": [["x"]]}):
        assert_envelope(await client.post("/projects", json={"name": "x", "workspace_path": str(make_dir(
            projects, f"bad-{abs(hash(str(bad)))}")), "test_policy": bad}), 422)


# --- PATCH test_policy: omitted keeps, object replaces, null removes ---------------------------------------------------
async def patch_env(env):
    client, h, projects, *_ = env
    policy = {"commands": [["python", "-m", "pytest", "-q", "--junitxml=reports/junit.xml", "tests"]]}
    project = (await client.post("/projects", json={"name": "P", "workspace_path": str(make_dir(projects, "pp")),
                                                    "test_policy": policy})).json()
    return client, h, project, policy


async def test_patch_without_test_policy_keeps_it_and_null_removes_it(env):
    client, h, project, policy = await patch_env(env)
    url = f"/projects/{project['id']}"
    kept = await client.patch(url, json={"name": "Renamed", "status": "ACTIVE"})
    assert kept.json()["test_policy"]["commands"] == policy["commands"]          # omitted: unchanged

    task = (await client.post(f"{url}/tasks", json={"requirements": "before removal"})).json()
    removed = await client.patch(url, json={"test_policy": None})
    assert removed.status_code == 200 and removed.json()["test_policy"] is None
    async with h.db.transaction() as s:                                          # SQL NULL, not a JSON 'null'
        stored = (await s.execute(select(Project.test_policy).where(Project.id == project["id"]))).scalar_one()
        is_sql_null = (await s.execute(text("select test_policy is null from projects where id = :i"),
                                       {"i": project["id"]})).scalar_one()
    assert stored is None and is_sql_null is True
    assert (await h.task(task["id"])).test_policy["commands"] == policy["commands"]    # existing task keeps its copy
    after = (await client.post(f"{url}/tasks", json={"requirements": "after removal"})).json()
    assert (await h.task(after["id"])).test_policy["commands"] != policy["commands"]   # new tasks: the default policy


async def test_patch_replaces_validates_and_protects_the_stored_policy(env):
    client, _h, project, policy = await patch_env(env)
    url = f"/projects/{project['id']}"
    new = {"commands": [["python", "-m", "pytest", "-q", "--junitxml=reports/junit.xml"]], "min_tests_executed": 3}
    assert (await client.patch(url, json={"test_policy": new})).json()["test_policy"]["min_tests_executed"] == 3
    assert_envelope(await client.patch(url, json={"test_policy": {"preset": "ant"}}), 422)
    assert (await client.get(url)).json()["test_policy"]["min_tests_executed"] == 3     # the rejected PATCH changed nothing


async def test_patch_rejects_null_name_and_status(env):
    client, _h, project, policy = await patch_env(env)
    url = f"/projects/{project['id']}"
    assert_envelope(await client.patch(url, json={"name": None}), 422)
    assert_envelope(await client.patch(url, json={"status": None}), 422)
    body = (await client.get(url)).json()
    assert body["name"] == "P" and body["status"] == "ACTIVE" and body["test_policy"]["commands"] == policy["commands"]
