import asyncio
import os
import subprocess
import sys
import time

import psutil
import pytest

from app.infra.command_executor import CommandExecutor, CommandSpec, build_env
from app.infra.process_control import (
    CONFIRMED_GONE, ProcessIdentity, TerminationResult, capture_identity, check_alive, terminate_tree,
)
from app.infra.workspace_guard import WorkspaceGuard
from app.models.states import CliOutcome
from app.validation.exceptions import FailClosed

PY = sys.executable


@pytest.fixture
def guard(tmp_path):
    return WorkspaceGuard(tmp_path)


@pytest.fixture
def executor(guard):
    return CommandExecutor(guard)


def spec(tmp_path, code, **kw):
    return CommandSpec(argv=[PY, "-c", code], cwd=tmp_path, **kw)


async def test_success_captures_output_and_exit_code(executor, tmp_path):
    r = await executor.run(spec(tmp_path, "print('hi'); import sys; print('err', file=sys.stderr)"))
    assert r.outcome is CliOutcome.SUCCESS and r.exit_code == 0
    assert r.stdout.strip() == "hi" and "err" in r.stderr
    assert r.identity is not None


async def test_nonzero_exit_is_an_error_not_a_success(executor, tmp_path):
    r = await executor.run(spec(tmp_path, "import sys; sys.exit(3)"))
    assert r.outcome is CliOutcome.ERROR and r.exit_code == 3


async def test_missing_executable_fails_closed(executor, tmp_path):
    with pytest.raises(FailClosed):
        await executor.run(CommandSpec(argv=["definitely-not-a-real-binary-xyz"], cwd=tmp_path))


async def test_shell_string_is_rejected(executor, tmp_path):
    with pytest.raises(FailClosed):
        await executor.run(CommandSpec(argv=[], cwd=tmp_path))


async def test_timeout_kills_the_whole_process_tree(executor, tmp_path):
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open(r'{pidfile}', 'w').write(str(c.pid))\n"
        "time.sleep(120)\n"
    )
    r = await executor.run(spec(tmp_path, code, timeout_seconds=3))
    assert r.outcome is CliOutcome.TIMEOUT
    child_pid = int(pidfile.read_text())
    await asyncio.sleep(0.5)
    assert not psutil.pid_exists(child_pid), "grandchild must be cleaned up on timeout"
    assert r.identity is not None and check_alive(r.identity) in CONFIRMED_GONE


async def test_idle_output_detection(executor, tmp_path):
    r = await executor.run(spec(tmp_path, "import time; time.sleep(60)", idle_seconds=2, timeout_seconds=30))
    assert r.outcome is CliOutcome.IDLE


async def test_cancel_terminates_process(executor, tmp_path):
    task = asyncio.create_task(executor.run(spec(tmp_path, "import time; time.sleep(60)"), run_key="k1"))
    await asyncio.sleep(1.5)
    executor.cancel("k1")
    r = await asyncio.wait_for(task, timeout=15)
    assert r.outcome is CliOutcome.CANCELLED


async def test_on_started_receives_identity(executor, tmp_path):
    seen = []

    async def hook(identity):
        seen.append(identity)

    await executor.run(spec(tmp_path, "pass"), on_started=hook)
    assert len(seen) == 1 and seen[0].pid > 0 and seen[0].create_time > 0


def test_environment_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    env = build_env({"EXTRA": "1"})
    assert "SUPER_SECRET_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env["EXTRA"] == "1" and "PATH" in {k.upper() for k in env}


async def test_child_does_not_receive_secrets(executor, tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "leak")
    r = await executor.run(spec(tmp_path, "import os; print(os.environ.get('SUPER_SECRET_TOKEN','absent'))"))
    assert r.stdout.strip() == "absent"


async def test_cwd_outside_workspace_is_rejected(executor, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    with pytest.raises(FailClosed):
        await executor.run(CommandSpec(argv=[PY, "-c", "pass"], cwd=outside))


def test_guard_rejects_dotdot_and_absolute_escape(guard, tmp_path):
    (tmp_path / "ok").mkdir()
    assert guard.resolve_inside("ok").name == "ok"
    with pytest.raises(FailClosed):
        guard.resolve_inside("../escape")
    with pytest.raises(FailClosed):
        guard.resolve_inside(os.path.abspath(os.sep))


def test_guard_rejects_junction_or_symlink_escape(guard, tmp_path, tmp_path_factory):
    target = tmp_path_factory.mktemp("target")
    link = tmp_path / "link"
    if sys.platform == "win32":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if r.returncode != 0:
            pytest.fail(f"cannot create junction for test: {r.stderr!r}")
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FailClosed):
        guard.resolve_inside("link")
    with pytest.raises(FailClosed):
        guard.resolve_inside("link/inner")


# --- process identity -----------------------------------------------------------------
def _spawn_sleeper():
    return subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])


def test_terminate_tree_requires_matching_identity():
    p = _spawn_sleeper()
    try:
        real = capture_identity(p.pid)
        forged = ProcessIdentity(p.pid, real.create_time + 1000)  # same pid, different "process"
        assert check_alive(forged) is TerminationResult.IDENTITY_MISMATCH
        assert terminate_tree(forged, 1) is TerminationResult.IDENTITY_MISMATCH
        assert p.poll() is None, "process with a mismatching identity must NOT be killed"
        assert terminate_tree(real, 5) is TerminationResult.TERMINATED
        p.wait(timeout=5)
    finally:
        if p.poll() is None:
            p.kill()


def test_terminate_already_exited_is_confirmed_gone():
    p = subprocess.Popen([PY, "-c", "pass"])
    ident = capture_identity(p.pid)
    p.wait()
    time.sleep(0.2)
    assert ident is not None
    assert terminate_tree(ident, 1) in CONFIRMED_GONE
