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
        "tool": "gradle", "args": ["cleanTest", "test", "--console=plain", "--no-build-cache"], "junit_path": "**/build/test-results/test/TEST-*.xml",
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
DIAGNOSTICS_POLICY_VERSION = 2             # part of the cache key: bump it when the set of checks changes (2: causes, 14.3)
DIAGNOSTICS_ITEM_TIMEOUT_SECONDS = 5.0     # one check: a subprocess, or one filesystem inspection
DIAGNOSTICS_DEADLINE_SECONDS = 15.0        # the whole run (cooperative); a stage with no time left is not started (UNKNOWN), the partial report is not cached
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

# --- B2 process lifecycle hardening (docs/design/process-lifecycle-hardening.md, 9.2 - 9.4) ---------------------------
# Every constant below is a code literal: no environment variable and no Settings field overrides it. All defaults are
# design defaults proposed by the design (none is verified on a real host). Relations between them are checked together
# by validate_process_timing_limits(), validate_process_input_constants() and validate_process_risk_limits() when the
# Runner is constructed (validate_process_policy()); nothing here is read by a behaviour yet except those validators.

# Orphan sweeper (9.2).
ORPHAN_SWEEP_BATCH_ROWS = 50                     # rows per pass (U and A together)
ORPHAN_SWEEP_PASS_SECONDS = 60                   # budget of one pass
ORPHAN_SWEEP_INTERVAL_SECONDS = 30.0             # between two passes
ORPHAN_SWEEP_BACKOFF_MIN_SECONDS = 30.0          # delay after the first failed pass
ORPHAN_SWEEP_BACKOFF_MAX_SECONDS = 300.0         # cap of the exponential backoff
ORPHAN_SWEEP_LOG_THROTTLE_SECONDS = 300.0
ORPHAN_SWEEP_MAX_INTERVAL_SECONDS = 86400.0      # ceiling of every sweeper retry/throttle interval (exactly this value)
ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS = 86400.0   # retry wait after SWEEP_CONFIG_INVALID (exactly the ceiling)
ORPHAN_SWEEP_STARTUP_WAIT_SECONDS = 10           # startup recovery waits for the first pass
ORPHAN_SWEEP_STALL_THRESHOLD = 5                 # consecutive failed passes before SWEEPER_STALLED (1..1000)
ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP = 16           # exponent cap before the power (0..62)

# Background supervisor and components (9.2, 9.3).
BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS = 15.0
BACKGROUND_READY_TIMEOUT_SECONDS = 5.0
BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS = 5.0
BACKGROUND_RESTART_BACKOFF_BASE_SECONDS = 5.0
BACKGROUND_RESTART_BACKOFF_CAP_SECONDS = 60.0
BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP = 10     # 0..62
BACKGROUND_MAX_RESTARTS = 5                      # consecutive failures per component (1..100)
BACKGROUND_STABLE_SECONDS = 300.0
BACKGROUND_RESTART_WINDOW_SECONDS = 3600.0
BACKGROUND_RESTART_WINDOW_MAX = 10               # 1..1000
BACKGROUND_WAKEUP_DEBOUNCE_SECONDS = 5.0
BACKGROUND_MIN_INTERVAL_SECONDS = 0.1            # floor of every loop/retry/throttle/debounce interval (fixed literal 0.1)
BACKGROUND_DB_CONCURRENCY = 1                    # 1..8
BACKGROUND_COMPONENT_IDS = ("sweeper", "risk-worker", "capability")

# Process-tracking capability (6.2).
CAPABILITY_REFRESH_SECONDS = 10
CAPABILITY_WRITE_TIMEOUT_SECONDS = 5
CAPABILITY_TTL_SECONDS = 30

# Tracked command execution (5.1, 9).
PUMP_DRAIN_SECONDS = 5
TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS = 10
TREE_SETTLE_POLL_SECONDS = 1.0
JOB_CLOSE_SETTLE_SECONDS = 2.0
JOB_TERMINATE_EXIT_CODE = 0x4B494C4C             # 1263094860, ASCII "KILL"; 1 .. 2147483647
# Polling granularity of the executor (phase 2; the design leaves these two intervals undefined, 5.1 step 8/9, 9).
EXECUTOR_POLL_SECONDS = 0.1                      # root-exit / cancel / timeout / idle tick (> 0, finite)
JOB_POLL_INTERVAL_SECONDS = 0.05                 # ActiveProcesses polling interval (> 0, finite)

