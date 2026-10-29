# Multi-Agent Orchestrator — Common Agent Instructions

## Goal & Scope
Build a local, single-runner FastAPI/Python orchestrator (Phase 1+2) that coordinates Claude Code and Codex CLI. Follow the approved v2.3 implementation plan; implement and verify rather than redesign it.

## Workflow
1. **PLANNER — Claude Code (Plan Mode):** produce a structured implementation plan without changing source files.
2. **DESIGN_REVIEWER — Codex CLI (read-only):** review the stored Plan artifact and its review basis.
3. **Human Gate (when needed):** approve material design trade-offs; approval never expands CLI permissions.
4. **DEVELOPER — Claude Code:** implement only a Plan that passed Design Review.
5. **TESTER — Claude Code:** write/fix tests under limited permissions.
6. **Orchestrator:** run and verify required tests independently.
7. **CODE_REVIEWER — Codex CLI (read-only):** review code and test results.
8. Repeat improvements up to **3** times, subject to separate replan/technical-retry budgets.

Re-review if Plan content or its review basis (requirements, plan, approved decisions, review policy) changes. Never implement against an unreviewed Plan.

## Architecture
- Layered architecture: **Router → Service → concrete Repository / Provider**. Routers handle HTTP and status mapping only.
- Noun-based REST URIs; centralized validation and common error envelope.
- Do **not** add DDD, generic repositories, generic workflow engines, Redis/Celery, Git Worktrees, distributed runners, or OS/container isolation in Phase 1+2.
- Keep abstractions small and explicit; substantial design/maintenance trade-offs require a Human Gate.

## Persistence & Sessions
- **PostgreSQL is the source of truth** for Conversations, Agent Sessions, Workflow, Checkpoints, Approvals and Execution Requests.
- `conversations`: minimal conversation metadata; `messages`: persisted user/system messages, separate from session metadata.
- `agent_sessions`: one current native resume ID per **task + agent role**; five roles are PLANNER, DESIGN_REVIEWER, DEVELOPER, TESTER and CODE_REVIEWER.
- Native CLI sessions are supplementary context, **not** authoritative state or permission. Resume only after Task/Conversation, Lease, Approval, Checkpoint and role/provider validation. Never expose native IDs in API responses or logs.
- `SessionStore` is a narrow protocol; PostgreSQL implementation only in Phase 1+2. Redis may be added later as a **read cache**, not as source of truth.
- Services own database transactions; repositories do not commit independently. Never hold a DB transaction while running a CLI command.

## Execution, Safety & Recovery
- Persist execution requests in PostgreSQL; one local Runner consumes them and acquires a Task Lease.
- Keep the session-level PostgreSQL advisory lock on a dedicated connection for the Runner lifetime. On loss of ownership, stop initiating CLI work and apply recovery rules.
- Checkpoints use input/output hashes; WorkspaceSnapshots detect changes and guard interrupted-step reruns. A snapshot hash is **not** a backup.
- If process termination, external side effects or workspace safety is uncertain, **do not auto-rerun**: enter RECOVERY Human Gate.
- CLI permissions are role-specific and validated by real preflight probes. Do not infer a permission request from timeouts or idle output.
- Completion requires independently executed tests; failure, zero collected tests or invalid results cannot pass.

## Delivery
- Follow the latest *Final Implementation* rules where older plan text conflicts.
- Report actually executed tests and remaining unverified assumptions. Do not claim Windows/CLI behavior was tested unless it was.
