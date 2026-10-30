# Human Gate 및 Approval

> 현재 구현 코드를 기준으로 정리한 흐름입니다. 코드로 확인되지 않았거나 검증되지 않은 동작은 현재 구현 제한으로 구분합니다.

`approvals.type`은 `DESIGN`, `PERMISSION`, `RECOVERY` 세 가지이며 서로의 효력을 갖지 않습니다. 특히 DESIGN 승인은 CLI 권한을 확대하지 않습니다.

이 문서는 네 개의 다이어그램으로 구성됩니다. 1번은 세 유형이 공유하는 Gate 진입과 Decision API 처리이고, 2~4번은 공통 Decision API를 통과한 뒤 수행되는 유형별 처리입니다. 중심 클래스는 `HumanGateService`(`open_gate`, `open_recovery`, `decide`, `_apply`)입니다.

## 1. 공통: Gate 진입 및 결정 저장

정상 Gate와 RECOVERY Gate의 진입, `POST /approvals/{approval_id}/decisions`의 멱등·만료·stale 검사, 결정과 RESUME 요청의 원자적 저장을 보여줍니다.

```mermaid
flowchart TD
    subgraph ENTRY["Gate 진입"]
        direction TB
        A["Agent 반환 또는 CLI 권한 요청 또는 Recovery 조건"] --> B{"CLI 호출이 반환되었고 종료가 입증됨?"}
        B -->|"Yes - 정상 Gate"| C["한 트랜잭션: Approval PENDING 생성 + Step 및 Task WAITING_APPROVAL + Lease 반환 Run FINISHED"]
        B -->|"No"| REC["RECOVERY Gate - 기존 Run 과 Lease 를 정상 종료로 간주하지 않음"]
        C --> W["WAITING_APPROVAL - Run 과 Heartbeat 없음"]
        REC --> W
    end

    subgraph API["Decision API"]
        direction TB
        D["POST /approvals/id/decisions - Idempotency-Key 필수"] --> E["Approval 행 잠금"]
    end

    subgraph EXISTING["기존 Decision 비교"]
        direction TB
        F{"이미 저장된 결정이 있음?"}
        F -->|"Yes - 같은 요청"| OK200["200 OK - 기존 결과"]
        F -->|"Yes - 다른 결정 또는 옵션"| C409["409 - 같은 Idempotency-Key 이면 IDEMPOTENCY_KEY_CONFLICT"]
    end

    subgraph NEWCHK["신규 Decision 의 만료 및 stale 검사"]
        direction TB
        G{"valid_until 만료?"}
        G -->|"Yes"| E410["410 Gone"]
        G -->|"No"| H{"Approval basis 와 현재 basis 일치?"}
        H -->|"No"| S409["409 STALE_BASIS"]
    end

    subgraph ATOMIC["Decision 과 RESUME Request 의 원자적 저장"]
        direction TB
        I["한 트랜잭션: 결정 저장 + 유형별 효과 + 필요 시 RESUME Execution Request 생성"] --> CR["201 Created"]
    end

    W --> D
    E --> F
    F -->|"No - 신규 결정"| G
    H -->|"Yes"| I
    CR --> TYPE["유형별 처리 - 다이어그램 2 DESIGN, 3 PERMISSION, 4 RECOVERY"]
```

정상 Gate는 CLI 호출이 반환된 뒤 Approval 생성, Step/Task 상태 변경, Lease 반환이 한 트랜잭션(`WorkflowService._enter_gate`, `StepRunner.complete`의 mutate)에서 처리됩니다. 결정과 RESUME Execution Request(`approval_id` UNIQUE)도 하나의 PostgreSQL 트랜잭션에서 저장됩니다. 승인 직후 프로세스가 종료되어도 요청이 남아 다음 Runner가 소비합니다.

응답 코드는 다음과 같습니다.

| 상황 | 응답 |
|---|---|
| 신규 Decision 저장 | 201 Created |
| 동일 Decision의 멱등 재요청 | 200 OK |
| 다른 Decision 또는 충돌 | 409 |
| 만료된 신규 Decision | 410 |
| stale basis | 409 (`STALE_BASIS`) |

기존 결정과의 비교가 만료·stale 검사보다 먼저 수행되므로 이미 저장된 결정에 대한 재요청은 만료 뒤에도 200으로 응답합니다.

