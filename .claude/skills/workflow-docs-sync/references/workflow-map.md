# Workflow 문서 맵

영향받는 `docs/workflow` 문서를 선택할 때 읽는다. 모든 문서를 항상 수정하지 않는다. 직접 영향 문서와, 흐름 연결이 달라지는 상위 문서만 수정한다.
문서 구조, 책임 또는 아래 매핑이 실제로 달라졌을 때만 이 파일을 갱신한다.

## 문서 책임
- `README.md`: 문서 목차와 탐색, 전체 Workflow의 공통 정책, 여러 문서에 공통으로 적용되는 구현 제한, Agent 역할이나 전체 단계 연결 변경. 링크 변경에 한정하지 않고, 모든 작업에서 수정하지도 않는다.
- `01-overview.md`: 전체 정상·예외 Workflow
- `02-architecture.md`: Layer와 Component 책임
- `03-task-runner.md`: Task, Execution Request, Runner, Lease
- `04-planning-review.md`: Plan, Design Review, Basis
- `05-development-testing.md`: Development, Test, Review, Retry
- `06-session.md`: Native Session과 CAS
- `07-lease.md`: Advisory Lock과 Task Lease
- `08-human-gate.md`: DESIGN, PERMISSION, RECOVERY
- `09-recovery.md`: 중단·종료·복구 안전성
- `10-invalidation.md`: Fingerprint와 Checkpoint 재사용
- `11-task-lifecycle.md`: Cancellation, Event, Completion

## 변경 유형별 영향 문서
| 변경 유형 | 확인할 문서 |
|---|---|
| 전체 Workflow 순서 | README, 01, 관련 단계 문서 |
| Component·Layer 책임 이동 | 02, 해당 상세 문서 |
| Runner, Execution Request | 01, 03 |
| Planning, Design Review, Basis | 01, 04 |
| Development, Test, Review, Retry | 01, 05 |
| Test Policy, Completion 조건 | 05, 11, 필요 시 10 |
| Session 생성·Resume·CAS | 06, 필요 시 01·02 |
| Lease, Heartbeat, 인계 | 03, 07, 필요 시 09 |
| Human Gate, Approval | 01, 08, 관련 Checkpoint 문서 |
| Recovery | 01, 07, 08, 09 |
| Fingerprint, Checkpoint 재사용 | 05, 09, 10 |
| Cancellation, Event, Completion | 11, 필요 시 01 |
| Agent 역할, Provider 책임 | README, 02, 관련 단계 문서 |
| API URI, 상태 코드, WebSocket | 관련 상세 문서, README |
| 구현 제한이 생기거나 해소됨 | 해당 문서의 「현재 구현 제한」, README 요약 |

표에 없는 변경 유형은 변경된 코드가 실행되는 단계의 문서를 기준으로 판단하고, 그 문서와 연결된 상위 문서를 함께 확인한다.
