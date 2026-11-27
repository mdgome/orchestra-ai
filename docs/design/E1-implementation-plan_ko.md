# E1 구현 계획 — test-only Task 실행 경로 (내부, API 미공개)

> **상태: 구현됨(E1). 승인 후 구현했고 §7에 결과와 계획과의 차이를 적었다.** 이 문서는 코드·테스트·migration을 바꾸지 않았다. 기준 설계는 [D4-test-only-task.md](D4-test-only-task.md)(v3.1, G1~G10 결정 기록됨, **문서 전체는 아직 미승인**)이며, 이 계획은 그 §11의 E1 범위를 구현 단위로 나눈 것이다.
> 사실은 HEAD `52afb9c`의 작업 트리에서 읽었다. 읽지 않았거나 실행하지 않은 것은 **(미확인)** 으로 표시한다.
> **착수 조건:** D4 설계의 명시적 승인(받음: 설계 승인, 사유 코드와 멱등성 범위는 제안대로).

## 1. 범위

**E1에 포함**
- `tasks.mode`, `idempotency_key`, `request_fingerprint`와 제약·index (migration `0006`)
- mode 인지 상태 전이 (`PENDING → TESTING`, `TESTING → COMPLETED`는 TEST_ONLY에서만)
- `WorkflowService`의 TEST_ONLY handler와 `(mode, state)` dispatch
- test-only 완료 판정 `evaluate_test_only_completion`과 mode 인지 `release_on_terminal`
- `TaskService.create_test_run` (내부 서비스 메서드, §8의 멱등성·활성 run 검사 포함)
- G3 대기 상한, G5 recovery 완화, 기존 경로(`PATCH /tasks/{id}`)의 TEST_ONLY 차단

**E1에 포함하지 않음 (E2)**
- `POST/GET /projects/{id}/test-runs`, `GET /test-runs/{id}`, `GET /tasks`의 `?mode=` 필터, `TaskOut.mode`
- 리포트·artifact의 `NOT_APPLICABLE` 표현, diagnostics 경고(G8), gate 만료 sweep(G6), 영·한 workflow 문서 묶음

**배포 가능성:** E1이 끝나도 API로 TEST_ONLY Task를 만들 수 없다(공개 endpoint 없음). 기존 행은 모두 `WORKFLOW`이므로 기존 동작은 바뀌지 않는다. E1 코드는 서비스 메서드와 테스트로만 닿는다.

## 2. 읽어서 확인한 접점

| 접점 | 확인한 사실 |
|---|---|
| Task 모델 | `app/models/tables.py`의 `Task`(`project_id` nullable, `profile_*` 3필드, `row_version`). 제약은 `__table_args__`의 `CheckConstraint`, migration 0003/0004는 `IS TRUE`를 쓰는 NULL-safe 형식 |
| migration | 최신은 `0005`(B2). `0003`은 `op.add_column` + `op.create_check_constraint` 스타일 |
| 상태 전이 | `check_task_transition(current, target)`은 `TASK_TRANSITIONS`만 본다. `change_task_state`가 이를 호출하고 terminal이면 `release_on_terminal`을 같은 transaction에서 부른다 |
| Task 생성 | `TaskService.create_task`: project row `FOR UPDATE`(archive와 직렬화) → policy 해석 → conversation 생성 → profile 복사 → Task INSERT → transition INSERT. `ExecutionRequest`는 별도 `request_execution`이 만든다 |
| dispatch | `run_task`가 `self._handlers[state]`를 호출하고 `WorkflowService._handlers`는 상태 키 dict다 |
| 에이전트 step | `_agent_step`: 잠금 gate(`ensure_for_step`, 재개 시 `GateLineage`) → `_reset_defer_once` → `steps.begin(on_begin=on_mutating_begin)` → `steps.execute`(에이전트 전용) → `evaluate` → `_complete_succeeded`(proof 재시도) |
| 테스트 실행 | 현재는 TEST 단계의 `evaluate` 안에서 `self.tests.run(..., tracked=self._tracked(...))`로 돌고, 완료 `mutate`가 `orchestrator_test_run` Execution 행을 추가한다 |
| terminal 해제 | `release_on_terminal`: 소유자 아님 → 무동작, 미해결 tracked 실행 → `DIRTY_UNRESOLVED`, 유효 proof + COMPLETED → CLEAN + **최종 지문이 새 baseline**, 그 외는 baseline 일치 시에만 CLEAN |
| 대기 | `ProjectBusy` → `_release_for_defer` → Runner가 `defer_delay(request.defer_count)`로 requeue. 횟수 상한 없음 |
| 중단 처리 | `_interrupted_needs_human`은 mutating step의 `snapshot_before`와 현재 workspace가 다르면 recovery gate를 연다. `_recovery_unsafe`의 `retry`도 같은 비교를 한다 |
| 보고서 | `KINDS`와 `tests` 섹션이 `STEP_TEST`를 읽는다(G10으로 재사용 확정) |
| 기존 endpoint | `PATCH /tasks/{id}`는 PENDING Task의 `requirements`·`limits`를 바꾼다 — TEST_ONLY에서 막아야 한다 |

