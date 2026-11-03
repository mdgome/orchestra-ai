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
anything blocking is found.

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
| GET/POST | `/tasks` | POST only **creates** a PENDING task (201); it does not execute anything |
| GET/PATCH | `/tasks/{id}` | The GET response includes the current lease, latest checkpoint, and any pending approvals |
| POST | `/tasks/{id}/execution-requests` | 202. Stores the request; the Runner executes it. Idempotent while a request is pending |
| POST | `/tasks/{id}/cancellation-requests` | 202. Records the request; marked CANCELLED only after CLI process termination is confirmed |
| GET | `/tasks/{id}/executions` · `/state-transitions` · `/approvals` | History |
| GET | `/approvals/{id}` | |
| POST | `/approvals/{id}/decisions` | `Idempotency-Key` header required. New = 201, same request replayed = 200, conflict/expired data = 409, deadline expired = 410 |
| WS | `/tasks/{id}/event-stream` | Real-time events |

Approving a decision stores the decision content and the `RESUME` execution request in a single transaction. So even
if a failure occurs right after approval, the work resumes the next time the Runner starts.

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
  `PROJECT_DIRTY` RECOVERY Gate opens when the next Task starts a change Step. `continue` accepts the current state
  as the new baseline, and `abort` fails the Task (without touching any files).

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
