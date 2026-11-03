"""Orchestrator-owned test execution. Success is decided by exit codes and parsed JUnit results, never by an
agent's prose. The Tester agent cannot change the policy (its hash is part of every Tester input).

Hard rules (never PASS): non-zero exit, nothing executed, everything skipped, missing/unparseable JUnit.
Changing test code is NOT a failure by itself; weakening signals go to the Code Reviewer.
"""
import asyncio
import hashlib
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from app.infra.command_executor import CommandExecutor, CommandSpec
from app.models.states import CliOutcome
from app.services.hashing import stable_hash
from app.validation import policies as P


@dataclass
class TestRunResult:
    passed: bool
    reason: str
    executed: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    exit_codes: list[int | None] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    output_tail: str = ""
    junit_ok: bool = False

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reason": self.reason, "executed": self.executed, "skipped": self.skipped,
                "failures": self.failures[:50], "exit_codes": self.exit_codes, "outcomes": self.outcomes,
                "output_tail": self.output_tail[-2000:], "junit_ok": self.junit_ok}


def hash_test_policy(policy: dict) -> str:
    return stable_hash("test_policy", policy)


def result_snapshot(cwd: Path, pattern: str) -> dict[str, tuple[int, int, str]]:
    """relative path -> (size, mtime_ns, sha256) of every regular result file matching `pattern` in the workspace.
    Links and anything resolving outside the workspace are ignored. Nothing is ever deleted or modified."""
    root = Path(cwd).resolve()
    found: dict[str, tuple[int, int, str]] = {}
    for path in root.glob(pattern):
        if len(found) >= P.JUNIT_GLOB_MAX_FILES:
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if root not in resolved.parents:
                continue
            stat = resolved.stat()
            found[resolved.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns,
                                                            hashlib.sha256(resolved.read_bytes()).hexdigest())
        except OSError:
            continue
    return found


def fresh_results(before: dict, after: dict) -> list[str]:
    """Only files that are new, or whose size / mtime / content changed, belong to THIS run."""
    return sorted(rel for rel, sig in after.items() if before.get(rel) != sig)


def parse_junit_files(paths: list[Path]) -> tuple[int, int, int, list[str]] | None:
    """Sums several JUnit files; None when there are none or any of them is unusable."""
    if not paths:
        return None
    total = skipped = bad = 0
    failures: list[str] = []
    for path in paths:
        parsed = parse_junit(path)
        if parsed is None:
            return None
        total, skipped, bad = total + parsed[0], skipped + parsed[1], bad + parsed[2]
        failures += parsed[3]
    return total, skipped, bad, failures


def parse_junit(path: Path) -> tuple[int, int, int, list[str]] | None:
    """Returns (tests, skipped, failed_or_errored, failure_ids) or None when unusable."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")    # top-level only: no double counting
    if not suites:
        return None
    tests = skipped = bad = 0
    failures: list[str] = []
    for suite in suites:
        for case in suite.iter("testcase"):
            tests += 1
            if case.find("skipped") is not None:
                skipped += 1
            elif case.find("failure") is not None or case.find("error") is not None:
                bad += 1
                failures.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return tests, skipped, bad, failures


def _argv(command: list[str]) -> list[str]:
    return [sys.executable if command[0] == "python" else command[0], *command[1:]]


class TestPolicyService:
    __test__ = False  # not a pytest class

    def __init__(self, executor: CommandExecutor):
        self._executor = executor

    async def collect_ids(self, cwd: Path, policy: dict, workspace_root: Path | None = None) -> set[str] | None:
        """pytest --collect-only node ids; None when collection itself fails."""
        base = _argv(policy["commands"][0])
        if base[1:3] != ["-m", "pytest"]:
            return None
        res = await self._executor.run(CommandSpec(
            argv=[base[0], "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"], cwd=cwd,
            timeout_seconds=policy["timeout_seconds"], idle_seconds=None, workspace_root=workspace_root,
            drop_cli_auth=True))
        if res.outcome is not CliOutcome.SUCCESS:
            return None
        return {line.strip() for line in res.stdout.splitlines() if "::" in line}

    async def run(self, cwd: Path, policy: dict, workspace_root: Path | None = None) -> TestRunResult:
        result = TestRunResult(passed=False, reason="not run")
        pattern = policy["junit_path"]
        # Result files are NEVER deleted (target/, build/, reports/ belong to the user): note what exists now and
        # accept only files that are new or changed once the commands have run.
        before = await asyncio.to_thread(result_snapshot, cwd, pattern)
        env = {name: os.environ[name] for name in policy.get("env_allow", []) if name in os.environ}   # exact names
        tails = []
        for command in policy["commands"]:
            run = await self._executor.run(CommandSpec(argv=_argv(command), cwd=cwd,
                                                       timeout_seconds=policy["timeout_seconds"], idle_seconds=None,
                                                       extra_env=env, workspace_root=workspace_root,
                                                       drop_cli_auth=True))
            result.exit_codes.append(run.exit_code)
            result.outcomes.append(str(run.outcome))
            tails.append((run.stdout + run.stderr)[-1500:])
            if run.outcome is not CliOutcome.SUCCESS:
                result.reason = f"test command did not succeed ({run.outcome}, exit {run.exit_code})"
                result.output_tail = "\n".join(tails)
                after = await asyncio.to_thread(result_snapshot, cwd, pattern)
                self._fill_from_junit(result, [Path(cwd) / rel for rel in fresh_results(before, after)])
                return result
        result.output_tail = "\n".join(tails)
        after = await asyncio.to_thread(result_snapshot, cwd, pattern)
        fresh = fresh_results(before, after)
        if not fresh:
            result.reason = "no new or updated JUnit result was produced by this run"
            return result
        parsed = self._fill_from_junit(result, [Path(cwd) / rel for rel in fresh])
        if not parsed:
            result.reason = "JUnit result missing or unparseable"
            return result
        executed = result.executed - result.skipped
        if result.failures:
            result.reason = f"{len(result.failures)} test(s) failed"
        elif executed < int(policy["min_tests_executed"]):
            result.reason = (f"only {executed} test(s) actually executed "
                             f"({result.skipped} skipped), required {policy['min_tests_executed']}")
        else:
            result.passed = True
            result.reason = "all required tests passed"
        return result

    @staticmethod
    def _fill_from_junit(result: TestRunResult, files: list[Path]) -> bool:
        parsed = parse_junit_files(files)
        if parsed is None:
            return False
        result.junit_ok = True
        result.executed, result.skipped, _, result.failures = parsed
        return True


def compute_test_signals(baseline_ids: set[str] | None, current_ids: set[str] | None, result: TestRunResult,
                 changed_test_files: list[str]) -> dict:
    """Facts for the Code Reviewer about possible weakening of verification (judgement is the reviewer's)."""
    removed = sorted((baseline_ids or set()) - (current_ids or set())) if current_ids is not None else None
    return {"removed_test_ids": removed, "skipped": result.skipped, "executed": result.executed,
            "changed_test_files": changed_test_files, "collection_ok": current_ids is not None}
