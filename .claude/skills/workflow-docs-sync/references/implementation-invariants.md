# 현재 구현 불변 조건과 알려진 제한

이 문서는 현재 구현을 빠르게 확인하기 위한 기준이며 영구적인 설계 규칙이 아니다. 관련 코드가 바뀌면 기존 항목을 그대로 적용하지 말고 실제 구현을 다시 확인한다. 코드와 다르면 코드가 최종 기준이다.
항목이 틀렸을 때만 갱신한다. 표현 선호나 한 작업에만 필요한 예외는 누적하지 않는다. 제한이 해소되거나 새로 생기면 이 파일과 관련 `docs/workflow` 문서를 함께 검토한다.

## 심볼 기록과 확인
- 심볼은 파일 경로와 이름으로만 기록하고 고정 line number는 저장하지 않는다.
- 사용 전 확인: 파일 존재, 클래스·함수 이름, `async def` 여부, 실제 호출자와 책임, 관련 테스트.
- 검색은 `async def`와 `def`를 모두 포함한다. 예: `rg "^\s*(async )?def <name>\b" app`. 결과의 line number는 검증 근거일 뿐이다.
- HTTP URI와 상태 코드는 `app/routers/`에서, 상태 값은 `app/models/states.py`에서 직접 확인한다.

## 구현 기준 위치
변경과 관련된 파일만 읽는다.
- 상태·Enum: `app/models/states.py`
- API: `app/routers/`
- 실행: `app/services/`의 `runner.py`, `workflow_service.py`, `step_runner.py`
- 소유권·Session·Gate: `lease_service.py`, `session_service.py`, `human_gate_service.py`
- 테스트·완료: `test_policy_service.py`, `completion_checker.py`
- Provider: `app/providers/`, 프로세스·이벤트: `app/infra/process_control.py`, `app/infra/event_bus.py`

## 현재 불변 조건
아래는 2026-11 시점에 코드와 `docs/workflow`에서 확인한 사실이다. 개수는 맞춰야 할 목표가 아니다.
- Workflow는 `app/services/runner.py`의 `Runner`가 `execution_requests`를 처리해 실행한다. `TaskService`는 Task 생성과 Execution Request 저장만 한다(`create_task`와 `request_execution`이 분리되어 있다).
- PostgreSQL이 Task, Session, Approval, Checkpoint의 Source of Truth다. Redis는 구현되어 있지 않다.
- 별도의 `RecoveryService`와 `InvalidationService` 클래스는 없다. Recovery 책임은 `Runner`, `WorkflowService`, `HumanGateService`, `LeaseService`, `StepRunner`에 분산되어 있다. 무효화는 각 `_do_*` Step 진입 시 hash 비교로 이루어진다.
- Design Review PASS 뒤에는 일반 Human Gate 판정 없이 Development로 진행한다. Gate는 명시적 요청, 권한 요청 또는 Recovery 불확실 조건에서만 열린다.
- `ApprovalType`은 `DESIGN`, `PERMISSION`, `RECOVERY`로 구분되며 처리 경로가 다르다.
- Tester Agent 단계(`TaskState.TESTING`의 Tester 실행)와 Orchestrator 독립 테스트(`TestPolicyService.run`)는 별도다.
- Completion은 `app/services/completion_checker.py`의 `check_definition_of_done`이 검증한다.
- `EventBus`는 단일 프로세스 in-memory이며 best-effort다. 복구 경로는 REST 조회다.
- `WorkspaceSnapshot`은 변경 감지 전용이며 복원 수단이 아니다.

## 알려진 제한
- Advisory Lock 연결 상실을 지속 감시하지 않는다.
- 프로세스 종료 신원이 `(pid, create_time)`에 한정된다.
- Codex 역할의 PERMISSION 승인은 `enforceable=false`이면 RESUME이 생성되지 않아 Task가 `WAITING_APPROVAL`에 남고 자동 복구 경로가 없다.
- `SessionConflict` 이후의 복구 경로는 테스트로 검증되지 않았다.
- 외부 부수 효과 판단이 stdout/stderr tail의 정규식에 의존한다.
- Windows `ACCESS_DENIED` 종료 실패는 시뮬레이션으로만 검증됐다.
- 단일 Runner만 지원한다. 정확히 한 번 실행은 보장하지 않는다(at-least-once + 변경 감지).

## reference가 오래되었을 때
1. 실제 코드와 관련 테스트를 확인한다.
2. `docs/workflow`를 현재 구현에 맞춘다.
3. 갱신 권한이 있으면 오래된 항목을 수정하고, 근거를 최종 보고에 기록한다.

권한 판단:
- 코드와 reference가 달라도 코드 변경을 되돌리거나 reference에 맞춰 코드를 바꾸지 않는다.
- 요청의 허용 범위에 reference 수정이 포함되면 오래된 항목을 함께 갱신한다.
- 사용자가 범위를 `docs/workflow`로 명시 제한했거나 reference 수정 권한이 불명확하면 reference를 수정하지 않는다. 이미 허용된 `docs/workflow` 수정은 계속 진행한다.
- 이 경우 최종 보고에 오래된 reference 경로, 불일치 내용, 권장 수정사항을 기록한다.
- reference 불일치 때문에 안전하게 문서를 작성할 수 없는 경우에만 해당 변경을 보류하고 사용자 지시를 요청한다.
