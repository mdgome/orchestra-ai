"""Preflight: verify the *installed* CLI versions actually support what we rely on, detect user/project
settings that widen a role's permissions, and probe that read-only roles really cannot write."""
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from app.infra.command_executor import CommandExecutor, CommandSpec
from app.models.states import CliOutcome
from app.providers.base import ProviderRequest
from app.validation.exceptions import FailClosed

CLAUDE_REQUIRED_FLAGS = ("--tools", "--allowedTools", "--disallowedTools", "--permission-mode", "--resume",
                         "--json-schema", "--output-format")
CODEX_EXEC_REQUIRED_FLAGS = ("--json", "--output-schema", "--sandbox", "--skip-git-repo-check")
HIGH_RISK_CLAUDE_MODES = {"bypassPermissions"}
BROAD_ALLOW = {"Bash", "Edit", "Write", "Bash(*)", "Bash(*:*)", "*"}


@dataclass
class PreflightReport:
    versions: dict[str, str] = field(default_factory=dict)
    missing_flags: dict[str, list[str]] = field(default_factory=dict)
    settings_findings: list[dict] = field(default_factory=list)   # {"source", "severity", "issue"}
    probes: dict[str, bool] = field(default_factory=dict)         # role -> write was blocked

    @property
    def blocking(self) -> list[str]:
        out = [f"{p}: missing {', '.join(m)}" for p, m in self.missing_flags.items() if m]
        out += [f"{f['source']}: {f['issue']}" for f in self.settings_findings if f["severity"] == "high"]
        out += [f"probe failed for {role}" for role, passed in self.probes.items() if not passed]
        return out

    def require_ok(self) -> None:
        if self.blocking:
            raise FailClosed("preflight failed", {"problems": self.blocking})


async def check_installed_cli(executor: CommandExecutor, cwd: Path, claude_bin="claude", codex_bin="codex"
                              ) -> PreflightReport:
    report = PreflightReport()
    for name, argv, required in (
        ("claude", [claude_bin, "--help"], CLAUDE_REQUIRED_FLAGS),
        ("codex", [codex_bin, "exec", "--help"], CODEX_EXEC_REQUIRED_FLAGS),
    ):
        help_run = await executor.run(CommandSpec(argv=argv, cwd=cwd, timeout_seconds=60, idle_seconds=None))
        text = help_run.stdout + help_run.stderr
        report.missing_flags[name] = [f for f in required if f not in text] if help_run.exit_code == 0 else ["--help"]
        ver = await executor.run(CommandSpec(argv=[argv[0], "--version"], cwd=cwd, timeout_seconds=30, idle_seconds=None))
        report.versions[name] = ver.stdout.strip()
    # `codex exec resume` must exist
    resume = await executor.run(CommandSpec(argv=[codex_bin, "exec", "resume", "--help"], cwd=cwd,
                                            timeout_seconds=60, idle_seconds=None))
    if resume.exit_code != 0:
        report.missing_flags.setdefault("codex", []).append("exec resume")
    return report


def scan_claude_settings(path: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return findings
    except (OSError, json.JSONDecodeError):
        return [{"source": str(path), "severity": "medium", "issue": "unreadable settings file"}]
    perms = data.get("permissions", {}) if isinstance(data, dict) else {}
    mode = perms.get("defaultMode") or data.get("defaultMode")
    if mode in HIGH_RISK_CLAUDE_MODES:
        findings.append({"source": str(path), "severity": "high", "issue": f"defaultMode={mode}"})
    if data.get("skipDangerousModePermissionPrompt"):
        findings.append({"source": str(path), "severity": "high", "issue": "skipDangerousModePermissionPrompt"})
    for rule in perms.get("allow", []) or []:
        if rule in BROAD_ALLOW:
            findings.append({"source": str(path), "severity": "medium", "issue": f"broad allow rule {rule!r}"})
    return findings


def scan_codex_config(path: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return findings
    except (OSError, tomllib.TOMLDecodeError):
        return [{"source": str(path), "severity": "medium", "issue": "unreadable config.toml"}]
    if data.get("sandbox_mode") == "danger-full-access":
        findings.append({"source": str(path), "severity": "medium",
                         "issue": "sandbox_mode=danger-full-access (overridden per call by -s)"})
    if data.get("approval_policy") == "never" and data.get("sandbox_mode") == "danger-full-access":
        findings.append({"source": str(path), "severity": "high", "issue": "approval never + danger-full-access"})
    return findings


def scan_all_settings(project_dir: Path, home: Path | None = None) -> list[dict]:
    home = home or Path.home()
    out: list[dict] = []
    for p in (home / ".claude" / "settings.json", home / ".claude" / "settings.local.json",
              project_dir / ".claude" / "settings.json", project_dir / ".claude" / "settings.local.json"):
        out += scan_claude_settings(p)
    out += scan_codex_config(home / ".codex" / "config.toml")
    return out


PROBE_PROMPT = ("Create a file named probe_forbidden.txt containing the text x. "
                "If you are not able to create files, reply exactly CANNOT.")


async def probe_write_blocked(provider, role: str, workdir: Path) -> bool:
    """Run the real CLI for `role` in an empty directory and verify no file was created."""
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / "probe_forbidden.txt"
    target.unlink(missing_ok=True)
    result = await provider.execute(ProviderRequest(role=role, prompt=PROBE_PROMPT, cwd=workdir, timeout_seconds=180))
    created = target.exists()
    target.unlink(missing_ok=True)
    return result.outcome in (CliOutcome.SUCCESS, CliOutcome.PERMISSION_REQUESTED) and not created
