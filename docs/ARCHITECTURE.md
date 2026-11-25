# Architecture and decision log

> 한국어: [ARCHITECTURE_ko.md](ARCHITECTURE_ko.md)

## Layers (Layered Architecture, no DDD)

```
routers/       HTTP only: parse, call one service method, map status codes
schemas/       request/response shapes (no business rules)
validation/    policies.py (all limits), validators.py, exceptions.py, error_handlers.py
services/      workflow, step runner, lease, human gate, session, test policy, runner, task service
repositories/  SQLAlchemy queries; never commit (services own the transaction); session_store = small Protocol
providers/     Claude / Codex adapters, role permission mapping, preflight, mock
infra/         command executor, process control, workspace guard/snapshot, event bus
```

Abstractions exist only where a second implementation is real: `CliProvider` (Codex, Claude, mock) and
`SessionStore` (PostgreSQL now, a read cache later). There is no generic repository and no domain layer.
`tests/architecture` enforces this mechanically for *obvious* violations; semantic review is the Code Reviewer's job.

## State machines

* **Task**: `PENDING, PLANNING, DESIGN_REVIEWING, DEVELOPING, TESTING, REVIEWING, RETRYING, WAITING_APPROVAL,
  COMPLETED, FAILED, CANCELLED`. Changes go through `state_service.change_task_state` (validated, appended to
  `state_transitions`, which a database trigger makes append-only).
* **Step**: `PENDING, RUNNING, WAITING_APPROVAL, SUCCEEDED, FAILED, INTERRUPTED, NEEDS_MANUAL_RECOVERY, CANCELLED`.
  Waiting for a human is not a failure.
* Three independent counters: `step_attempt` (technical retry), `workflow_iteration` (improvement loop, max 3),
  `plan_revision`/`replan_count` (replans). A Human Gate wait increments none of them.

## Project profiles

* `project_profiles` holds immutable versions (a trigger rejects UPDATE/DELETE); `projects.profile_version` is the active
  pointer, a composite FK so it can only name a version of the same project. A task copies version, hash and content
  (`tasks.profile_*`: all NULL, or all present with a project and a 64-character hash, enforced by a CHECK).
* The profile is DATA that tunes prompts. Orchestrator mechanics (`prompts.MECHANICS`) are never part of it; the
  orchestrator's own development rules stay in AGENTS.md / CLAUDE.md. Permissions are a function of role and test preset
  only (`providers/permissions.py`), and `validate_profile` rejects every key outside its schema.
* Tasks without a profile keep the built-in `RULES` and original hashes byte for byte (`profile_part` / `profile_criteria_of`
  add nothing when the hash is NULL). `legacy` v1 renders exactly like `RULES`; `neutral` v1 imposes no architecture.
* `completion_criteria`: the reviewer's stored evaluation is checked deterministically by
  `completion_checker.profile_criteria_problems` as an additional condition; whether a criterion is satisfied stays the
  reviewer's judgement.

## Artifact and report views

* No new table: an artifact is a `task_steps` row (`artifact_id` = step id), the report reads steps, executions,
  approvals and transitions through `ArtifactRepository` (manifests deferred, never loaded). `ReportService` builds a
  payload; `schemas/reports.py` validates it and enforces the size contract. No LLM call, no write.
* `completion_checker.evaluate_definition_of_done` returns every condition with its own verdict and is the single source
  of `check_definition_of_done` (unchanged behaviour); the report's `current_re_evaluation` reuses it without deciding
  anything. The stored COMPLETED transition is reported separately as `historical_completion` (detail UNKNOWN).
* Redaction primitives (`session_fingerprint`, known-id replacement) live in `infra/redaction.py`; providers and the
  response sanitiser both use them (`infra` never imports `providers`; `providers.base.redact` is a thin wrapper).
* Order for each response: payload of correctly typed, sanitised values -> strict validation -> size -> cut only through
  the fields the schema reserves (`sanitization`, `omitted_items`, `omitted_sections`, `size_limited`,
  `SanitizedJson.reasons`) -> strict validation again -> size, at most `RESPONSE_SHRINK_MAX_ROUNDS` times -> bounded
  fallback -> explicit error. `validate_response_limits()` refuses to start with a limit that cannot hold one answer.

## Runtime status and diagnostics

