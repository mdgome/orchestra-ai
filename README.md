# Multi-Agent Orchestrator

> 한국어: [README_ko.md](README_ko.md)

A FastAPI service that drives **Codex CLI** and **Claude Code CLI** through a single deterministic workflow.

```
PLAN (Claude, Plan Mode) -> DESIGN_REVIEW (Codex, read-only) -> [Human Gate] -> DEVELOP (Claude)
      -> TEST (Claude writes tests, the Orchestrator runs them) -> CODE_REVIEW (Codex, read-only) -> COMPLETED
                         ^---------------------- up to 3 improvement iterations ----------------------'
```

The Orchestrator itself is composed purely of Python and uses no LLM. Success is judged not by the description an agent
writes, but by exit codes, JUnit results, and structured reviews.

| Role | Provider | Permissions |
|---|---|---|
| PLANNER | Claude `--permission-mode plan` | Read/explore only (`Read,Glob,Grep`) |
| DESIGN_REVIEWER | Codex `-s read-only` | Read-only |
| DEVELOPER | Claude | Edit + limited Bash |
| TESTER | Claude | Edit under `tests/` + `pytest` only (verified by the Orchestrator via snapshot diff) |
| CODE_REVIEWER | Codex `-s read-only` | Read-only |

## Scope of Support (Please Read First)

* **A single Runner (Coordinator) process, a trusted local environment.** Only one Runner process per database is
  allowed (enforced via a PostgreSQL advisory lock; a second process refuses to start as a Runner).
  Run with `uvicorn app.main:app --workers 1`. Parallelism is at the Task level **inside** this process (Tasks across
  different Projects run in parallel; change Steps within the same Project are serialized by the Project write lock).
* **Project usage rules.** Direct modification by external processes (editors, build tools, etc.) while a Project
  has an active Task is **not supported.** The filesystem and the DB cannot form a single atomic transaction, and
  changes made between measurement and commit may not be fully detected (if detected, the read phase is retried and
  the change phase becomes a Recovery target). User modifications between Tasks (while the Project is idle) are
  allowed and are accepted as the baseline the next time a Task first acquires the write lock. The Orchestrator does
  not `reset`, `clean`, `checkout`, or delete Project files.
* CLI permission flags are **tool policy, not an OS sandbox.** They cannot isolate a malicious or compromised model,
  or a CLI bug. OS/container isolation is a Phase 3 item. Do not run against untrusted requirements on a system that
  holds secrets you cannot afford to lose.
* **Exactly-once execution is not guaranteed.** External side effects may occur at least once. The Orchestrator
  detects workspace changes, and when it cannot prove that no work occurred, it does not automatically re-run the
  file-changing step; instead it opens a `RECOVERY` Human Gate.
* WebSocket events are best-effort, and PostgreSQL is the ultimate source of truth. After a disconnect, re-query
  `GET /tasks/{id}`, `/executions`, `/state-transitions`.
* Not included (Phase 3): Redis, Celery, Git worktrees, container isolation, distributed Runners, usage tracking.