## 2. DESIGN Gate

공통 Decision API를 통과한 뒤의 DESIGN 전용 처리입니다. 승인과 거절은 효과가 같으며 모두 설계 결정으로 저장되고 checkpoint 단계부터 재개됩니다.

```mermaid
flowchart TD
    subgraph POLICY["DESIGN 정책"]
        direction TB
        A["Agent 가 human_gate_request 또는 NEEDS_HUMAN_REVIEW 반환"] --> B["DESIGN Gate - checkpoint step 기록"]
        B --> D["승인 또는 거절 모두: design_decisions 저장 + decisions_hash 갱신 + RESUME 요청 생성"]
    end

    subgraph RESUMEZ["checkpoint 기반 재개"]
        direction TB
        E["Runner 가 RESUME 소비 - 재개 시 Lease 검증"] --> F{"checkpoint step"}
        F -->|"PLAN"| G["plan_revision 증가 후 PLANNING"]
        F -->|"DEVELOP"| H["DEVELOPING"]
        F -->|"TEST"| I["TESTING"]
        G --> J["Plan 이 바뀌면 Design Review 재수행"]
        H --> K{"decisions_hash 변경으로 Basis 가 달라짐?"}
        K -->|"Yes"| L["Development 직전 비교에서 DESIGN_REVIEWING 으로 복귀"]
        K -->|"No"| M["Developer 실행"]
    end

    D --> E
```

- DESIGN 거절은 현재 구현에서 즉시 FAILED가 아닙니다. 거절도 설계 결정으로 기록되고 RESUME 요청이 생성되어 checkpoint 단계부터 재개됩니다.
- Design Reviewer가 `NEEDS_HUMAN_REVIEW`를 반환한 경우 checkpoint는 `PLAN`이며 PLANNING으로 재개됩니다. Developer·Tester·Code Reviewer가 요청한 Gate는 각각 `DEVELOP`/`TEST`/`DEVELOP` checkpoint를 가집니다.
- Code Reviewer가 `human_gate_request` 없이 `NEEDS_HUMAN_REVIEW`를 반환하면 Orchestrator가 reviewer의 summary와 issues로 요청을 구성합니다 (`accept` / `revise` 옵션).
- 결정이 `decisions_hash`를 바꾸므로 이후 Development 직전 Basis 비교에서 기존 PASS는 무효가 될 수 있습니다 ([04](04-planning-review.md)).

## 3. PERMISSION Gate

공통 Decision API를 통과한 뒤의 PERMISSION 전용 처리입니다. 승인은 해당 Execution과 명령에만 결속되며, `enforceable` 여부에 따라 재개되거나 `WAITING_APPROVAL`에 남습니다.

```mermaid
flowchart TD
    subgraph BIND["Execution 및 command 결속"]
        direction TB
        A["CLI 가 권한 요청을 명시 보고 - PERMISSION_REQUESTED"] --> B["Gate 생성: execution_id 와 command_fingerprint 결속 - enforceable 여부 기록"]
    end

    subgraph DECISION["enforceable 검증"]
        direction TB
        D{"결정"}
        D -->|"승인 이고 enforceable"| R["RESUME 요청 생성"]
    end

    subgraph SPAWN["used_at 원자적 소진"]
        direction TB
        S["재개 - 같은 Step 을 one-off 도구로 재실행"] --> U{"spawn 직전 검증: 결정 승인 및 command fingerprint 일치 및 미사용 및 만료 전 및 같은 task 및 enforceable"}
        U -->|"Yes"| V["used_at 원자적 소진 - rowcount 1 일 때만 실행"]
    end

    subgraph OUTCOME["Resume, FAILED 또는 WAITING_APPROVAL 잔류"]
        direction TB
        F(["Task FAILED - PERMISSION_REJECTED"])
        H(["WAITING_APPROVAL 잔류 - RESUME 요청 없음"])
        X(["FailClosed - 실행 안 함"])
    end

    B --> D
    D -->|"거절"| F
    D -->|"승인 이고 enforceable 아님"| H
    R --> S
    U -->|"No"| X
```