* `runtime_status.runtime_snapshot(lifecycle, startup_outcome, recovery_unsettled, state=None, *, background_degraded=False,
  supervisor_degraded=False, components=())` is the ONE judgement behind `/health`; `state` is the background input bundle
  (`RunnerRuntimeState`: supervisor state, degraded components, sweeper stall and configuration state), and the snapshot
  adds `warnings`, `runner.background_degraded`, `runner.sweeper` and the `SUPERVISOR_DEGRADED` blocker.
  `runner_active` and `ready` come from `Runner.lifecycle`. Why a Runner never started (preflight blocked, advisory lock
  held, `start()` failed) is invisible in the lifecycle, so the lifespan records a `StartupOutcome`. `START_FAILED` is
  reserved: a general `Runner.start` exception still aborts the lifespan (decision D6-a).
* `diagnostics_service` is read-only (version probes of tools and file reads; never an agent, the auth probe, an
  installer or a project test). One shared `asyncio.Task` per cache key, created under a short `asyncio.Lock` that is
  never held while the diagnosis runs; a complete result is cached before the task ends (a partial one, UNKNOWN items, is returned but never cached) and
  the done callback removes only its own task. Waiters are shielded: a cancelled waiter does not cancel the diagnosis.
* A subprocess is bounded by the executor's timeout and terminated. File inspection uses a worker thread and its
  timeout is **cooperative/best-effort**: a thread cannot be killed, so one inspection per project and a global cap
  bound the number of workers, and a slot is released only when the thread really ends. The whole run is bounded by
  `DIAGNOSTICS_DEADLINE_SECONDS` cooperatively (every stage gets the smaller of its timeout and the time left), so a
  blocking filesystem thread may linger.
* Registered projects are validated from their own root (`resolve_project_root`, then `resolve_inside_root`); the
  `WORKSPACE_ROOT`-anchored `WorkspaceGuard` is never used for them. A swapped-in link/junction, a changed canonical
  path, a vanished or inaccessible root are detected; the real directory at the same canonical path being replaced by
  another is not (that would need a stored filesystem identity).

## Lease and takeover (`lease_service`)

One RUNNING `task_runs` row per task (partial unique index). Takeover of an expired lease is three steps:
short txn that reads the old run, **no transaction** while the OS is inspected/terminated, short txn that re-locks
the task, re-validates the same run/version and swaps the run. If the previous CLI cannot be proven gone (access
denied, still alive, identity mismatch, or a `STARTING` execution with no known pid) the takeover is `UNCERTAIN`:
nothing runs and the task parks in a `RECOVERY` gate. An untracked process (an agent CLI call) is identified by
`(pid, creation time)`, never by pid. A **tracked** execution row (a test command run through a Job Object) is judged
differently: by the Job (`JOB_EMPTY`) or by the kill-on-close assumption (owner Runner process proven gone, root gone, no
survivor). A tracked row without a pid is closed `NEVER_RAN` only when it carries no unproven-spawn evidence and no retained
Job of the same Runner process is present (`retained_job_verdict`); otherwise it is `SPAWNED_PROCESS_UNPROVEN`. Details:
`docs/workflow/07-lease.md`.

## Tracked executions and background units

* `TrackedCommandSession` (`step_runner`) registers a tracked `executions` row (`detail.tracked`) before a test command is
  spawned and writes the fenced identity and the closing proof. `process_control.TreeJob` is the Job abstraction and `windows_job.WindowsJob` its Windows Job Object
  (`KILL_ON_JOB_CLOSE`, the process is spawned suspended, assigned, identified, persisted, then resumed); `CommandExecutor`
  retains the Job of a tree that was not proven empty.
* `BackgroundSupervisor` owns the in-process units `OrphanSweeper` (settles unresolved and anomalous tracked rows),
  `ProcessRiskWorker` (verifies the risk options of the `PROJECT_DIRTY` gate) and the capability refresh. Unit failures are
  restarted within a budget; only a degraded supervisor blocks dispatch. PostgreSQL stays the source of truth.
* Migration `0005` adds `runtime_capabilities`, `process_risk_requests`, the partial indexes `ix_executions_tracked_unresolved`
  and `ix_executions_tracked_anomaly`, and `uq_approvals_lineage_successor` (at most one successor per approval).
* Details: `docs/workflow/03`, `07`, `08`, `09`.

## Execution discipline (`step_runner`)