## Running

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"      # Windows; use .venv/bin/pip on POSIX
docker compose up -d postgres                                       # or PostgreSQL 15+
cp .env.example .env
.venv/Scripts/alembic upgrade head
.venv/Scripts/uvicorn app.main:app --workers 1
```

`codex` and `claude` must be installed and logged in. A preflight check at startup verifies that the installed
versions support every flag used. It also inspects `~/.claude/settings*.json`, the project's `.claude/`, and
`~/.codex/config.toml` for settings that escalate permissions (e.g. `bypassPermissions`), and stops the Runner if
anything blocking is found. The login state itself is **not** verified (a probe would run the CLI and spend tokens): the
diagnostics below report it as `UNKNOWN`.

## API

Use nouns in the URI and express actions through the HTTP method. Errors always use the
`{"error": {"code", "message", "details"}}` format.

| Method | URI | Description |
|---|---|---|
| POST | `/conversations` | 201 |
| GET | `/conversations/{id}` | |
| POST | `/conversations/{id}/messages` | 201; 409 if the conversation is CLOSED/EXPIRED |
| POST | `/projects` | 201. Specify exactly one of `workspace_path` (an existing local Workspace) **or** `repository_url` (cloned under `PROJECT_CLONE_ROOT`). Optional `test_policy` (e.g. `{"preset": "maven"}`) |
| GET | `/projects` · `/projects/{id}` | |
| PATCH | `/projects/{id}` | `name`, `status` (`ARCHIVED` returns 409 if there are unfinished Tasks), `test_policy` (omitted = unchanged, object = validated then replaced, `null` = removed; `null` for `name`/`status` is 422. Policy copies already made for existing Tasks are unaffected) |
| GET/POST | `/projects/{id}/tasks` | List/create Tasks that share the Project Workspace (201, does not execute) |
| GET | `/projects/{id}/profile` | The active Project Profile version (see *Project Profiles*) |
| GET/POST | `/projects/{id}/profile-versions` | List versions (newest first, paginated) / create **and activate** a new version: 201, or 200 `created: false` when identical to the active one |
| GET | `/projects/{id}/diagnostics` | Read-only readiness check (see *Health and Diagnostics*); `?refresh=true` bypasses the 30 s cache |
| GET | `/health` | API liveness plus Runner readiness (see *Health and Diagnostics*) |
| GET/POST | `/tasks` | POST only **creates** a PENDING task (201); it does not execute anything |
| GET/PATCH | `/tasks/{id}` | The GET response includes the current lease, latest checkpoint, and any pending approvals |
| POST | `/tasks/{id}/execution-requests` | 202. Stores the request; the Runner executes it. Idempotent while a request is pending |
| POST | `/tasks/{id}/cancellation-requests` | 202. Records the request; marked CANCELLED only after CLI process termination is confirmed |
| GET | `/tasks/{id}/executions` · `/state-transitions` · `/approvals` | History |
| GET | `/tasks/{id}/artifacts` · `/tasks/{id}/artifacts/{artifact_id}` | Stored step results, paginated (`kind`, `limit` ≤ 100, `offset`); see *Artifacts and the Completion Report* |
| GET | `/tasks/{id}/report` | Deterministic completion report built from stored facts (no LLM call) |
| GET | `/tasks/{id}/changes` | Cumulative changes since the task took the project write lock (`limit` ≤ 100, `offset`, `path`, `verify_workspace`); see *Task Changes* |
| GET | `/approvals/{id}` | |
| POST | `/approvals/{id}/decisions` | `Idempotency-Key` header required. New = 201, same request replayed = 200, conflict/expired data = 409, deadline expired = 410 |
| WS | `/tasks/{id}/event-stream` | Real-time events |

Approving a decision stores the decision content and the `RESUME` execution request in a single transaction. So even
if a failure occurs right after approval, the work resumes the next time the Runner starts.

## Artifacts and the Completion Report

Everything here is derived from what is already stored (task steps, executions, approvals, state transitions). No agent,
command or LLM is called to produce it, and nothing the agents never produced is invented.

* `GET /tasks/{id}/artifacts?kind=&limit=&offset=` lists the stored step results (`PLAN`, `DESIGN_REVIEW`, `DEVELOP`,
  `TEST`, `CODE_REVIEW`), oldest first (`created_at`, then `id`). Each item carries `plan_revision`,
  `workflow_iteration` and `step_attempt` separately (a replan, an improvement iteration and a technical retry are
  different things), `is_latest_succeeded` (the most recently produced SUCCEEDED artifact of its kind, **not** "currently
  valid") and `validity`.
* `validity.state`: `CURRENT` (a plan / design review whose stored basis still matches), `STALE` (a stored basis no
  longer matches, or a later revision / iteration / attempt superseded it; `reasons` say which), `UNVERIFIED` (artifacts
  that describe workspace content: every stored basis matches, but the live workspace is not compared, so they are never
  called CURRENT), `NOT_APPLICABLE` (the step did not succeed).
* `GET /tasks/{id}/artifacts/{artifact_id}` returns one artifact with its sanitised stored output. An id that belongs to
  another task is `404`.
* `GET /tasks/{id}/report` returns the outcome and `outcome_note`, plan, design review, implementation, tests (the
  command the Orchestrator ran and its result, `run_by: ORCHESTRATOR`), code review, issues per round, Human Gate
  history, the last transition, and `next_actions`.
* `COMPLETED` means the orchestrator's completion conditions were met. It does **not** mean you accepted, merged or
  deployed the result.
* `historical_completion` shows only the stored COMPLETED transition. The Definition-of-Done detail of that moment was
  never stored, so its `conditions` are `UNKNOWN` and are not reconstructed. `current_re_evaluation` evaluates the same
  conditions now against the stored artifacts (`evaluated_at`, `evaluation_basis`); it neither confirms nor cancels a
  past completion and does not compare the live workspace.
* Responses never contain native session ids, credentials or the step manifests. One recursive sanitiser masks the
  task's known session ids (ordinary UUIDs stay), URL credentials and tokens, removes denied keys, and bounds depth, keys,
  array items, string length and size. What was cut is named in `sanitization` / `SanitizedJson.reasons`, never inside
  the data. A page over its size limit drops trailing items: `total` stays the database count, while `returned`,
  `omitted_items`, `size_limited`, `has_more` and `next_offset` say what happened; the omitted items are re-fetchable from
  `next_offset`. The application refuses to start if a limit cannot hold even one answer.

## Task Changes

When a task takes the project write lock, the orchestrator records a **baseline** of the workspace in the same transaction:
the Changes manifest (`CHANGES_MANIFEST_V1`: per path a `FILE` {sha256, executable}, `SYMLINK` {target} or `GITLINK` {oid}),
HEAD (`COMMIT` or `UNBORN`) and the paths that were already dirty. **Only hashes are stored, never file content.**
`GET /tasks/{id}/changes` compares the live workspace with it.

* **Two measurements, two failure policies.** The Project fingerprint (`WORKSPACE_FINGERPRINT_V1`, unchanged) decides whether
  the task may own the project: if it cannot be measured the existing fail-closed path applies (no owner, no baseline,
  `DIRTY_UNRESOLVED`, the `PROJECT_DIRTY` gate). The manifest, HEAD and dirty paths are supplementary: if only they fail, the
  task **continues** and an `UNAVAILABLE` baseline row with a reason code (`TOO_MANY_FILES`, `DIRTY_PATHS_LIMIT`, `DEADLINE`,
  `GIT_ENUMERATION_FAILED`, ...) is written in the same transaction as the owner. Order: fingerprint, manifest, fingerprint
  again; if the two fingerprints differ the measurement is stale and is retried within the existing bounded retry.
* **Epochs.** `proof_epoch` = project epoch when the fingerprint was measured (before acquisition), `acquired_epoch` = the
  epoch the acquisition produced (`proof_epoch + 1`), `recorded_epoch` = epoch when the row was written. `set_owner` states the
  epoch it produced; nothing guesses the `+1`. A task that already owned the lock without a baseline row (a crash gap, or an
  owner from before this feature) never gets READY without one: if it changed nothing and the fingerprint still equals its
  write baseline the manifest is recovered, otherwise an explicit `UNAVAILABLE` row (`BASELINE_MISSING_AT_REENTRY`) with
  **null** proof/acquired epochs is written: the original epoch is not invented.
* **Scope.** GIT mode covers tracked files and untracked non-ignored files (ignored build output is excluded); CONTENT mode
  (no usable git) covers the workspace-policy walk. `executable` is the working-tree mode bit (never on Windows). Not covered:
  ignored files, the inside of a submodule (only its commit id), and a mode change that is only staged. The response lists
  `limitations`.
* **Bodies.** A text diff exists only in GIT mode with a `COMMIT` head, for a file that was clean at the start (the old content
  comes from `git cat-file`). Otherwise `diff_status` is `UNAVAILABLE` with a reason (`BASELINE_CONTENT_NOT_STORED`,
  `UNBORN_HEAD`, `NO_BASELINE_CONTENT_WITHOUT_GIT`, `BINARY`, `LFS_POINTER`, `SYMLINK_TARGET_ONLY`,
  `SUBMODULE_CONTENT_NOT_STORED`), `WITHHELD` (`SENSITIVE_PATH`: `.env*`, keys, certificates, credentials; the path is listed, the
  content never; `PROJECT_OWNED_BY_OTHER_TASK`) or `TRUNCATED` (`FILE_TOO_LARGE`, `BODY_BUDGET`, `DEADLINE`, `SIZE_LIMIT`). A file
  that was already dirty is flagged `pre_existing_dirty` and is not claimed as the task's own. Bodies are credential-masked.
  Only `rev-parse`, `ls-files`, `status` and `cat-file` are run: no reset, clean, checkout, commit or push.
* **A live comparison, not a snapshot.** `lock_consistency` (owner, write epoch and workspace state compared before and after;
  one retry when they moved) and `workspace_consistency` are separate: an outside process can change files without touching the
  database. `workspace_consistency` is `STABLE` only with `verify_workspace=true` when two real measurements agree, and
  `UNVERIFIED` (with a reason) otherwise. A finished task's answer is not settled once another task changed the workspace
  (`workspace_relation`).
* **Bounded.** One deadline reaches the measuring code itself: git is killed by `min(its timeout, remaining)`; the walk and
  hashing check it between entries and chunks. That filesystem deadline is **cooperative**: a read that stalls inside the OS
  cannot be interrupted, so the worker may outlive it. At most one measurement per project and `CHANGES_MAX_CONCURRENT_GLOBAL`
  overall run; a slot is returned when the worker thread really ends (not when the request gives up), a busy answer is
  `503 CHANGES_MEASUREMENT_BUSY`, and after a timeout `verify_workspace` is refused for a cooldown (`UNVERIFIED`, reason
  `COOLDOWN`). A task without a project is `UNAVAILABLE` (`NO_PROJECT`), a task that never took the lock `NO_BASELINE`.

## Health and Diagnostics

`GET /health` answers 200 while the API process is alive. The original keys keep their meaning; `ready`, `warnings` and `runner` are new:

```json
{"status": "ok", "runner_active": false, "ready": false, "warnings": [],
 "runner": {"lifecycle": "STOPPED", "startup_outcome": "PREFLIGHT_BLOCKED", "blockers": ["PREFLIGHT_FAILED"],
            "background_degraded": false,
            "sweeper": {"stalled": false, "consecutive_failures": 0, "last_failure_code": null, "last_failure_at": null,
                        "next_retry_at": null, "config_invalid_active": false, "clearance_pending": false}}}