# Operator risk acceptance worker (7.5 item 5).
PROCESS_RISK_IDLE_INTERVAL_SECONDS = 15
PROCESS_RISK_ROW_OVERHEAD_SECONDS = 3
PROCESS_RISK_ROW_TIMEOUT_SECONDS = 10
PROCESS_RISK_DB_OVERHEAD_SECONDS = 20
PROCESS_RISK_REQUEST_TIMEOUT_SECONDS = 120
PROCESS_RISK_PASS_OVERHEAD_SECONDS = 20
PROCESS_RISK_PASS_SECONDS = 150
PROCESS_RISK_CLAIM_MARGIN_SECONDS = 120
PROCESS_RISK_CLAIM_TIMEOUT_SECONDS = 300
PROCESS_RISK_BACKOFF_BASE_SECONDS = 5
PROCESS_RISK_BACKOFF_CAP_SECONDS = 60
PROCESS_RISK_MAX_ATTEMPTS = 5
PROCESS_RISK_BATCH_REQUESTS = 4
PROCESS_RISK_BATCH_ROWS = 32
PROCESS_RISK_MAX_TARGETS = 8                     # 1 .. PROCESS_RISK_TARGETS_DB_LIMIT
PROCESS_RISK_TARGETS_DB_LIMIT = 500              # schema-fixed: the cardinality CHECK of migration 0005
PROCESS_RISK_NORMAL_UNRESOLVED_PER_TASK = 1
PROCESS_RISK_TARGET_LOCK_SECONDS = 3
PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS = 2
PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS = 1

# Recheck rate limit (7.5 item 8).
PROCESS_RECHECK_COOLDOWN_BASE_SECONDS = 60
PROCESS_RECHECK_COOLDOWN_CAP_SECONDS = 900
PROCESS_RECHECK_WINDOW_SECONDS = 3600
PROCESS_RECHECK_MAX_PER_WINDOW = 4

# Diagnostics thresholds (14.1).
TRACKED_EXECUTION_MAX_AGE_SECONDS = 3660         # MAX_CLI_TIMEOUT_SECONDS + a fixed margin of 60 s (live sum is the minimum)
PROCESS_RISK_REQUEST_STALE_SECONDS = 165         # PROCESS_RISK_IDLE_INTERVAL_SECONDS + PROCESS_RISK_PASS_SECONDS
DIAGNOSTIC_MAX_ANOMALIES = 50
DIAGNOSTIC_MAX_RELATED_IDS = 50                  # ids named by one diagnostics cause (14.1): bounded like the anomaly list
DIAGNOSTIC_TRACKED_PROJECT_ROWS = 50             # process-risk requests read for one project diagnosis

# Candidate scan (7.4).
CANDIDATE_PAGE_ROWS = 50
CANDIDATE_MAX_SCANNED_ROWS = 1000
CANDIDATE_SCAN_MAX_SECONDS = 5
CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS = 2

# --- Schema-fixed input constants (9.3): the same numbers are written into migration 0005 ---------------------------
REQUESTED_BY_MAX_LENGTH = 128                    # exactly 128: CHECK char_length(requested_by) BETWEEN 1 AND 128
REQUESTED_BY_PATTERN = r"[A-Za-z0-9._@:+-]+"     # full match after trimming
PROCESS_RISK_RATIONALE_MAX_LENGTH = 2000         # exactly 2000: CHECK char_length(rationale) BETWEEN 1 AND 2000
DETAIL_SCHEMA_VERSION = 1
DISPOSITION_KIND_RISK_ACCEPTED = "OPERATOR_RISK_ACCEPTED"

# The nine detail keys that only the B2 tracked writers write (6.1); the SQL predicates are rendered from this one set.
TRACKED_ONLY_KEYS = frozenset({"v", "runner_process", "checkpoint_step_key", "proof", "disposition", "reason_code",
                               "risk_acceptance", "anomaly_original", "anomaly_settlement"})


