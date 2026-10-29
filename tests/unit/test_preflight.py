import json
import shutil

import pytest

from app.infra.command_executor import CommandExecutor
from app.infra.workspace_guard import WorkspaceGuard
from app.providers.preflight import PreflightReport, check_installed_cli, scan_claude_settings, scan_codex_config
from app.validation.exceptions import FailClosed


def test_bypass_permissions_is_a_blocking_finding(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"permissions": {"defaultMode": "bypassPermissions", "allow": ["Bash"]}}))
    findings = scan_claude_settings(f)
    assert any(x["severity"] == "high" for x in findings)
    report = PreflightReport(settings_findings=findings)
    with pytest.raises(FailClosed):
        report.require_ok()


def test_specific_allow_rules_are_not_flagged(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"permissions": {"allow": ["Bash(git status:*)", "Read(//tmp/**)"]}}))
    assert scan_claude_settings(f) == []


def test_missing_settings_file_is_not_a_finding(tmp_path):
    assert scan_claude_settings(tmp_path / "nope.json") == []
    assert scan_codex_config(tmp_path / "nope.toml") == []


def test_codex_dangerous_combo_is_blocking(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('sandbox_mode = "danger-full-access"\napproval_policy = "never"\n')
    assert any(x["severity"] == "high" for x in scan_codex_config(f))


def test_failed_probe_blocks():
    with pytest.raises(FailClosed):
        PreflightReport(probes={"PLANNER": False}).require_ok()


@pytest.mark.skipif(not (shutil.which("claude") and shutil.which("codex")), reason="CLIs not installed")
async def test_installed_cli_supports_every_flag_we_use(tmp_path):
    """Uses the real installed CLIs' --help only (no model call, no cost)."""
    executor = CommandExecutor(WorkspaceGuard(tmp_path))
    report = await check_installed_cli(executor, tmp_path)
    assert report.versions["claude"] and report.versions["codex"]
    assert report.missing_flags == {"claude": [], "codex": []}, report.missing_flags