```

* `status` is API liveness only. `runner_active` and `ready` are **derived from `Runner.lifecycle`** (nothing sets them by
  hand): `runner_active` is true only while the Runner loop runs, `ready` additionally requires that nothing blocks it
  (for example an unsettled recovery).
* While the Runner is not ready, `POST /tasks/{id}/execution-requests` still answers 202 and the request stays `PENDING`
  until a Runner is ready. `runner.blockers` explains why: `PREFLIGHT_FAILED`, `ADVISORY_LOCK_HELD`, `STOPPED`,
  `STARTING`, `SHUTTING_DOWN`, `STOP_INCOMPLETE`, `RECOVERY_UNSETTLED`, `SUPERVISOR_DEGRADED` (the background supervisor
  ended; the Runner claims no new work). `START_FAILED` is **reserved**: a general exception from `Runner.start` still
  aborts API startup (decision D6), so a running API never reports it.
* `warnings` is a sorted list of `SWEEPER_STALLED`, `SWEEPER_CONFIG_INVALID` and `BACKGROUND_COMPONENT_DEGRADED`.
  `runner.background_degraded` is true only while the supervisor or a required background unit (orphan sweeper, risk
  worker, capability refresh) is really degraded; a degraded unit other than the supervisor does not block dispatch.
  `runner.sweeper` reports the orphan sweeper's stall and configuration state.

`GET /projects/{id}/diagnostics` is a **read-only quick check**. It never runs an agent, the login probe, an installer or
the project's tests; it only runs tool version probes (`git --version`, `java -version`) and reads files. Every item has
`code`, `status` (`PASS`/`WARN`/`FAIL`/`UNKNOWN`), `checked_at`, `cause`, `recommendation` and `blocks_execution`;
`can_start` is false only when an item that blocks execution FAILED. It is **not** a guarantee that a task can run or
will succeed. Notes:

* `CLI_AUTH` is always `UNKNOWN`. `PYTHON_INTERPRETER` reports a discovered project `.venv`, but the orchestrator's own
  interpreter is what runs the tests. `JUNIT_COLLECTION` warns that Gradle may skip an UP-TO-DATE test task.
* Project paths are re-validated from the registered root; only `.claude/settings.json` and `.claude/settings.local.json`
  inside it are read, and a link or junction there is a blocking finding. Replacing the real directory at the same
  canonical path with another one is **not** detected.
* One diagnosis per project runs at a time and concurrent requests share it. File inspection runs in a worker thread
  with a best-effort timeout: a stalled filesystem cannot be interrupted, so the overall deadline is **not** a hard
  deadline. Such a worker keeps its slot until it really ends, and while it lives the file-derived items are `UNKNOWN`.
* `PROCESS_TRACKING` reports the process-tracking capability of the Runner (`PASS` supported; `WARN` on an unsupported
  platform, where tracked commands degrade to untracked; `FAIL` with `blocks_execution` when the self-test failed;
  `UNKNOWN` when it cannot be confirmed). When it is not supported, the item carries `blocks_features: ["TEST_RUN"]`.
  `TRACKED_EXECUTIONS` lists the unresolved or anomalous tracked executions of the project as `causes` (each with
  `reason_code`, severity, `blocks_execution`, `recommendation`, related execution/request ids, and for anomalies a bounded
  list); the item status is the highest severity of its causes. The checks changed, so `DIAGNOSTICS_POLICY_VERSION` is 2
  (part of the cache key).

## Project Profiles

The built-in engineering rules used to be injected into every prompt of every project. A Project Profile lets one project
tune them. It is **data**, versioned and immutable per version:

```json
{"description": "Billing service", "languages": ["Java"], "frameworks": ["Spring"],
 "architecture_rules": ["Hexagonal modules"], "coding_rules": ["No static state"],
 "completion_criteria": [{"id": "DOC", "text": "the public API is documented"}], "test_policy_ref": "project"}
