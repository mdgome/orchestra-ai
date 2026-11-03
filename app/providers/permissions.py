"""Role -> CLI-native permission flags.

These are CLI *tool policies*, NOT an OS sandbox: they only make sense in a trusted local environment.
Claude: --tools (what exists) != --allowedTools (what runs without a prompt) != --disallowedTools.
Codex:  -s (sandbox mode) != approval policy.
"""
import re
from dataclasses import dataclass, replace

from app.validation import policies as P
from app.validation.exceptions import FailClosed


@dataclass(frozen=True)
class RolePermissions:
    provider: str
    # Claude
    permission_mode: str | None = None
    tools: tuple[str, ...] = ()
    allowed: tuple[str, ...] = ()
    disallowed: tuple[str, ...] = ()
    # Codex
    sandbox: str | None = None
    approval_policy: str | None = None
    writes_files: bool = False


_DEV_BASH = (
    "Bash(python:*)", "Bash(python -m pytest:*)", "Bash(git status:*)", "Bash(git diff:*)",
    "Bash(git log:*)", "Bash(ls:*)",
)
_DENY_ALWAYS = ("Bash(rm:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(git push:*)", "Bash(git reset:*)",
                "WebFetch", "WebSearch")

ROLE_PERMISSIONS: dict[str, RolePermissions] = {
    # Plan Mode: read/explore only. No Edit/Write/Bash exist as tools at all.
    "PLANNER": RolePermissions(
        "CLAUDE", permission_mode="plan", tools=("Read", "Glob", "Grep"), allowed=("Read", "Glob", "Grep"),
        disallowed=("Edit", "Write", "Bash", *_DENY_ALWAYS)),
    "DEVELOPER": RolePermissions(
        "CLAUDE", permission_mode="acceptEdits", tools=("Read", "Glob", "Grep", "Edit", "Write", "Bash"),
        allowed=("Read", "Glob", "Grep", "Edit", "Write", *_DEV_BASH), disallowed=_DENY_ALWAYS, writes_files=True),
    # Tester: may edit tests and run a restricted set of commands. The orchestrator additionally verifies
    # (snapshot diff) that only test paths changed, because CLI path rules are not an OS sandbox.
    "TESTER": RolePermissions(
        "CLAUDE", permission_mode="acceptEdits", tools=("Read", "Glob", "Grep", "Edit", "Write", "Bash"),
        allowed=("Read", "Glob", "Grep", "Edit(tests/**)", "Write(tests/**)", "Bash(python -m pytest:*)",
                 "Bash(git status:*)", "Bash(git diff:*)", "Bash(ls:*)"),
        disallowed=_DENY_ALWAYS, writes_files=True),
    "DESIGN_REVIEWER": RolePermissions("CODEX", sandbox="read-only", approval_policy="never"),
    "CODE_REVIEWER": RolePermissions("CODEX", sandbox="read-only", approval_policy="never"),
}

TESTER_WRITABLE_PREFIXES = ("tests/", "test_", "conftest.py")


def permissions_for(role: str, test_preset: str | None = None) -> RolePermissions:
    """Role permissions, specialised for the task's test preset (pytest is the baseline and stays as written).

    A Maven/Gradle project lets the Developer run its build tool and the Tester edit src/test/** and run only the
    test goal of that tool. Wrapper scripts (mvnw, gradlew ...) are never writable by the Tester."""
    try:
        base = ROLE_PERMISSIONS[role]
    except KeyError:
        raise FailClosed("no permission policy for role", {"role": role}) from None
    preset = P.TEST_PRESETS.get(test_preset or "")
    if preset is None or test_preset == "pytest" or base.provider != "CLAUDE":
        return base
    if role == "DEVELOPER":
        return replace(base, allowed=(*base.allowed, *preset["developer_bash"]))
    if role == "TESTER":
        edits = tuple(f"{tool}({glob})" for glob in preset["tester_edit"] for tool in ("Edit", "Write"))
        keep = ("Read", "Glob", "Grep", "Bash(git status:*)", "Bash(git diff:*)", "Bash(ls:*)")
        return replace(base, allowed=(*keep, *edits, *preset["tester_bash"]))
    return base


def _tool_name(pattern: str) -> str:
    return pattern.split("(", 1)[0]


def authorize_one_off(role: str, tool_pattern: str) -> str:
    """A PERMISSION approval can only grant something the role may *ever* use (its --tools set), and
    never a pattern the role explicitly disallows. It can never widen the role's maximum."""
    perms = permissions_for(role)
    if perms.provider != "CLAUDE":
        raise FailClosed("one-off tool grants are not enforceable for this provider", {"role": role})
    if _tool_name(tool_pattern) not in perms.tools:
        raise FailClosed("tool is outside the role's maximum permissions",
                         {"role": role, "tool": _tool_name(tool_pattern)})
    if tool_pattern in perms.disallowed:
        raise FailClosed("tool is explicitly disallowed for this role", {"role": role})
    return tool_pattern


def claude_permission_args(role: str, one_off_tool: str | None = None, test_preset: str | None = None) -> list[str]:
    perms = permissions_for(role, test_preset)
    allowed = list(perms.allowed)
    if one_off_tool:
        allowed.append(authorize_one_off(role, one_off_tool))
    args = ["--permission-mode", perms.permission_mode, "--tools", ",".join(perms.tools),
            "--allowedTools", ",".join(allowed)]
    if perms.disallowed:
        args += ["--disallowedTools", ",".join(perms.disallowed)]
    return args


def codex_permission_args(role: str) -> list[str]:
    perms = permissions_for(role)
    return ["-s", perms.sandbox, "-c", f'approval_policy="{perms.approval_policy}"']


_JVM_TEST_NAME = re.compile(r"^(Test.+|.+Tests?|.+IT)\.(java|kt|groovy|scala)$")


def is_test_path(rel_path: str, test_preset: str | None = None) -> bool:
    """What the Tester may change. Build files and wrappers (pom.xml, mvnw, gradlew, build.gradle ...) never count."""
    rel = rel_path.replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]
    if test_preset in ("maven", "gradle"):
        return rel.startswith("src/test/") or "/src/test/" in rel or bool(_JVM_TEST_NAME.match(name))
    return rel.startswith("tests/") or name.startswith("test_") or name == "conftest.py"
