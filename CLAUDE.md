# Claude Code — Project-Specific Instructions

Follow `AGENTS.md` for common architecture, persistence, workflow and recovery rules. This file only adds **Claude-specific** behavior.

## PLANNER — Plan Mode
- Run with **Plan Mode** (`--permission-mode plan`, after confirming support on the installed CLI). Allow project inspection/reading; block source, test and configuration writes.
- Produce a **structured Plan artifact** containing: `requirements`, `design_direction`, `changed_files`, `implementation_steps`, `test_plan`, `acceptance_criteria`, `tradeoffs`.
- Return the Plan to the Orchestrator; **do not write the Plan artifact into the workspace yourself**. The Orchestrator validates, stores and hashes it.
- Codex **DESIGN_REVIEWER** must return PASS against the current review basis before implementation starts.
- On NEEDS_REVISION, resume the Planner's own native session when valid; incorporate Codex feedback and submit a new Plan for re-review. Respect `max_replans`.
- If a material design trade-off needs user choice, request a **DESIGN Human Gate**; never silently settle it.

## DEVELOPER — Implementation
- Use a **separate native session** from PLANNER; obtain its resume ID via the PostgreSQL-backed Session Service.
- Implement only the reviewed Plan and approved decisions. If requirements, Plan hash or review basis changed, stop and request re-review.
- Keep changes within the approved scope and existing Layered Architecture; no DDD/generic repository or unnecessary framework additions.
- If unexpected complexity or permissions are required, request the appropriate Human Gate instead of bypassing restrictions.

## TESTER — Testing
- Use an independent **TESTER** native session and limited editing/execution permissions.
- Add and repair meaningful tests without weakening assertions or bypassing mandated tests.
- The Orchestrator, not Claude, determines completion by independently running required tests and examining results.

## Session, Permission & Recovery Rules
- Native session IDs belong to their **task, provider and role**; never reuse Planner's session for Developer, or one role's session for another.
- The PostgreSQL Checkpoint, Approval and Lease state overrides anything remembered by a native CLI session.
- If a resume attempt clearly reports the session is missing, follow the controlled new-session path using stored shared context. For timeout or uncertain execution state, **stop and use RECOVERY**, not a blind retry.
- Do not expose native session IDs, tokens or secrets in artifacts, API responses or logs.
- `--tools`, `--allowedTools` and `--disallowedTools` mean different things: verify exact behavior with the installed Claude CLI preflight before execution.

## Final Checklist
- Planner made **no workspace code changes**.
- Codex approved the **exact current Plan and review basis**.
- Developer and Tester used their **own sessions and permitted tools**.
- Required tests ran independently and results were reported accurately.
