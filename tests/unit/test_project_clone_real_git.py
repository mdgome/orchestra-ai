"""The real `git clone` command line, executor and environment used by ProjectService (no network: a local repo)."""
import os
import shutil
import subprocess

import pytest

from app.infra.command_executor import CommandExecutor
from app.infra.workspace_guard import WorkspaceGuard
from app.infra.redaction import redact_credentials
from app.services.project_service import CloneFailed, ProjectService

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def local_repo(path):
    path.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        if args[0] == "add":
            (path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@x.io",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, env=env)
    return path


async def test_real_clone_runs_through_the_executor_inside_the_clone_root(tmp_path):
    source = local_repo(tmp_path / "source")
    clones = tmp_path / "clones"
    clones.mkdir()
    service = ProjectService(None, CommandExecutor(WorkspaceGuard(tmp_path / "ws")), clone_root=clones)
    target = clones / "service-1234"
    target.mkdir()
    await service._run_clone(str(source), target)           # same argv, env, cwd and trusted root as production
    assert (target / "hello.py").read_text(encoding="utf-8") == "print('hi')\n" and (target / ".git").is_dir()


async def test_a_failing_real_clone_reports_a_sanitised_error(tmp_path):
    clones = tmp_path / "clones"
    clones.mkdir()
    service = ProjectService(None, CommandExecutor(WorkspaceGuard(tmp_path / "ws")), clone_root=clones)
    target = clones / "missing-1234"
    target.mkdir()
    with pytest.raises(CloneFailed) as err:
        await service._run_clone(str(tmp_path / "does-not-exist"), target)
    assert err.value.code == "CLONE_FAILED" and err.value.details["outcome"] == "ERROR"


def test_credentials_in_urls_and_tokens_are_masked():
    text = "fatal: unable to access 'https://user:s3cretT0ken@host/x.git/' with ghp_abcdefghijklmnop1234567890"
    masked = redact_credentials(text)
    assert "s3cretT0ken" not in masked and "ghp_abcdefghijklmnop1234567890" not in masked and "***" in masked
    assert redact_credentials(None) == "" and redact_credentials("nothing secret here") == "nothing secret here"