## 3. 작업 단위 (순서대로, 각 단위는 별도 커밋)

### WP0 — 기준선
- `git status --porcelain=v1 -uall`, 전체 테스트 결과(HEAD 기준 **3258 passed, 12 skipped, 7 deselected**)를 기록한다. 이 수가 이후 단위의 비교 기준이다.

### WP1 — 상태 모델과 mode 인지 전이
- `app/models/states.py`: `TaskMode`(`WORKFLOW`, `TEST_ONLY`) 추가. `TASK_TRANSITIONS`는 **수정하지 않는다.** TEST_ONLY 전용 추가 edge(`PENDING → TESTING`, `TESTING → COMPLETED`)를 별도 상수로 두고 `check_task_transition(current, target, mode=TaskMode.WORKFLOW)`가 mode에 따라 합쳐 판정한다.
- `app/services/state_service.py`: `change_task_state`가 `task.mode`를 넘긴다.
- 테스트(unit): WORKFLOW의 허용 집합이 골든과 **동일**, WORKFLOW에서 두 새 edge 거부, TEST_ONLY에서 두 edge 허용, 나머지는 같음.
- 이 단위만으로 배포 가능(mode 컬럼 없이도 기본값이 WORKFLOW).

### WP2 — Task 컬럼과 migration `0006`
- 모델 `Task.mode`(`String(16)`, default/`server_default` `'WORKFLOW'`), `idempotency_key`(`String(128)` NULL), `request_fingerprint`(`String(64)` NULL).
- 제약(모두 NULL-safe `IS TRUE`): `ck_tasks_mode`(값 집합), `ck_tasks_test_only_project`(`mode='TEST_ONLY'`이면 `project_id IS NOT NULL`), `ck_tasks_idempotency_test_only`(키는 TEST_ONLY에서만). 부분 unique index `(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL`.
- downgrade: TEST_ONLY Task가 있으면 **거부**(이력 조용히 유실하지 않음), 없으면 index → 제약 → 컬럼 순으로 삭제.
- 테스트(integration_pg): up/down/up + `alembic check`, 제약 위반 INSERT 각각 거부, 기존 행이 `WORKFLOW`, downgrade 거부 케이스.
- 계획의 migration 번호 `0006`은 다른 변경이 먼저 들어가지 않을 때의 값이다.

### WP3 — 정책 상수와 실패 사유 코드
- `app/validation/policies.py`: `TEST_ONLY_REQUIREMENTS`(고정 문구), `TEST_RUN_MAX_DEFERS`, 실패 사유 코드 `TESTS_FAILED`, `NO_JUNIT`, `TOO_FEW_TESTS`, `CANCEL_REQUESTED`, `TESTS_TIMEOUT`, `PROJECT_BUSY`. 한도는 정책 모듈에만 둔다(기존 아키텍처 규칙).
- 코드 이름은 이 계획의 제안이다. 이름은 공개 계약이 되므로 승인 때 확정한다.

