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

# --- Project ----------------------------------------------------------------
PROJECT_NAME_MIN_LENGTH = 1
PROJECT_NAME_MAX_LENGTH = 100
PROJECT_STATUSES = frozenset({"ACTIVE", "ARCHIVED"})
PROJECT_REPOSITORY_URL_MAX_LENGTH = 2048
PROJECT_REPOSITORY_URL_SCHEMES = frozenset({"https", "ssh"})
PROJECT_CLONE_TIMEOUT_SECONDS = 600
# Environment variable names whose directories (and everything below them) can never be a project workspace.
PROJECT_FORBIDDEN_ENV_DIRS = ("WINDIR", "SYSTEMROOT", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA")
WORKSPACE_STATES = frozenset({"CLEAN", "DIRTY_UNRESOLVED"})

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
# The CLI providers need these; the project's own test processes must never see them.
CLI_AUTH_ENV = frozenset({"CODEX_HOME", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"})

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

# --- Project write lock / fingerprint proofs -------------------------------------------
PROOF_MAX_ATTEMPTS = 3
PROJECT_EPOCH_RETRIES = 2          # read-only step attempts invalidated by a project change, per step
RECOVERY_REASON_EXECUTION_UNCERTAIN = "EXECUTION_UNCERTAIN"
RECOVERY_REASON_PROJECT_DIRTY = "PROJECT_DIRTY"
PROJECT_DIRTY_OPTIONS = ("continue", "abort")    # no `retry`: nothing was started

# --- Runner dispatch / backoff ---------------------------------------------------------
DEFER_BASE_SECONDS = 5.0           # first defer waits BASE, then BASE*2, BASE*4 ... capped at MAX
DEFER_MAX_SECONDS = 60.0
DEFER_JITTER = (0.8, 1.2)
DEFAULT_MAX_CONCURRENT_TASKS = 1
DEFAULT_MAX_TASKS_PER_PROJECT = 1
DEFAULT_RUNNER_SHUTDOWN_GRACE_SECONDS = 30.0

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
    "design-review-result.json", "code-review-result.json", ".git/", ".gradle/", ".idea/",
)

# --- Test policy presets (owned by the Orchestrator; an agent can never change them) ----------------------------
# `tool` is a logical name resolved to ONE executable when a task is created; `args` are static (no user text).
# Only the environment variables listed here ever reach a test process, by exact name: there are no wildcards, so
# credentials such as GITHUB_TOKEN, DATABASE_URL, MAVEN_PASSWORD or *_API_KEY can never be passed along.
TEST_ENV_ALLOWED_NAMES = frozenset({"JAVA_HOME", "MAVEN_HOME", "M2_HOME", "GRADLE_HOME", "GRADLE_USER_HOME"})
TEST_PRESETS: dict[str, dict] = {
    "pytest": {
        "tool": "python", "args": ["-m", "pytest", "-q", "--junitxml=reports/junit.xml"],
        "junit_path": "reports/junit.xml", "env_allow": [], "wrappers": (),
        "tester_edit": ("tests/**",), "tester_bash": ("Bash(python -m pytest:*)",),
        "developer_bash": ("Bash(python:*)", "Bash(python -m pytest:*)"),
    },
    "maven": {
        "tool": "mvn", "args": ["-B", "-q", "test"], "junit_path": "**/target/surefire-reports/TEST-*.xml",
        "env_allow": ["JAVA_HOME", "MAVEN_HOME", "M2_HOME"], "wrappers": ("mvnw.cmd", "mvnw"),
        "tester_edit": ("src/test/**",),
        "tester_bash": ("Bash(mvn test:*)", "Bash(./mvnw test:*)", "Bash(mvnw test:*)", "Bash(mvnw.cmd test:*)"),
        "developer_bash": ("Bash(mvn:*)", "Bash(./mvnw:*)", "Bash(mvnw:*)", "Bash(mvnw.cmd:*)"),
    },
    "gradle": {
        "tool": "gradle", "args": ["test", "--console=plain"], "junit_path": "**/build/test-results/test/TEST-*.xml",
        "env_allow": ["JAVA_HOME", "GRADLE_HOME", "GRADLE_USER_HOME"], "wrappers": ("gradlew.bat", "gradlew"),
        "tester_edit": ("src/test/**",),
        "tester_bash": ("Bash(gradle test:*)", "Bash(./gradlew test:*)", "Bash(gradlew test:*)",
                        "Bash(gradlew.bat test:*)"),
        "developer_bash": ("Bash(gradle:*)", "Bash(./gradlew:*)", "Bash(gradlew:*)", "Bash(gradlew.bat:*)"),
    },
}
DEFAULT_TEST_PRESET = "pytest"
# Only a wrapper the preset itself resolved inside the workspace may be a .cmd/.bat; a custom command may not.
SHELL_SCRIPT_SUFFIXES = (".cmd", ".bat")
JUNIT_GLOB_MAX_FILES = 500

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