```

* A **new** Project without `profile` gets the **neutral** v1 (no architecture imposed). Projects that **existed before**
  profiles got `legacy` v1 (migration `0003`), whose rules render exactly like the old built-in text. A Task without a
  Project, or created before profiles, keeps the built-in rules unchanged (its `profile_*` fields are null).
* Project row, v1 and the active pointer are created in one transaction. `POST /projects/{id}/profile-versions` creates
  **and activates** a version under the project row lock; content identical to the active version writes nothing (200,
  `created: false`). An ARCHIVED Project takes no new version but stays readable; no profile row is ever deleted.
* A Task **copies** the active version (number, hash, content) when it is created, under the same lock. A later change
  never alters a task that already exists (`profile_version`, `profile_hash` are on the Task).
* The profile hash is part of the design-review basis and of the PLAN, DEVELOP and CODE_REVIEW input hashes **only for
  tasks that have a profile**, so a result produced under one profile is never reused under another, and tasks without a
  profile keep their hashes exactly as before.
* The orchestrator's own mechanics (Human Gate rule, "never use more tools than you were given") are not part of the
  profile and cannot be removed by it. Unknown keys are **rejected**: a profile cannot carry permissions, tools, models,
  paths or interpreters (those stay in the trusted policy, `providers/permissions.py`), and `test_policy_ref` can only be
  `"project"`: the project's own `test_policy` is referenced, never redefined.
* `completion_criteria` are an **additional** Definition-of-Done condition. The code reviewer must return `profile_criteria`
  with exactly one `{id, met, evidence}` per criterion; the result is stored with the profile hash. The orchestrator
  checks **deterministically** that the stored evaluation is for the current profile, covers exactly those ids (no missing,
  duplicate or unknown id), and that each is `met` with non-empty `evidence`. Whether a criterion is really satisfied is
  the **reviewer's judgement**; the orchestrator does not verify it independently. Every other condition (tests, review
  verdict, fingerprints, design review, Human Gate) still applies unchanged.

## Using Projects

```bash
# 1) Register an existing local project (absolute path, no symlinks/junctions, must not overlap system/orchestrator paths or another Project)
curl -X POST localhost:8000/projects -H 'content-type: application/json' \
     -d '{"name": "Service A", "workspace_path": "D:\\Projects\\Service-A", "test_policy": {"preset": "pytest"}}'