@dataclass(frozen=True)
class RiskAcceptanceField:
    key: str
    required: bool
    json_type: str                            # the jsonb_typeof of the value
    allowed: tuple[str, ...] | None = None    # a closed value set, when the schema has one


# The one definition of the risk_acceptance record (7.5 item 7): the SQL text, the classifier and the tests follow it.
RISK_ACCEPTANCE_SCHEMA = (
    RiskAcceptanceField("approval_id", True, "string"),
    RiskAcceptanceField("execution_id", True, "string"),
    RiskAcceptanceField("pid", True, "number"),
    RiskAcceptanceField("create_time", True, "number"),
    RiskAcceptanceField("last_verdict", True, "string", ("UNCLEAR",)),
    RiskAcceptanceField("requested_at", True, "string"),
    RiskAcceptanceField("rationale", True, "string"),
    RiskAcceptanceField("reverify_result", True, "string",
                        ("UNKNOWN", "ACCESS_DENIED", "OTHER_HOST", "H3_DISABLED", "IDENTITY_RECORD_MISSING")),
    RiskAcceptanceField("applied_at", True, "string"),
    RiskAcceptanceField("requested_by", False, "string"),
)
RISK_ACCEPTANCE_REQUIRED_KEYS = frozenset(f.key for f in RISK_ACCEPTANCE_SCHEMA if f.required)
RISK_ACCEPTANCE_UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
RISK_ACCEPTANCE_TS_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"
RISK_ACCEPTANCE_REQUESTED_BY_RE = rf"^[A-Za-z0-9._@:+-]{{1,{REQUESTED_BY_MAX_LENGTH}}}$"
# Every control character except newline and tab, U+2028, U+2029 and the bidirectional controls. The low ranges stay
# regex escapes (PostgreSQL and Python both read \uXXXX); the high ones are the literal characters, as in the design.
RISK_ACCEPTANCE_RATIONALE_FORBIDDEN = ("[\u0001-\u0008\u000B-\u001F\u007F-\u009F"
                                       "\u2028\u2029\u202a-\u202e\u2066-\u2069]")

# --- B2 phase 3: tracked-row settlement (design 6.1, 7.2, 8) ------------------------------------------------------------
PROCESS_TRACKING_UNAVAILABLE = "PROCESS_TRACKING_UNAVAILABLE"       # FailClosed message / Task failure code (6.2, 7.2 item 6)
PROOF_JOB_EMPTY = "JOB_EMPTY"
PROOF_JOB_KILL_ON_CLOSE_ASSUMED = "JOB_KILL_ON_CLOSE_ASSUMED"
PROOF_NEVER_RAN = "NEVER_RAN"
POSITIVE_PROOFS = (PROOF_JOB_EMPTY, PROOF_JOB_KILL_ON_CLOSE_ASSUMED, PROOF_NEVER_RAN)
# SPAWN_FAILED reasons that are infrastructure failures (7.2 item 6): the Task fails closed, it is never a "failed test".
# SPAWN_OSERROR (the OS refused the spawn) stays a normal ERROR outcome as today.
TRACKED_INFRA_SPAWN_REASONS = frozenset({"JOB_CREATE_FAILED", "JOB_ASSIGN_FAILED", "IDENTITY_UNAVAILABLE",
                                         "IDENTITY_PERSIST_FAILED", "IDENTITY_FENCED", "RESUME_FAILED"})
TRACKED_RESULT_MISSING = "RESULT_MISSING"
TRACKED_SUSPENDED_ORPHAN_POSSIBLE = "SUSPENDED_ORPHAN_POSSIBLE"
TRACKED_SPAWNED_PROCESS_UNPROVEN = "SPAWNED_PROCESS_UNPROVEN"   # pid IS NULL but the row records an unproven spawned process
TRACKED_DEGRADE_UNTRACKED_PLATFORM = "UNTRACKED_PLATFORM"
# Decision H3 (8): "the Job dies with its owner" is a conditional OS guarantee; False turns the policy off, so every crash of a
# Runner with a recorded pid ends in TERMINATION_UNCONFIRMED. A code literal, like every constant of this block.
JOB_KILL_ON_CLOSE_ASSUMPTION_ENABLED = True
