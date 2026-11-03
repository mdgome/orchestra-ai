"""Turns a test policy (possibly a preset reference) into ONE runnable command for a given workspace.

The result is copied into the task, so its hash (part of every Tester input) describes exactly what will run. The
preset's alternatives are never kept in argv: a Maven project gets either its own `mvnw`/`mvnw.cmd` wrapper or the
system `mvn`, never "mvn or mvnw". Resolution happens when the task is created, so a project whose toolchain is
missing is rejected up front instead of failing deep inside a workflow.
"""
import os
import shutil
from pathlib import Path

from app.validation import policies as P
from app.validation import validators as V
from app.validation.exceptions import ValidationFailed


def _wrapper(workspace: Path, names: tuple[str, ...]) -> str | None:
    """The project's own wrapper for this OS, only if it is a plain, executable file directly in the workspace."""
    windows = os.name == "nt"
    for name in names:
        if name.lower().endswith(P.SHELL_SCRIPT_SUFFIXES) != windows:
            continue                                              # .cmd/.bat on Windows, extension-less elsewhere
        candidate = workspace / name
        if not candidate.exists():
            continue
        if candidate.is_symlink() or not candidate.is_file() or candidate.resolve().parent != workspace.resolve():
            raise ValidationFailed("the build wrapper must be a regular file inside the workspace",
                                   {"wrapper": name})
        if not windows and not os.access(candidate, os.X_OK):
            raise ValidationFailed("the build wrapper is not executable (chmod +x it)", {"wrapper": name})
        return str(candidate.resolve())
    return None


def resolve_executable_for(preset_name: str, workspace: Path | None) -> str:
    preset = P.TEST_PRESETS[preset_name]
    tool = preset["tool"]
    if tool == "python":
        return "python"                                           # the executor maps it to this interpreter
    if workspace is not None:
        wrapper = _wrapper(Path(workspace), preset["wrappers"])
        if wrapper:
            return wrapper
    system = shutil.which(tool)
    if not system:
        raise ValidationFailed(
            f"the {preset_name} test preset needs `{tool}` but neither a project wrapper nor a system install "
            "was found", {"preset": preset_name, "tool": tool})
    return system


def resolve_test_policy(policy: dict | None, workspace: Path | None) -> dict:
    validated = V.validate_test_policy(policy)
    if "preset" not in validated:
        return validated
    name = validated["preset"]
    preset = P.TEST_PRESETS[name]
    executable = resolve_executable_for(name, workspace)
    return {"preset": name, "commands": [[executable, *preset["args"]]], "junit_path": preset["junit_path"],
            "min_tests_executed": validated["min_tests_executed"], "timeout_seconds": validated["timeout_seconds"],
            "env_allow": list(preset["env_allow"])}
