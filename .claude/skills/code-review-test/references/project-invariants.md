# 프로젝트 빠른 확인 reference

- 현재 구현을 빠르게 검토하기 위한 **비규범** reference이며 영구 설계 규칙이 아니다.
- 관련 코드가 변경되면 실제 구현과 테스트를 다시 확인한다.
- reference와 코드가 다르면 코드와 명시된 요구사항을 우선한다. 불일치를 발견하면 이번 요청에 Skill reference 수정 권한이 포함되는지 확인하고, 없으면 임의로 수정하지 말고 불일치와 필요한 갱신을 보고한다.
- 일회성 예외나 확인되지 않은 추정을 불변 조건으로 추가하지 않는다.

## 구현 위치
- 상태 enum: `app/models/states.py`
- DB table: `app/models/tables.py`
- API schema: `app/schemas/api.py`
- Router: `app/routers`
- Runner와 recovery: `app/services/runner.py`
- Workflow: `app/services/workflow_service.py`
- Step 실행: `app/services/step_runner.py`
- Lease: `app/services/lease_service.py`
- Project lock: `app/services/project_lock_service.py`
- Project terminal 처리: `app/services/project_terminal.py`
- Session: `app/services/session_service.py`
- Snapshot과 fingerprint: `app/infra/workspace_snapshot.py`, `app/infra/workspace_fingerprint.py`
- Process 실행과 취소: `app/infra/command_executor.py`, `app/infra/process_control.py`
- 기존 테스트: `tests/unit`, `tests/architecture`, `tests/api`, `tests/integration_pg`, `tests/e2e_cli`

## 실제 코드에서 재확인할 불변 조건
- terminal Task를 재실행하지 않는다.
- claim token 없는 일반 finish 또는 requeue를 허용하지 않는다.
- stale owner는 lease 검증 이후 상태를 기록하지 못한다.
- 동일 Project의 변경 작업은 직렬화한다.
- Task terminal transaction과 ExecutionRequest finish는 별도 경계일 수 있다.
- startup recovery와 정상 periodic recovery의 의미가 다르다.
- Snapshot 실패는 재사용 가능 성공으로 기록하지 않는다.
- 종료된 CLI 결과는 후속 Snapshot 실패로 미종료 상태가 되지 않는다.
- 서로 다른 목적의 fingerprint를 교차 비교하지 않는다.
- cancellation 이후 permit과 process가 남지 않아야 한다.
- 사용자 입력의 생략과 명시적 null은 다른 의미일 수 있다.