# 2) Create a natural-language Task -> request execution
curl -X POST localhost:8000/projects/{id}/tasks -d '{"requirements": "..."}' -H 'content-type: application/json'
curl -X POST localhost:8000/tasks/{task_id}/execution-requests
```

* `repository_url` only accepts the `https://`, `ssh://`, and `git@host:path` formats, and rejects URLs that contain
  credentials (`user:token@host`). Authentication is left to the system credential helper or ssh-agent, and
  credentials in URLs/error messages are masked in logs, the DB, and API responses.
* Test Policy presets: `pytest` (default), `maven`, `gradle`. At Task creation it's resolved to a single tool — the
  project's `mvnw`/`gradlew` (falling back to the system tool) — and if no tool is found, Task creation is rejected
  with 422. Test results (JUnit XML) are not deleted, and only files **newly created or changed by this run** are
  used for the verdict.
* If a previous Task left a Project with unresolved changes (`workspace_state: DIRTY_UNRESOLVED`), the
  `PROJECT_DIRTY` RECOVERY Gate opens when the next Task starts a change Step. `abort` fails the Task (without touching
  any files). `continue` accepts the current state as the new baseline, but **only while the Project has no unresolved
  and no anomalous tracked test execution**; otherwise it is hidden (`options_hidden`, reason `UNRESOLVED_ROWS_PRESENT`
  or `TRACKED_ANOMALY_PRESENT`). For a Project with unresolved tracked executions the gate offers instead
  `accept_unproven_risk` (risk HIGH; an operator accepts the risk of a process that could not be proven gone; it needs a
  `rationale`), `recheck_processes` (only after a live process was observed) or, for a stale gate, `refresh_gate`. Neither
  risk option makes the Project `CLEAN`; only `continue` does.
