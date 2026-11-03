# Lease and Runner Ownership

> 한국어: [07-lease_ko.md](07-lease_ko.md)

> This flow is organized based on the current implementation code. Behavior that is not confirmed by code or not verified is marked as a current implementation limitation.

Three protective mechanisms prevent different problems.

- Advisory Lock (process level): A PostgreSQL session advisory lock (`pg_try_advisory_lock`) prevents duplicate execution of the Runner (Coordinator) process. A second Runner process cannot start. Within one process, multiple Tasks run in parallel ([03](03-task-runner.md)).
- Task Lease (Task level): A Lease in `task_runs` (a partial unique index limiting one RUNNING Run per Task) and row locking on `tasks` guarantee ownership of the RUNNING Run per Task. The execution request's `claim_token` is also checked at this stage (`FENCED`).
- Project write lock (Project level): Among Tasks sharing the same Workspace, limits the Task executing change Steps to only one (see "Ordering with the Project write lock" below).

The diagram below shows the flow from Runner startup, through Lease acquisition (including takeover of an expired Lease), Heartbeat, and result commit or Lease loss. The normal flow and the takeover/loss exception flows are shown together, and the central class is `LeaseService`. OS process verification and termination are performed by the `app.infra.process_control` module.

```mermaid
sequenceDiagram
    autonumber

    box Runner and Workflow
        participant R as Runner
        participant WF as WorkflowService
    end

    box Lease and Persistence
        participant LS as LeaseService
        participant DB as PostgreSQL
    end

    box OS Process Control
        participant PC as process_control
    end

    box CLI Provider
        participant CLI as ClaudeCliProvider / CodexCliProvider
    end

    Note over R,DB: 1. Acquire Advisory Lock - prevent duplicate Runner process
    R->>DB: pg_try_advisory_lock on the Runner-dedicated connection
    alt Acquisition failed
        R-->>R: RunnerAlreadyActive - Runner does not start
    else Acquisition succeeded
        R->>R: start loop after recover()
    end

    Note over R,DB: 2. Txn A - Task Lease lookup
    R->>LS: acquire(task_id, fence = request id + claim_token)
    LS->>DB: tasks FOR UPDATE and current RUNNING Run lookup
    alt claim_token mismatch - request was requeued or reclaimed
        LS-->>R: FENCED - neither creates nor takes over a Lease - same check in Txn B
    else No RUNNING Run
        LS->>DB: Create new RUNNING Run
        LS-->>R: ACQUIRED
    else Lease not expired
        LS-->>R: HELD
    else Lease expired
        LS->>DB: Collect Run id, row_version, and unfinished Executions, then commit - release lock

        Note over LS,PC: 3. OS process check - outside the transaction
        LS->>PC: Verify process identity and terminate tree (terminate_tree)
        PC-->>LS: TerminationResult

        alt All termination proven
            Note over LS,DB: 4. Txn B - revalidate then take over
            LS->>DB: Lock, then revalidate same Run id, row_version, and expiry
            alt Revalidation passed
                LS->>DB: Mark previous Run ABANDONED and create new RUNNING Run
                LS-->>R: ACQUIRED
            else Revalidation failed
                LS-->>R: HELD - takeover raced
            end
        else Termination could not be proven
            LS-->>R: UNCERTAIN
            R->>DB: Runner._park_uncertain - create RECOVERY Gate - no new execution
        end
    end

    Note over R,CLI: 5. Heartbeat and execution
    opt ACQUIRED
        R->>WF: run_task
        WF->>CLI: Execute Agent Step (via StepRunner)
        loop Every 1/3 of TTL
            R->>LS: heartbeat
            LS->>DB: Heartbeat and ownership check
        end

        Note over WF,DB: 6. Commit result or Lease loss
        WF->>DB: Verify Lease before committing Step result
        alt Lease lost
            R->>CLI: on_lost - cancel the CLI process for that run
            WF->>DB: Record Step INTERRUPTED and discarded_result only - no Task transition
        else Lease retained
            WF->>DB: Commit Step result
        end
    end
    Note over R,DB: Runner.stop - after drain, remaining Runs are locally blocked via LeaseService.revoke and CLI cancel - DB Lease is left to expire
    Note over R,DB: advisory unlock on Runner.stop
    R->>DB: pg_advisory_unlock

    Note over R,DB: Current implementation limitation - Advisory Lock connection loss is not continuously monitored
```

