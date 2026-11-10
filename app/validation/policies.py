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
# Request recovery. statement < operation: the server-side limit fires before the Runner gives up waiting. The shutdown
# wait is how long a cancelled recovery may take to roll back / release its connection before it counts as PENDING.
DEFAULT_RECOVERY_STATEMENT_TIMEOUT_SECONDS = 15.0
DEFAULT_RUNNER_RECOVERY_TIMEOUT_SECONDS = 20.0
DEFAULT_RUNNER_RECOVERY_SHUTDOWN_WAIT_SECONDS = 5.0
DEFAULT_DB_POOL_TIMEOUT_SECONDS = 30.0             # SQLAlchemy's own default, stated explicitly
MAX_PG_TIMEOUT_MS = 2_147_483_647                  # statement_timeout is a 32-bit integer of milliseconds

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

# --- Project diagnostics (read-only) -----------------------------------------------
DIAGNOSTICS_POLICY_VERSION = 1             # part of the cache key: bump it when the set of checks changes
DIAGNOSTICS_ITEM_TIMEOUT_SECONDS = 5.0     # one check: a subprocess, or one filesystem inspection
DIAGNOSTICS_DEADLINE_SECONDS = 15.0        # the whole run; a check still pending is reported UNKNOWN
DIAGNOSTICS_CACHE_TTL_SECONDS = 30.0
# Global cap of filesystem-inspection worker threads, abandoned ones (a waiter timed out) included: a thread cannot be
# killed, so a stalled filesystem may keep a worker alive. The cap is what stops such workers from piling up.
DIAGNOSTICS_MAX_FS_WORKERS = 4
PROJECT_SETTINGS_RELATIVE_PATHS = (".claude/settings.json", ".claude/settings.local.json")

# --- Response sanitising and size limits (artifacts / report) ------------------------
SANITIZE_MAX_DEPTH = 8
SANITIZE_MAX_KEYS = 100                 # per object
SANITIZE_MAX_ITEMS = 200                # per array
SANITIZE_MAX_STRING_CHARS = 4000
SANITIZE_MAX_NODES = 5000               # whole value
SANITIZE_MAX_OMITTED_PATHS = 50         # how many omissions are named in `omitted_paths`
SANITIZE_SHRINK_MAX_ROUNDS = 256        # byte-budget shrinking of one value
SANITIZE_MIN_SHRINK_STRING = 256        # a string is never cut below this by the byte-budget shrinker
# Exact key names that are removed from any returned JSON, plus a reviewed suffix rule. `session_fingerprint` and
# `token_count` are normal fields: they are allow-listed, never removed by substring matching.
SANITIZE_DENY_KEYS = frozenset({
    "native_session_id", "session_id", "snapshot_before", "snapshot_after", "idempotency_key", "password", "passwd",
    "secret", "api_key", "apikey", "authorization", "cookie", "token", "access_token", "refresh_token"})
SANITIZE_DENY_KEY_SUFFIXES = ("_token", "_secret", "_password", "_api_key", "_apikey")
SANITIZE_ALLOW_KEYS = frozenset({"session_fingerprint", "token_count", "tokens", "max_tokens", "total_tokens"})
SANITIZED_JSON_MAX_BYTES = 64 * 1024            # one free-form JSON field
ARTIFACT_ITEM_MAX_BYTES = 128 * 1024            # one artifact (summary + content + error)
ARTIFACT_LIST_RESPONSE_MAX_BYTES = 512 * 1024   # a whole page of artifacts
REPORT_RESPONSE_MAX_BYTES = 512 * 1024
RESPONSE_ENVELOPE_MAX_OVERHEAD_BYTES = 8 * 1024  # everything in a list response except its items
RESPONSE_SHRINK_MAX_ROUNDS = 8                  # re-truncation rounds of one response before the bounded fallback
ARTIFACT_LIST_MAX_LIMIT = 100
REPORT_ITERATIONS_MAX = 50
REPORT_GATES_MAX = 50
REPORT_ISSUES_MAX = 50                          # per list of review issues in the report

# --- Project profile -------------------------------------------------------------------
# A profile is DATA that tunes the prompts of one project. It can carry no permission, tool, model, path or interpreter:
# those keys are not accepted at all (they belong to the trusted policy, see providers/permissions.py).
PROFILE_SCHEMA_VERSION = 1
PROFILE_KEYS = frozenset({"schema_version", "description", "languages", "frameworks", "architecture_rules",
                          "coding_rules", "completion_criteria", "test_policy_ref"})
PROFILE_DESCRIPTION_MAX = 2000
PROFILE_LABEL_MAX = 64                  # one language / framework name
PROFILE_LABELS_MAX = 20
PROFILE_RULE_MAX = 500                  # one architecture / coding rule, one criterion text
PROFILE_RULES_MAX = 30
PROFILE_CRITERIA_MAX = 20
PROFILE_CRITERION_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"
PROFILE_TEST_POLICY_REF = "project"     # the only accepted value: the project's own test_policy is referenced, not copied
PROFILE_VERSIONS_MAX_LIMIT = 200

# --- Task baseline and cumulative changes ------------------------------------------------
# Changes manifest: path -> tagged entry, gzip-compressed in the database. Every bound is checked on write AND on read.
MANIFEST_MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MANIFEST_MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MANIFEST_MAX_FILES = 100_000
MANIFEST_MAX_PATH_LEN = 1024
MANIFEST_MAX_SYMLINK_TARGET_LEN = 1024
# Paths that were already dirty when the task took the write lock (so their content is not the task's own start state).
DIRTY_PATHS_MAX_ITEMS = 20_000
DIRTY_PATHS_MAX_PATH_LEN = 1024
DIRTY_PATHS_MAX_SERIALIZED_BYTES = 2 * 1024 * 1024
CHANGES_BASELINE_DEADLINE_SECONDS = 120.0   # collecting the supplemental baseline at lock acquisition (cooperative)
CHANGES_DEADLINE_SECONDS = 10.0             # one GET /tasks/{id}/changes, including a verification re-measure
CHANGES_DEADLINE_GRACE_SECONDS = 2.0        # how long the waiter lets a cooperative worker notice an expired deadline
CHANGES_MAX_CONCURRENT_PER_PROJECT = 1      # a slot is released when the worker REALLY ends, not when the waiter gives up
CHANGES_MAX_CONCURRENT_GLOBAL = 2
CHANGES_VERIFY_COOLDOWN_SECONDS = 60.0      # after a deadline, verify_workspace requests of that project are refused
CHANGES_FILE_DIFF_MAX_BYTES = 256 * 1024
CHANGES_TOTAL_DIFF_MAX_BYTES = 1024 * 1024
CHANGES_MAX_DIFF_FILES = 200
CHANGES_PAGE_MAX_LIMIT = 100
# Paths whose content is never returned in a diff body (the path itself is still listed).
SENSITIVE_PATH_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*",
                           "credentials*", "*.keystore", "*.jks", "secrets.*", ".netrc", ".npmrc", ".pypirc")
CHANGES_RESPONSE_MAX_BYTES = 2 * 1024 * 1024    # a whole /changes answer: the diff bodies (<= CHANGES_TOTAL_DIFF_MAX_BYTES) plus a page