* **Tracked test commands.** On Windows the Orchestrator's own test commands run as tracked executions: the process is
  spawned suspended, assigned to a Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), its identity is persisted
  with a fence on the `executions` row (`detail.tracked`), and only then resumed, so a row without a `pid` never ran user
  code. The tree is terminated through the Job. If the tree cannot be proven empty, the Job stays retained in the Runner
  process and the row stays unresolved. After a Runner restart the Job's processes are ended by the OS when the owning
  process ends (a conditional OS guarantee that the recovery verdict treats as an assumption, switchable). A background
  orphan sweeper and a risk worker settle such rows. Migration `0005` adds `runtime_capabilities`,
  `process_risk_requests`, the partial indexes `ix_executions_tracked_unresolved` and `ix_executions_tracked_anomaly`, and
  `uq_approvals_lineage_successor`.
* **Verification scope.** Verified by tests on Windows: the real Job Object behaviour (tree kill, owner-process death,
  crash points of the spawn protocol). POSIX has a policy only: an unsupported platform degrades tracked commands to
  untracked execution, and an unknown or unavailable capability fails closed (`PROCESS_TRACKING_UNAVAILABLE`); its runtime
  behaviour is not verified. Not verified: POSIX runtime, real Gradle/Maven builds, real CLI end-to-end runs, random race
  stress, EXPLAIN/generic plans, real deadlock tests.