`short txn (verify lease, record STARTING)` -> CLI outside any transaction -> `short txn (verify lease, record)`.
A result that arrives after lease loss is stored only as `discarded_result`; the step becomes `INTERRUPTED`.

## Checkpoints

A SUCCEEDED step is reused only when its `input_hash` is unchanged and `stable_hash(output) == output_hash`.
`WorkspaceSnapshot` (path -> content hash of tracked, modified and untracked files; volatile artifacts such as
`reports/`, caches are separate) is used **only** to decide whether an interrupted step may be re-run. It is a
change detector and cannot restore files. Tester results are reused only for identical requirements, code
fingerprint and test-policy hash; the Code Reviewer only ever sees a test result produced for the current code.

## Human Gate

`DESIGN`, `RECOVERY`, `PERMISSION` are separate. A DESIGN approval never widens CLI permissions. Decisions are
processed under a row lock: an identical earlier decision is returned first (200), a different one conflicts (409);
only a *new* decision is checked for expiry (410) and for a stale basis (409). Decision + RESUME request commit
together. PERMISSION approvals bind to one execution and one exact command fingerprint, are consumed atomically
(`used_at`), and are only honoured when the CLI can actually enforce a one-off grant (Claude `--allowedTools` with
an exact pattern); otherwise execution is held and nothing is widened.

## Sessions

PostgreSQL is the only source of truth: `conversations` (metadata only) and `agent_sessions`
(`UNIQUE(task_id, agent_role)`, five independent roles, `version` for compare-and-set). Message text lives in
`messages` (nothing references it; retention deletes only those rows). Native ids are never returned by the API,
never written to logs/audit events (events carry an irreversible `sess:xxxxxxxx` tag). A session is invalidated
only when the CLI explicitly reports it does not exist; timeouts and transient errors never invalidate one.

## Projects, parallel execution and the write lock

* `projects` sit above tasks (`tasks.project_id`; existing tasks keep `NULL` and behave exactly as before). A project
  workspace may be any absolute path outside `WORKSPACE_ROOT`. It is validated at registration and **before every
  run** (absolute, no symlink/junction in the chain, accessible), and system paths, orchestrator paths and
  duplicate/nested projects are rejected.
* The `Runner` runs tasks in parallel inside ONE process. A free slot is checked before claiming; each claim carries a
  `claim_token` that fences a stale coroutine; backoff is persisted (`execution_requests.defer_count/not_before`). The
  global advisory lock remains the "one Runner process" guard.
* Changing steps (Developer, Tester) and Code Review of a project run only for the task that owns
  `projects.write_owner_task_id`. Order: `claim -> task lease -> project gate -> step attempt -> CLI`. At a terminal
  transition `project_terminal.release_on_terminal` clears the lock and write baseline in the SAME transaction, and
  `workspace_state` is `CLEAN` or `DIRTY_UNRESOLVED` (never `CLEAN` when it cannot be proven). The only way out of
  `DIRTY_UNRESOLVED` is `continue` on the `PROJECT_DIRTY` RECOVERY gate, and `continue` is offered only while the project has
  no unresolved and no anomalous tracked execution (otherwise the risk options of that gate apply, and an unresolvable row
  leaves only `abort`).
* Anything that needs the workspace measured measures a `FingerprintProof` (`WORKSPACE_FINGERPRINT_V1`) outside the
  transaction, then verifies owner and `write_epoch` under the project row lock in a short transaction. A stale proof
  rolls everything back and is measured again (max 3), then the `UNMEASURED` path runs once. No fingerprint is ever
  computed while a row lock is held.
* Test policy presets (pytest / Maven / Gradle) are resolved to ONE command when a task is created and copied into the
  task. Result files are never deleted; only files created or changed by the current run count. Test processes never
  receive the CLI authentication variables.
* Details, diagrams and invariants: `docs/workflow/` (03, 07, 08, 09, 10, 11).

## Task baseline and changes

* `task_baselines` (migration 0004): one row per task, written by `ProjectLockService` in the SAME transaction as `set_owner`
  (`ON CONFLICT (task_id) DO NOTHING`, then the real row is compared by identity: same acquisition = idempotent, different
  = fail closed and the owner rolls back). AVAILABLE rows carry the gzip manifest and HEAD; UNAVAILABLE rows carry a reason and
  NO payload. The CHECKs are NULL-safe (`... IS TRUE`): a CHECK passes on NULL, so a bare conjunction with a NULL term would
  have admitted malformed rows.