### WP4 — `TaskService.create_test_run`과 기존 경로 차단
- `create_task`의 공통 부분(project lock·ACTIVE 확인, policy 해석, conversation 생성, **profile 복사**, Task INSERT, 생성 transition)을 하나의 private 헬퍼로 뺀다. `create_task`는 시그니처와 동작이 그대로이며 기존 테스트가 이를 증명한다.
- `create_test_run(project_id, idempotency_key=None)`: 같은 transaction에서 project row 잠금 → 멱등성 조회(키가 있으면 **먼저**) → 활성 TEST_ONLY run 검사 → Task(`mode=TEST_ONLY`, 고정 `requirements`, 기본 limits) + 생성 transition + `ExecutionRequest(START, PENDING)` INSERT. 설계 §8의 다섯 가지 응답 규칙을 서비스 수준에서 구현한다(오류는 `Conflict`/`IDEMPOTENCY_KEY_REUSED`/`TEST_RUN_ACTIVE`).
- `update_task`는 TEST_ONLY Task에 대해 `Conflict`. `TaskCreate`에 `mode` 필드를 **추가하지 않는다**(아키텍처 테스트로 고정).
- 테스트(integration_pg): 멱등성 5행, 동시 같은 키 POST는 Task 하나, 활성 run이 있을 때 다른 키는 `TEST_RUN_ACTIVE`, archive와의 경합, profile 복사가 `create_task`와 같음, `update_task` 차단, ARCHIVED project 409.

### WP5 — Workflow handler (이 계획의 핵심)
1. **순수 리팩터링 먼저(동작 불변):** `_do_test`의 `evaluate` 안에서 테스트 실행 부분(owner assert → `tests.run(tracked)` → 결과 → 완료 `mutate`의 `orchestrator_test_run` 행)을 `_run_policy_tests` 같은 헬퍼로 추출하고, `_complete_succeeded`가 `StepExecution` 전체 대신 `snapshot_after`만 받도록 좁힌다. 이 단계는 기존 전체 스위트로만 검증하고 커밋을 분리한다.
2. **dispatch:** `run_task`가 `task.mode`로 handler 표를 고른다(`_handlers` 불변, TEST_ONLY용 표 신설).
3. **`_do_pending_test_only`:** `PENDING → TESTING`(`execution_started`).
4. **`_do_test_only`:** D4 §4.1의 순서 — `_interrupted_needs_human(…, mutating=True)`, `ensure_for_step`(재개 시 `GateLineage`), `_reset_defer_once`, `steps.begin(on_begin=on_mutating_begin)`, `_run_policy_tests`(tracked), `snapshot_after`, 완료 판정(WP6), proof 재시도 완료. `steps.execute`는 쓰지 않으며 provider·session·prompt를 만들지 않는다.
   - 별도 `StepRunner.execute_local`이 필요한지는 **구현 중 확정한다.** 읽은 바로는 `TrackedCommandSession`이 STARTING 행, 취소 watcher, 종료 증명을 이미 처리하므로 `StepRunner`에 새 메서드 없이 handler와 헬퍼만으로 될 가능성이 높다(미확인).
   - step output은 `test_result`, `test_policy_hash`, `requirements_hash`, `code_fingerprint`를 포함해 기존 `tests` 섹션이 읽는 모양을 유지한다. 테스트가 실패해도 step은 `SUCCEEDED`(실행은 끝났다)이고 Task만 `FAILED`가 된다.
5. **G3:** `run_task`의 `ProjectBusy` 처리에서 Task가 TEST_ONLY이고 요청의 `defer_count`가 `TEST_RUN_MAX_DEFERS` 이상이면 `_release_for_defer` 대신 `_fail(…, "PROJECT_BUSY", …)`. WORKFLOW는 그대로 무제한 defer.
6. **G5:** TEST_ONLY에서는 `_interrupted_needs_human`과 `_recovery_unsafe(retry)`의 snapshot-diff 비교를 생략한다(부분 실행된 빌드가 파일을 바꾸는 것이 정상). 프로젝트 지문과 tracked 실행 증명이 안전망으로 남는다.

### WP6 — 완료 판정
- `completion_checker.py`에 순수 함수 `evaluate_test_only_completion(...) -> list[Condition]` 추가(D4 §5의 조건). 기존 `Condition` 형태를 쓰고 `check_definition_of_done`은 건드리지 않는다.
- 조건 중 `PROCESS_TREE_TERMINATION_CONFIRMED`는 같은 transaction에서 이 Task의 미종결·이상 tracked 실행이 없음을 확인하는 방식으로 구현한다(정확한 질의는 `project_terminal`의 기존 함수를 읽고 확정, 미확인). 취소 요청이 완료보다 우선한다.
- 테스트(unit): 조건별 충족/미충족, 첫 미충족 조건이 `failure_reason`이 됨, 취소 우선.