* **Known limits of tracked executions.** A process spawned but not yet assigned to the Job (the CP2 window) leaves a row
  indistinguishable from "nothing spawned": user code cannot have run, but a Job-less suspended process may survive a close
  as `NEVER_RAN`; this is accepted by design. A pid-less row recorded as `SPAWNED_PROCESS_UNPROVEN` is not resolved by a
  restart and has **no supported resolution path**: a valid acceptance record needs a `pid`, so the gate hides
  `accept_unproven_risk` (`TARGET_WITHOUT_PID`) and a direct submit is rejected with 409 `OPTION_NOT_AVAILABLE`, nothing
  written. `abort` only terminates the Task and does not clear `DIRTY_UNRESOLVED`; the row can only be settled by cleaning up
  the process and editing the database by hand (a restart Windows test is a separate, unverified follow-up; details in
  `docs/workflow/08-human-gate.md` and `09-recovery.md`).

Settings (`.env`): `MAX_CONCURRENT_TASKS` (default 2), `MAX_TASKS_PER_PROJECT` (default 1),
`MAX_CONCURRENT_CLI_PROCESSES` (default 4), `RUNNER_SHUTDOWN_GRACE_SECONDS` (default 30), `PROJECT_CLONE_ROOT`
(default `./project-clones`).

Request recovery timeouts (`.env`): `RECOVERY_STATEMENT_TIMEOUT_SECONDS` (default 15, PostgreSQL statement limit),
`RUNNER_RECOVERY_TIMEOUT_SECONDS` (default 20, time to wait for a recovery operation), `RUNNER_RECOVERY_SHUTDOWN_WAIT_SECONDS`
(default 5, time to wait for settlement after a cancellation request), `DB_POOL_TIMEOUT_SECONDS` (default 30,
connection checkout). All must be greater than 0, and the statement timeout must be smaller than the recovery
timeout. The recovery wait and post-cancellation settlement are bounded, but `Runner.stop` as a whole is not fully
bounded, and closing the PostgreSQL advisory lock connection currently has no separate upper bound.

**DB pool** (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`): this section is authoritative (`.env.example` only lists the keys
and defaults).

* There is only one hard requirement. `DB_POOL_SIZE + DB_MAX_OVERFLOW` (total capacity) must be **at least 2**,
  because the Runner's advisory lock occupies one connection for the Runner's entire lifetime, so there must be at
  least one more connection available for business transactions. `DB_POOL_SIZE` must be at least 1, and
  `DB_MAX_OVERFLOW` must be at least 0 (`0`, a fixed pool, is also valid).
* If `DB_POOL_SIZE` is not specified, the **recommended value** `4 × MAX_CONCURRENT_TASKS + 6` is used (14 by
  default). The recommended value is an **estimate** meant to lower throughput and Lease heartbeat delay risk, and
  is not enforced: each running Task can overlap short transactions (Step recording, Lease heartbeat, cancellation
  confirmation), and the Runner loop and API requests consume additional connections. A valid pool smaller than the
  recommended value is not rejected. A small pool shows up not as an error but as **waiting** (waiting until a
  connection becomes available).
* Raising `MAX_CONCURRENT_TASKS` also raises the pool size when it is not explicitly set. An explicitly set
  `DB_POOL_SIZE` does not change.

## Testing

```bash
.venv/Scripts/python -m pytest                    # Full test suite excluding real CLIs (no Docker or installed PostgreSQL required)
.venv/Scripts/python -m pytest -m e2e_cli         # Real Codex + Claude CLI tests (slow, consumes tokens)
```

The PostgreSQL integration tests start an **embedded PostgreSQL** (`pixeltable-pgserver`, a test-only dev
dependency), so Docker is not required. If it cannot start, the tests raise a clear error and no item is silently
skipped. The test schema is created via real Alembic migrations, and `alembic check` verifies that the models and
schema continue to match.

See `docs/ARCHITECTURE.md` for design decisions and a list of what is **not** verified.