- TIMEOUT, IDLE, ERROR는 권한 요청으로 추정하지 않습니다. `PERMISSION_REQUESTED`일 때만 이 Gate가 만들어집니다.
- 승인은 해당 Execution과 명령에만 결속되고 한 번만 소진됩니다. 소진 후 CLI가 실패해도 재사용하지 않으며 새 Gate가 필요합니다.
- 설치된 CLI가 해당 명령 1건을 강제할 수 없으면(`enforceable` 거짓, 예: Codex 역할) 승인되어도 권한을 확대하지 않습니다. 이때 실제 상태는 다음과 같습니다.
  - Decision은 저장됩니다.
  - Approval은 `DECIDED`가 됩니다.
  - RESUME Execution Request는 생성되지 않습니다.
  - Task는 `WAITING_APPROVAL`에 남습니다.
  - 현재 이 상태에서 빠져나오는 자동 복구 경로가 없습니다 (현재 구현 제한).

## 4. RECOVERY Gate

공통 Decision API를 통과한 뒤의 RECOVERY 전용 처리입니다. `abort` 승인과 거절은 FAILED로 종료되고, `continue`/`retry`만 RESUME 후 안전성을 다시 검증합니다. Recovery 진입 조건은 [09](09-recovery.md)에 있습니다.

```mermaid
flowchart TD
    subgraph ENTRY["RECOVERY Gate 생성"]
        direction TB
        A["Recovery 조건 - 09 문서 참조"] --> B["HumanGateService.open_recovery - 한 트랜잭션: Task WAITING_APPROVAL + Approval RECOVERY + Step NEEDS_MANUAL_RECOVERY"]
    end

    subgraph CHOICE["continue, retry, abort, 거절"]
        direction TB
        D{"결정 및 selected_option"}
        D -->|"승인 이고 continue 또는 retry"| R["RESUME 요청 생성"]
    end

    subgraph REVERIFY["승인 후 _recovery_unsafe"]
        direction TB
        S["Runner 소비 및 Lease 획득"] --> T["WorkflowService._recovery_unsafe 로 재검증"]
        T --> U{"안전?"}
    end

    subgraph OUTCOME["Resume, 새 Recovery Gate 또는 FAILED"]
        direction TB
        F(["Task FAILED - RECOVERY_ABORTED"])
        F2(["Task FAILED - recovery_rejected"])
        N["새 RECOVERY Gate - 신규 실행 없음"]
        V["checkpoint step 부터 재개"]
    end

    B --> D
    D -->|"승인 이고 abort"| F
    D -->|"거절"| F2
    R --> S
    U -->|"workspace 없음 또는 미종료 Execution 존재 또는 retry 인데 snapshot_before 와 다름"| N
    U -->|"Yes"| V
```

- RECOVERY 승인만으로 기존 Run과 Lease가 정상 종료된 것으로 간주되지 않습니다.
- 재개 시 안전성은 다시 검증됩니다. 미종료 Execution이 남아 있으면 승인 후에도 신규 실행을 차단하고 새 RECOVERY Gate를 만듭니다.
- Recovery 진입 경로별로 Run/Lease 처리가 다릅니다 ([09](09-recovery.md)).

## 유형별 결정 결과 비교

| 유형 | 승인 | 거절 | 비고 |
|---|---|---|---|
| DESIGN | 결정 저장 + RESUME | 결정 저장 + RESUME (FAILED 아님) | checkpoint 단계부터 재개 |
| PERMISSION | `enforceable`이면 RESUME, 아니면 결정만 저장하고 `WAITING_APPROVAL` 잔류 | Task FAILED (`PERMISSION_REJECTED`) | 승인은 1회 소진 |
| RECOVERY | `continue`/`retry`는 RESUME 후 `_recovery_unsafe`, `abort`는 FAILED (`RECOVERY_ABORTED`) | Task FAILED (`recovery_rejected`) | 재개 전 안전성 재검증 |

## 현재 구현 제한

- PERMISSION의 일회성 권한은 Claude 역할의 `--allowedTools` 정확 패턴으로만 강제됩니다. Codex 역할은 강제 수단이 없어 `enforceable=false`로 기록되며, 승인해도 RESUME이 생성되지 않고 Task가 `WAITING_APPROVAL`에 남습니다. 이 상태에서 자동으로 복구하는 경로는 없습니다.
- Codex 승인 이벤트의 실제 형태는 실측하지 못했습니다. `approval`이 이름에 들어간 이벤트를 권한 요청으로 취급하는 추정 구현입니다.