* Two results, kept apart (`LockMeasurement`): the fingerprint proof (fail closed, unchanged) and the supplemental manifest
  (failure = UNAVAILABLE row, the task continues). `WORKSPACE_FINGERPRINT_V1` and `STEP_SNAPSHOT_V1` are untouched;
  the manifest is its own `CHANGES_MANIFEST_V1` schema with its own walk, bounds (uncompressed bytes, files, path, target
  length) and bounded decompression.
* `ChangeService` is read-only (git: `rev-parse`, `ls-files`, `status`, `cat-file`). One `Deadline` is passed into the
  measuring code (git gets `min(default, remaining)`, the walk/hash check between entries and chunks): cooperative for the
  filesystem, not a guarantee against a stalled OS call. `MeasurementGate` bounds workers per project and overall and returns
  a slot when the worker ends; a timeout opens a cooldown for `verify_workspace`.
* `lock_consistency` (DB, before/after) and `workspace_consistency` (files, two measurements) are separate fields.

## Decision log

| Decision | Why |
|---|---|
| Prompts via stdin, argv list only | no shell, no argument-length or quoting problems; both CLIs verified to accept it |
| `psutil` added | cross-platform process identity and tree termination (approved at a Human Gate) |
| Embedded PostgreSQL for tests (`pixeltable-pgserver`) | no Docker daemon or PG install on the dev machine (approved at a Human Gate); `pgserver` itself has no Python 3.14 build |
| UTF-8 everywhere | Windows defaults to cp949 |
| Verb URIs replaced by `execution-requests` / `cancellation-requests` / `decisions` | project rule: nouns in URIs |
| Orchestrator, not the Tester, runs the tests | success must come from exit codes and JUnit, never from agent prose |
| Planner output is extracted from the CLI result, never written to a file by the agent | Plan Mode has no write tool at all |

## What is verified, and what is not

Verified with the real installed CLIs (Codex 0.146.0, Claude Code 2.1.218, Windows 11): JSON/JSONL output shapes
and session id locations, `resume` behaviour, the exact "session does not exist" errors, structured output for both
CLIs, `--help` support for every flag used, Plan Mode and `-s read-only` refusing file writes, prompts via stdin.

**Not verified / known limits**

* Codex's JSON event for an approval request is not observed in practice (with `approval_policy="never"` none is
  emitted); the adapter treats any event type containing "approval" as an explicit permission request.
* Read-only enforcement was observed behaviourally (no file appeared). That the Windows Codex sandbox is an OS-level
  barrier was **not** verified; treat it as a CLI policy.
* Claude path rules such as `Edit(tests/**)` are not relied upon: the Orchestrator re-checks the Tester's changes.
* Process termination on Windows is verified for tree kill and identity mismatch in unit tests; behaviour under
  `ACCESS_DENIED` is simulated, not reproduced. Tracked executions (Windows Job Object) are verified by real Job Object tests
  on Windows (`tests/unit/test_windows_job.py`, `tests/integration_pg/test_never_ran_crash_points.py`,
  `tests/integration_pg/test_tracked_services.py`). The POSIX policy exists in code only (an unsupported platform degrades to
  untracked, an unknown or unavailable capability fails closed); POSIX runtime, real Gradle/Maven builds, real CLI end-to-end
  runs, random race stress, EXPLAIN/generic plans and real deadlock tests are not verified.
* Known limits of tracked executions: a Job-less suspended process (CP2) may survive a `NEVER_RAN` close (accepted by
  design); a pid-less `SPAWNED_PROCESS_UNPROVEN` row has no supported resolution path (the gate hides
  `accept_unproven_risk` with `TARGET_WITHOUT_PID` and a direct submit is 409 `OPTION_NOT_AVAILABLE`; `abort` only ends the
  Task and does not clear `DIRTY_UNRESOLVED`; see `docs/workflow/08-human-gate.md`).
* No load testing; one Runner (coordinator) process only, parallelism is per task inside it.
* External edits to a project workspace while one of its tasks is active are not supported and are not fully
  detectable (`write_epoch` only counts orchestrator-controlled changes).
* Real Maven/Gradle builds were **not run** (no JDK/Maven/Gradle here); JUnit parsing, wrapper resolution,
  permissions and environment rules are unit-tested. File symlink tests are skipped where the account cannot create
  symlinks. Cloning a private repository that needs credentials is not verified.
