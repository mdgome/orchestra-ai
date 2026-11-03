# AgentConductor — Workflow Diagrams

> 한국어: [README_ko.md](README_ko.md)

> This is a flow organized based on the current implementation code. Behaviors that could not be confirmed in code or have not been verified are distinguished as current implementation limitations.

## Overall and Module-Level Diagrams

- [Overall Workflow](01-overview.md) — Splits the normal flow and exception flow (Human Gate, Recovery, Retry, Replan) into two diagrams
- [Overall Architecture and Module Relationships](02-architecture.md)
- [Task Creation and Runner Execution](03-task-runner.md)
- [Planning and Design Review](04-planning-review.md)
- [Development, Testing, and Code Review](05-development-testing.md)
- [Session Management](06-session.md)
- [Lease and Runner Ownership](07-lease.md)
- [Human Gate and Approval](08-human-gate.md)
- [Recovery and Checkpoint](09-recovery.md)
- [Context Invalidation](10-invalidation.md)
- [Cancellation, Streaming, and Completion](11-task-lifecycle.md)

The diagrams in each document are divided into sections (`subgraph`, `box`) by area of responsibility or processing stage. Flows that were difficult to read as a single diagram have been split into multiple diagrams, and the connections between the split diagrams are described in the body of each document.

## Core Policies

Claude Plan Mode → Codex Design Review → (if Design Review PASS, immediately) Claude Development → Claude Testing → Orchestrator Test → Codex Code Review → Completion.

- The Workflow starts when a `Runner` claims `execution_requests`. `TaskService` only handles Task creation and Execution Request storage.
- Human Gate does not have a general "determine whether needed" step. It opens only in the following cases:
  - When an Agent explicitly requests it via `NEEDS_HUMAN_REVIEW` or `human_gate_request` (DESIGN Gate)
  - When the CLI requests permission (`PERMISSION_REQUESTED`) (PERMISSION Gate)
  - Recovery conditions where termination/change safety is uncertain (RECOVERY Gate)
- There is no separate `RecoveryService`. Recovery responsibility is distributed across `Runner`, `WorkflowService`, `HumanGateService`, `LeaseService`, and `StepRunner`.
- PostgreSQL is the Source of Truth for Task / Session / Approval / Checkpoint. Redis is not implemented and is a Phase 3 cache candidate.
- Multiple Tasks run in parallel within a single Runner (Coordinator) process (`MAX_CONCURRENT_TASKS`, per-project `MAX_TASKS_PER_PROJECT`). It uses Lease, claim fencing, and a resume policy after safety verification ([03](03-task-runner.md), [07](07-lease.md)).
- **Project**: An existing local Workspace or a cloned repository is registered via `POST /projects`, and Tasks are created via `POST /projects/{id}/tasks`. Mutating Steps for the same Project are serialized by a single Project write lock, while other Projects and existing Tasks without a `project_id` run in parallel. If unresolved changes remain, a RECOVERY Gate (`PROJECT_DIRTY`) opens ([07](07-lease.md), [08](08-human-gate.md), [09](09-recovery.md)).
- The Orchestrator owns the Test Policy and resolves pytest / Maven / Gradle presets into a single command at Task creation time ([05](05-development-testing.md)).

## Current Implementation Limitations (Summary)

Details are in the "Current Implementation Limitations" section of each document.

- There is no code that continuously monitors for loss of the Advisory Lock connection ([07](07-lease.md)).
- If a PERMISSION Approval is approved with `enforceable=false`, only the decision is stored and no RESUME request is created, so the Task remains in `WAITING_APPROVAL`; there is currently no automatic recovery path ([08](08-human-gate.md)).
- The automatic recovery path after a Session version CAS conflict (`SessionConflict`) has not been verified ([06](06-session.md)).
- The Event Bus is in-memory best-effort, and the recovery path is a PostgreSQL REST query ([11](11-task-lifecycle.md)).
- Determination of external side effects relies on regex checks of the execution log stdout/stderr tail ([09](09-recovery.md)).
- Modification by external processes while an active Project Task exists is not supported, and complete detection is not guaranteed even via fingerprint. `write_epoch` is merely an internal generation number for the Orchestrator ([09](09-recovery.md), [10](10-invalidation.md)).
- A `DIRTY_UNRESOLVED` Project is released only via `continue` on the PROJECT_DIRTY Gate ([08](08-human-gate.md)).
- Actual Maven/Gradle builds and Windows file symlink paths could not be executed/verified in this environment ([05](05-development-testing.md), [10](10-invalidation.md)).
