# Architecture and decision log

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

## Lease and takeover (`lease_service`)

One RUNNING `task_runs` row per task (partial unique index). Takeover of an expired lease is three steps:
short txn that reads the old run, **no transaction** while the OS is inspected/terminated, short txn that re-locks
the task, re-validates the same run/version and swaps the run. If the previous CLI cannot be proven gone (access
denied, still alive, identity mismatch, or a `STARTING` execution with no known pid) the takeover is `UNCERTAIN`:
nothing runs and the task parks in a `RECOVERY` gate. A process is identified by `(pid, creation time)`, never by pid.

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
  `ACCESS_DENIED` is simulated, not reproduced.
* No load testing; one Runner only.