### WP7 — mode 인지 `release_on_terminal`
- `project_terminal.py`: 유효 proof + `COMPLETED`일 때 **WORKFLOW만** 최종 지문을 새 baseline으로 채택한다. TEST_ONLY는 COMPLETED/FAILED/CANCELLED 모두 `write_baseline_fingerprint`와 같을 때만 CLEAN(accepted baseline 불변), 다르거나 proof가 없으면 `DIRTY_UNRESOLVED`.
- 테스트(integration_pg): 변경 없는 COMPLETED → CLEAN·baseline 불변, tracked 파일이 바뀐 COMPLETED → `DIRTY_UNRESOLVED`·baseline 미교체, FAILED·CANCELLED 동일, 측정 실패 → `DIRTY_UNRESOLVED`, **WORKFLOW COMPLETED는 여전히 baseline을 채택(회귀)**.

### WP8 — E1 통합 테스트 (`tests/integration_pg/test_test_only_task.py`)
기존 `make_project` 헬퍼(`test_project_lock.py`)와 Harness를 쓴다.
- 에이전트 호출 없음: 모든 provider spy, `cli_invocations == 0`, Planner/Reviewer/Developer 호출 0.
- 상태 경로 `PENDING → TESTING → COMPLETED`, 실패 시 `FAILED(TESTS_FAILED)`, JUnit 없음·너무 적은 테스트·timeout 각각의 사유 코드.
- 잠금 상호 배제: TEST_ONLY 대 WORKFLOW, TEST_ONLY 대 TEST_ONLY — owner는 정확히 하나.
- DIRTY 작업 공간 → `PROJECT_DIRTY` gate, **plan 없는 Task의 gate 결정이 끝까지 동작**(`continue`는 `TESTING`에서 재개, `abort`는 `FAILED`) — D4의 미확인 항목을 닫는다.
- 취소(명령 중, 트리 소멸 증명 후 `CANCELLED`), 트리 미확인 → parking·owner 유지·재실행 없음, 취소가 완료보다 우선.
- G3 대기 상한 도달 → `FAILED(PROJECT_BUSY)`, WORKFLOW는 계속 defer.
- 보고서 스모크: TEST_ONLY Task에 대해 `ReportService`의 보고서와 artifact 목록이 **예외 없이** 응답한다(`NOT_APPLICABLE` 표현은 E2, 여기서는 plan 없는 Task가 깨지지 않는지만 본다).
- `/tasks/{id}/changes`가 TEST_ONLY Task의 baseline 대비 부산물을 보고한다.
- 회귀: `TASK_TRANSITIONS` 불변, WORKFLOW Task가 `PENDING → TESTING`·`TESTING → COMPLETED`를 할 수 없음, 기존 전체 스위트.

### WP9 — 문서
- 구현 의미가 바뀌므로 `workflow-docs-sync`로 상태 전이·프로젝트 잠금 workflow 문서를 영·한 쌍으로 갱신한다(TEST_ONLY는 공개 endpoint가 없음을 현재 구현 제한으로 명시).
- D4 문서 상태 줄에 "E1 구현됨, E2 미구현"을 반영한다.

## 4. 순서와 점검
- WP1 → WP2 → WP3 → WP4 → WP5(리팩터링 → handler) → WP6 → WP7 → WP8 → WP9. WP1·WP2·WP3·WP4는 서로 독립적으로 배포 가능하다.
- 각 WP는 별도 커밋, 커밋마다 체크아웃 전체 테스트를 돌린다(WP0의 수에서 **기존 테스트가 줄지 않았는지** 먼저 본다). 커밋과 push는 별도 지시를 받은 뒤에만 한다.
- 락 순서: 새 lock 간선을 만들지 않는다. `create_test_run`은 `P`(project row) 후 INSERT뿐이고 실행·gate는 기존 순서를 쓴다(D4 §10).

