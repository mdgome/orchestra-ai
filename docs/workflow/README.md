# AgentConductor — Workflow 다이어그램

> 현재 구현 코드를 기준으로 정리한 흐름입니다. 코드로 확인되지 않았거나 검증되지 않은 동작은 현재 구현 제한으로 구분합니다.

## 전체 및 모듈별 다이어그램

- [전체 Workflow](01-overview.md) — 정상 흐름과 예외 흐름(Human Gate·Recovery·Retry·Replan)을 두 다이어그램으로 분리
- [전체 아키텍처 및 모듈 관계](02-architecture.md)
- [Task 생성 및 Runner 실행](03-task-runner.md)
- [Planning 및 Design Review](04-planning-review.md)
- [Development, Testing 및 Code Review](05-development-testing.md)
- [Session Management](06-session.md)
- [Lease 및 Runner Ownership](07-lease.md)
- [Human Gate 및 Approval](08-human-gate.md)
- [Recovery 및 Checkpoint](09-recovery.md)
- [Context Invalidation](10-invalidation.md)
- [Cancellation, Streaming 및 Completion](11-task-lifecycle.md)

각 문서의 다이어그램은 책임 영역 또는 처리 단계별 구역(`subgraph`, `box`)으로 나뉘어 있습니다. 하나로 읽기 어려운 흐름은 여러 다이어그램으로 분리했고, 분리한 다이어그램 사이의 연결은 각 문서 본문에 설명합니다.

## 핵심 정책

Claude Plan Mode → Codex Design Review → (Design Review PASS이면 바로) Claude Development → Claude Testing → Orchestrator Test → Codex Code Review → Completion.

- Workflow는 `Runner`가 `execution_requests`를 claim하여 시작합니다. `TaskService`는 Task 생성과 Execution Request 저장만 합니다.
- Human Gate는 일반적인 "필요 여부 판정 단계"가 없습니다. 다음 경우에만 열립니다.
  - Agent가 `NEEDS_HUMAN_REVIEW` 또는 `human_gate_request`로 명시적으로 요청한 경우 (DESIGN Gate)
  - CLI가 권한을 요청(`PERMISSION_REQUESTED`)한 경우 (PERMISSION Gate)
  - 종료·변경 안전성이 불확실한 Recovery 조건 (RECOVERY Gate)
- 별도의 `RecoveryService`는 없습니다. Recovery 책임은 `Runner`, `WorkflowService`, `HumanGateService`, `LeaseService`, `StepRunner`에 분산되어 있습니다.
- PostgreSQL은 Task / Session / Approval / Checkpoint의 Source of Truth입니다. Redis는 구현되어 있지 않으며 Phase 3 캐시 후보입니다.
- 단일 Runner와 Lease, 안전성 검증 후 재개 정책을 사용합니다.

## 현재 구현 제한 (요약)

각 문서의 「현재 구현 제한」 절에 상세가 있습니다.

- Advisory Lock 연결 상실을 지속 감시하는 코드가 없습니다 ([07](07-lease.md)).
- PERMISSION Approval을 `enforceable=false`로 승인하면 결정만 저장되고 RESUME 요청이 만들어지지 않아 Task가 `WAITING_APPROVAL`에 남으며, 현재 자동 복구 경로가 없습니다 ([08](08-human-gate.md)).
- Session 버전 CAS 충돌(`SessionConflict`) 이후의 자동 복구 경로는 검증되지 않았습니다 ([06](06-session.md)).
- Event Bus는 in-memory best-effort이며 복구 경로는 PostgreSQL REST 조회입니다 ([11](11-task-lifecycle.md)).
- 외부 부수 효과 판정은 실행 로그 stdout/stderr tail의 정규식 검사에 의존합니다 ([09](09-recovery.md)).
