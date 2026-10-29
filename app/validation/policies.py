"""Central, reusable validation / execution policy values.

Every limit used by validators, services and providers lives here so the
policy has exactly one management point.
"""
from dataclasses import dataclass

# --- Task input -----------------------------------------------------------
REQUIREMENTS_MIN_LENGTH = 5
REQUIREMENTS_MAX_LENGTH = 20_000
MESSAGE_MAX_LENGTH = 20_000
MESSAGE_ROLES = frozenset({"USER", "SYSTEM"})

# --- Approval -------------------------------------------------------------
APPROVAL_DECISIONS = frozenset({"APPROVED", "REJECTED"})
APPROVAL_TTL_SECONDS = 24 * 3600
IDEMPOTENCY_KEY_MAX_LENGTH = 128

# --- Messages retention (messages rows only; task history is never deleted) ---
MESSAGE_RETENTION_DAYS = 30

# --- Environment passed to CLI children (allowlist; everything else is dropped) ---
ENV_ALLOWLIST = frozenset(
    {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH",
        "LANG", "LC_ALL", "TERM",
        # CLI authentication / config locations (values are not logged)
        "CODEX_HOME", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    }
)

# --- Execution limits (fixed at task creation) ---------------------------------
@dataclass(frozen=True)
class ExecutionLimits:
    task_max_wall_seconds: int = 3600
    max_cli_invocations: int = 40
    max_workflow_iterations: int = 3   # improvement loop after the first implementation
    max_step_attempts: int = 2         # technical retries per (revision, iteration, step)
    max_replans: int = 2               # NEEDS_REVISION loops


LIMIT_BOUNDS = {
    "task_max_wall_seconds": (10, 24 * 3600),
    "max_cli_invocations": (1, 500),
    "max_workflow_iterations": (0, 3),
    "max_step_attempts": (1, 5),
    "max_replans": (0, 5),
}

DEFAULT_CLI_TIMEOUT_SECONDS = 900
MAX_CLI_TIMEOUT_SECONDS = 3600
CLI_IDLE_SECONDS = 300
LOG_TAIL_CHARS = 4000

# --- Lease -----------------------------------------------------------------------
LEASE_TTL_SECONDS = 60
PROCESS_TERMINATE_GRACE_SECONDS = 5

# --- Design review ---------------------------------------------------------------
# Bump when the design-review checklist below changes; part of design_review_basis.
DESIGN_REVIEW_CHECKLIST = (
    "Layered Architecture (Router -> Service -> Repository/Provider)",
    "No DDD, no Generic Repository, no needless abstraction",
    "Noun-based REST URIs; verbs only via HTTP method",
    "Controllers contain no business logic",
    "Central validation module, no duplicated validation",
    "SOLID where it improves maintainability and testability",
    "Security and performance",
    "Testability and Mock-ability of external CLIs",
)

# --- Workspace snapshot ------------------------------------------------------------
# Paths whose content changes on every test run; excluded from code fingerprints.
VOLATILE_PATH_PATTERNS = (
    ".pytest_cache/", "__pycache__/", ".mypy_cache/", ".ruff_cache/", "reports/",
    "htmlcov/", ".coverage", "junit.xml", "test-result.json", "plan.md",
    "design-review-result.json", "code-review-result.json", ".git/",
)

# --- Test policy -------------------------------------------------------------------
DEFAULT_TEST_POLICY = {
    "commands": [["python", "-m", "pytest", "-q", "--junitxml=reports/junit.xml"]],
    "min_tests_executed": 1,
    "junit_path": "reports/junit.xml",
    "timeout_seconds": 600,
}

# --- Reaction to CLI sessions ------------------------------------------------------
SESSION_ROLES = frozenset({"PLANNER", "DESIGN_REVIEWER", "DEVELOPER", "TESTER", "CODE_REVIEWER"})
SESSION_PROVIDERS = frozenset({"CODEX", "CLAUDE"})
ROLE_PROVIDER = {
    "PLANNER": "CLAUDE",
    "DESIGN_REVIEWER": "CODEX",
    "DEVELOPER": "CLAUDE",
    "TESTER": "CLAUDE",
    "CODE_REVIEWER": "CODEX",
}