## How to read this

- Lease acquisition has 3 stages. Txn A checks and commits the existing Run. Afterward, outside the transaction, the process is checked and terminated. Txn B only takes over after revalidating the same Run id, `row_version`, and expiry status. No row lock is held during the OS operation.
- If termination cannot be proven (including a `STARTING` Execution whose `pid` or creation time was not recorded), it returns `UNCERTAIN` and starts nothing. The RECOVERY Gate is opened by `Runner._park_uncertain`, not by `LeaseService` ([09](09-recovery.md)).
- If the Heartbeat's `rowcount` is 0 or the update fails, it is treated as Lease loss and the CLI process for that Run is canceled. A result arriving afterward is not recorded as a normal completion; `StepRunner._discard_result` records it only as `discarded_result`. `run_task` returns `LEASE_LOST` and `Runner.process` requeues the request ([09](09-recovery.md)).
- When normally entering a Human Gate, Approval creation and Lease return (`FINISHED`) happen in the same transaction after the CLI returns ([08](08-human-gate.md)).
- `process_control` is not a class but the `app.infra.process_control` module, composed of `ProcessIdentity`, `TerminationResult`, `capture_identity`, `check_alive`, and `terminate_tree`.
- `LeaseService.revoke(run_id)` only marks that Run as "lost" in this process's memory. Afterward, `verify` throws `LeaseLost`, preventing the in-progress Run from recording anything, and leaves the DB's Lease to expire. `Runner.stop` uses this for Tasks that have not finished even after the grace period; the next startup's `acquire` either proves process termination or opens a RECOVERY Gate with `UNCERTAIN`.

## Ordering with the Project write lock

A Task belonging to a project additionally carries a Project write lock (`projects.write_owner_task_id`) on top of the Task Lease. The order is always as follows.

`claim(claim_token)` → `Task Lease` → `[Project gate before Step start]` → `Step attempt` → `CLI / Workspace command`

- The Project gate (`ProjectLockService.ensure_for_step`) runs before change Steps (`DEVELOPER`, `TESTER`) and Code Review. If there is no owner, it acquires the lock; if this Task is already the owner, it re-enters (keeping the existing write baseline); if another Task is the owner, it is `BLOCKED` (Lease returned, then delayed requeue). Waiting does not consume `step_attempt`, `cli_invocations`, or `workflow_iteration`.
- The release order is the reverse. The Project lock is released in the **same transaction as the Task's terminal transition** (`project_terminal.release_on_terminal`), and the Lease is returned by the existing path. The Lease logic itself is unchanged.
- Upon entering `WAITING_APPROVAL`, the CLI and Task Lease are returned, but the Project write lock is retained, because there may be Workspace changes that have not yet been cleaned up ([08](08-human-gate.md), [11](11-task-lifecycle.md)).
- Even if the owning Task's Lease expires, the Project lock is not automatically released. Uncertain executions are judged through the existing `UNCERTAIN` → RECOVERY path.

## Current implementation limitations

- The Advisory Lock is acquired once in `Runner.start` and released in `Runner.stop`, but there is no code that continuously monitors whether the connection has dropped. Logic to block new CLI execution upon connection loss is not implemented. (Task-level duplicate execution continues to be blocked by the Lease and the partial unique index.)
- The process termination target is identified only by `(pid, create_time)` (psutil). `runner_id` and the execution command hash are not used in the actual termination decision.

## Future improvements

- Monitor Advisory Lock connection loss and block new CLI execution upon loss.
- Include `runner_id` and execution command hash verification in process identity.