## 5. 위험과 미확인
| 항목 | 내용 |
|---|---|
| 리팩터링 위험 | `_do_test`의 `evaluate` 추출은 TEST 단계 전체에 영향을 준다. 동작 불변 커밋으로 분리하고 기존 스위트로만 검증한다 |
| 완료 조건 일부 | `PROCESS_TREE_TERMINATION_CONFIRMED`의 정확한 질의는 `project_terminal`/`tracked_classifier`를 읽고 확정한다 |
| `execute_local` | 필요 여부를 구현 중 확정한다. 필요하면 `StepRunner`에 한정된 작은 메서드로 추가한다 |
| recovery 옵션 | TEST_ONLY의 RECOVERY gate에서 `abort`/`retry`가 실제로 나오는지는 테스트로 확인한다(미확인) |
| 실제 환경 | JDK/Maven/Gradle, 실제 CLI, POSIX는 이 계획에서 검증하지 않는다. B2의 Windows/POSIX 프로세스 테스트 결과는 플랫폼별로 따로 보고한다 |
| 승인 | D4 설계 전체가 미승인이다. 승인 후 이 계획의 사유 코드 이름과 WP4의 서비스 수준 멱등성 범위를 확정한다 |

## 6. 완료 기준
- WP1~WP8의 테스트가 통과하고 전체 스위트(WP0 기준 + 새 테스트)가 체크아웃에서 통과한다.
- `TASK_TRANSITIONS`, WORKFLOW의 `release_on_terminal`, `create_task`의 동작이 기존과 같음을 회귀 테스트가 증명한다.
- 문서(WP9)가 코드와 일치한다.
- **주장하지 않는 것:** 공개 API, 보고서의 `NOT_APPLICABLE` 표현, 진단 경고, 실제 JDK/CLI/POSIX 검증.

## 7. 구현 결과와 계획과의 차이

- **그대로 구현:** WP1(상태 모델), WP2(migration `0006`), WP3(정책 상수), WP4(`create_test_run`과 `update_task` 차단), WP5(handler, 리팩터링 먼저), WP6(완료 판정), WP7(`release_on_terminal`), WP8(통합 테스트), WP9(03·11번 문서 영한).
- **`execute_local`은 만들지 않았다.** 예상대로 `TrackedCommandSession`과 추출한 헬퍼(`_run_orchestrator_tests`, `_orchestrator_run_row`)만으로 충분했다. `StepRunner`는 바뀌지 않았다.
- **완료 조건이 계획보다 세분화되었다.** `COMMANDS_SUCCEEDED`, `NO_TIMEOUT`, `NO_FAILED_TESTS`를 추가해 사유 코드가 판정과 일치하게 했다(예: 명령이 JUnit 없이 죽으면 `NO_JUNIT`이 아니라 `TESTS_FAILED`). `LEASE_VALID`와 `CLAIM_FENCE_VALID`는 조건 목록에 넣지 않았다. 완료 트랜잭션이 구조적으로 강제하며 판정값을 만들지 않는다.
- **상수 값:** `TEST_RUN_MAX_DEFERS = 10`(defer 백오프로 약 7분). 사유 코드는 계획의 이름 그대로다.
- **계획에 없던 수정(발견):** (1) 취소·parked recovery가 `TEST_ONLY` Task를 `PLAN` 체크포인트로 보내면 재개 시 handler가 없는 상태로 들어가 버리므로 `TEST`로 바꾸고, handler가 없는 상태는 fail-closed로 막았다. (2) `PENDING → TESTING`은 잠금 확인보다 먼저 일어나므로, 바쁜 project에서 기다리는 test run의 상태는 `PENDING`이 아니라 `TESTING`이다.
- **기존 테스트의 기대값 갱신 한 건:** `tests/unit/test_process_schema.py`의 migration head 단언(0005 → 0006). 다른 테스트는 약화하지 않았다.
- **검증:** 새 테스트(단위·통합·아키텍처)와 변이 검증 6건(release 규칙, 대기 상한, update 차단, 멱등성 조회, 전이 edge, 취소 조건)이 모두 실패를 잡았다. 전체 스위트 결과와 실행하지 않은 검증은 최종 보고에 기록한다.
- **E2로 남은 것:** 공개 endpoint, 리포트·artifact의 `NOT_APPLICABLE`, diagnostics 경고(G8), gate 만료 sweep(G6), `TaskOut.mode`와 `?mode=` 필터(G2), 나머지 영·한 workflow 문서.
