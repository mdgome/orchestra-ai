# D4 설계 v2 — Project Test Run 과 Write Lock 도메인

상태: **v2 초안 — 독립 재검토 결과 "재작업" (부록 B), 미승인.** 재검토는 (a) 대신 (d) test-only Task를 권고했다. v1의 독립 리뷰(BLOCKER 3, MAJOR 7)를 모두 반영했다(부록 A에 항목별 대응). **이 문서가 승인되기 전에는 lock-domain migration, `project_test_runs`, 어떤 test-run API·Runner 연동도 구현하지 않는다.** 승인 후 영문본과 `docs/workflow/07-lease`, `09-recovery`, `11-task-lifecycle`(영·한)을 구현과 함께 갱신한다.

## 0. 결정된 원칙 (사용자 지시)

1. owner는 **`NONE | TASK | TEST_RUN` 명시적 3상태**다. `write_owner_task_id IS NULL`을 "자유"로 해석하는 코드는 없다.
2. 기존 Task owner 접근 지점을 모두 **discriminated owner API**로 교체한다.
3. **프로세스 트리 종료가 확인되지 않으면 ownership을 해제하지 않는다.** 불확실하면 `RECOVERY_REQUIRED` 또는 Human Gate. **RECOVERY 우회 금지.**
4. 예약 transaction 안에서 장시간 fingerprint 측정을 하지 않는다. 기존 Project Lock과 같은 **측정 → proof 검증 → 예약** 패턴.
5. 완료·취소·lease 만료·recovery는 **Project row lock으로 직렬화**한다.

## 1. 목적과 비목표

목적: Task 없이 프로젝트의 테스트를 한 번 실행해 현재 상태를 확인한다.
비목표: 의존성 설치, 임의 명령, 대기열, 병렬 실행, 컨테이너/worktree, 자동 수정·commit, interpreter 선택 변경(D5), Gradle 신선도 정책(F), 다중 Runner.

## 2. 현재 lock 도메인 (코드 기준)

- owner = `projects.write_owner_task_id`(단일 컬럼 FK) + `write_baseline_fingerprint`, `ck_projects_write_owner_baseline`(`(owner IS NULL) = (baseline IS NULL)`), `write_epoch`(acquisition·mutating step 시작·release마다 +1).
- `write_owner_task_id` 사용 `app/` 28곳: `tables.py` 3, `project_repository.py` 4, `project_lock_service.py` 14, `project_terminal.py` 2, `diagnostics_service.py` 4, `change_service.py` 1. `project_lock_service.py`의 `is None`/`is not None` 판정(`read_state`, `ensure_for_step`, `_mark_dirty_unmeasured`, `_acquire_once`)이 "자유/점유"의 실제 의미다.
- release = `project_terminal.release_on_terminal`(Task 종료, 같은 transaction)과 `ProjectLockService.reconcile`(crash 잔여). `accept_baseline`은 `ProjectDirtyRecoveryEffect`(Human Gate `continue`) 한 곳.
- Runner(단일, advisory lock)는 `execution_requests`→Task만 처리한다. Lease·`task_runs`·`executions`(pid·`process_create_time`)·`approvals`(`task_id NOT NULL`)·Checkpoint는 모두 Task 중심이다. 프로세스 종료 확인 도구: `process_control.ProcessIdentity`/`TerminationResult`/`CONFIRMED_GONE`.

## 3. 문제와 대안

Test Run은 build/JUnit/cache를 바꾸는 workspace mutation이므로 write lock 아래에서만 실행한다. 현 owner는 Task만 표현한다.

| 안 | 내용 | 평가 |
|---|---|---|
| **(a) owner kind 확장** (이 문서의 본안) | `projects` 한 행에 3상태 owner. Run은 별도 테이블·별도 Runner 경로 | 불변식은 한 행에 모이나 Lease·heartbeat·recovery·gate를 Run용으로 **병렬 구축**해야 함 |
| (b) 별도 예약 테이블 | Task는 기존, Run은 별도 | 불변식이 두 곳. 비권고 |
| (c) 미제공 | | 가장 안전, 기능 없음 |
| **(d) test-only Task** (재검토 요청) | Test Run을 **Task의 한 종류**로 만든다(CLI 에이전트 없이 오케스트레이터 TEST 단계만). owner 도메인 변경 없음 | Lease·Checkpoint·프로세스 종료 확인·`PROJECT_DIRTY` gate·recovery를 **그대로 재사용**, B1~B3·M1~M3이 구조적으로 사라짐. 대신 Task 모델에 "실행 모드"가 생기며, 이는 후속 설계로 분리해 둔 "계획/검토/전체 모드"와 겹침 |

본 문서는 사용자 지시에 따라 (a)를 상세화한다. (d)는 (a)가 요구하는 병렬 기계(§7~§9)의 크기와 비교할 수 있도록 남겼고, **E-1 결정 항목**으로 재검토 대상이다.

## 4. Owner 모델

### 4.1 스키마 (`projects`)

- `write_owner_kind text NOT NULL DEFAULT 'NONE' CHECK IN ('NONE','TASK','TEST_RUN')`
- `write_owner_task_id`(기존, nullable), `write_owner_test_run_id uuid NULL`
- `ck_projects_write_owner` (기존 `ck_projects_write_owner_baseline` 교체, 모두 `IS TRUE`):
  ```
  (kind='NONE'     AND task_id IS NULL     AND run_id IS NULL     AND baseline IS NULL)
  OR (kind='TASK'     AND task_id IS NOT NULL AND run_id IS NULL     AND baseline IS NOT NULL)
  OR (kind='TEST_RUN' AND task_id IS NULL     AND run_id IS NOT NULL AND baseline IS NOT NULL)
  ```
- 복합 FK `(id, write_owner_test_run_id) → project_test_runs(project_id, id)` (MATCH SIMPLE, `fk_projects_active_profile`와 같은 패턴). 비대칭 주의: 기존 Task owner FK는 단일 컬럼이며, 이번에 바꾸지 않는다.
- 테스트 전용 `UPDATE`로 kind만 바꾸는 우회를 막는 DB 불변식은 위 CHECK가 전부다.

### 4.2 Discriminated owner API (R1 대책)

`app/repositories/project_repository.py`에 다음만 둔다.

```text
OwnerView = frozen dataclass(kind: NONE|TASK|TEST_RUN, task_id|None, run_id|None)
ProjectRepository.owner_of(project) -> OwnerView
ProjectRepository.set_task_owner(...)   # WHERE write_owner_kind='NONE' AND write_epoch=:expected
ProjectRepository.set_run_owner(...)    # 동일 조건. write_baseline_fingerprint만 설정, baseline_fingerprint(accepted)는 건드리지 않음
ProjectRepository.release_owner(project_id, expected: OwnerView, state, baseline)  # expected owner와 일치할 때만
```

- `project_lock_service.py`의 14곳, `project_terminal.py` 2곳, `diagnostics_service.py` 4곳, `change_service.py` 1곳은 전부 `OwnerView`를 받아 `match kind`로 분기한다. "자유" = `kind == NONE`. `task_id is None` 비교는 존재하지 않는다.
- `FingerprintProof`는 `owner_task_id` 대신 `owner: OwnerView`를 가지고, `proof_matches`는 `OwnerView`와 epoch를 모두 비교한다(None==None 문제 제거).
- **강제 테스트(아키텍처):** `.write_owner_task_id` / `.write_owner_test_run_id` 속성 접근과 컬럼 참조를 AST로 검사해 `project_repository.py`(+모델 정의)의 허용 위치 밖에서 금지. 허용 위치와 이유를 테스트에 명시한다. 이전 안(허용 목록에 `project_lock_service.py` 포함)은 폐기.
- Task 경로의 의미: owner가 `TEST_RUN`이면 `ensure_for_step`은 측정 없이 즉시 BLOCKED(지연). 측정·`_mark_dirty_unmeasured`·`FingerprintProofStale` 재시도 경로에 들어가지 않는다.

### 4.3 read-only Task step (M1)

- `read_state`는 `OwnerView`를 반환한다. owner가 `TEST_RUN`이고 Run이 비종결(ACQUIRED/RUNNING/RECOVERY_REQUIRED)이면 "다른 owner가 실행 중"과 동일하게 **지연**(BLOCKED)한다. Run 종결 후 다음 pass에서 진행.
- Run의 acquire/release로 인한 epoch 증가가 Task의 `PROJECT_EPOCH_RETRIES`(2)를 소모하지 않도록, `_project_changed`가 변경의 양 끝에서 owner kind `TEST_RUN`을 보면 실패 대신 지연으로 처리한다.

## 5. `project_test_runs`

| 컬럼 | 의미 |
|---|---|
| `id`, `project_id`(FK), `UNIQUE(project_id,id)` | |
| `status` CHECK | `REQUESTED, ACQUIRED, RUNNING, RECOVERY_REQUIRED` (비종결) / `PASSED, FAILED, TIMED_OUT, CANCELLED, EXPIRED` (종결) |
| `idempotency_key`, `request_fingerprint`, `UNIQUE(project_id, idempotency_key)` | 부분 unique(키가 있을 때) |
| `test_policy_hash`, `policy_snapshot jsonb` | 요청 시점 정책(명령은 Project `test_policy`가 해석한 것뿐) |
| `requested_at`, `expires_at`(REQUESTED 상한), `acquired_at`, `started_at`, `finished_at` | |
| `runner_id`, `lease_epoch bigint`, `lease_expires_at` | fencing: acquire/takeover마다 +1, 모든 전이는 `lease_epoch` CAS |
| `launch_state` CHECK | `NOT_LAUNCHED, LAUNCHING, LAUNCHED`; `pid`, `process_create_time`(`ProcessIdentity`); `terminated_state`(`TerminationResult` 값), `terminated_at` |
| `cancel_requested_at` | |
| `pre_fingerprint`, `post_fingerprint`, `workspace_effect` | `UNCHANGED | CHANGED | UNMEASURED`; CHANGED면 `changed_paths`(상한·sanitize) |
| `result jsonb`, `result_sha256`, `reason` | JUnit 집계(원문 로그 미저장), 이유 코드 |
| CHECK | 종결이면 `finished_at` 필수; `REQUESTED`이면 owner·pid·lease 없음; `LAUNCHED`이면 pid·ctime 필수 등 |

## 6. 상태 기계와 직렬화

```text
REQUESTED ──(Runner: 측정→proof→owner 획득)──► ACQUIRED ──(spawn)──► RUNNING ──► PASSED|FAILED|TIMED_OUT|CANCELLED
    │ expires / cancel(API)                          │                    │
    ▼                                                └── 프로세스 종료 미확인 ──► RECOVERY_REQUIRED ──(gate)──► 종결
 EXPIRED | CANCELLED
```

- **모든 전이의 잠금 순서: Project 행 `FOR UPDATE` → Run 행**(역순 금지). 전이는 `status`와 `lease_epoch` CAS이며, owner 해제는 `release_owner(expected=OwnerView(TEST_RUN, run_id))`로 **이 Run이 owner일 때만** 한다. 늦게 도착한 종료자(CAS 패배)는 결과를 폐기하고 로그만 남기며 owner를 풀 수 없다.
- 완료·취소·lease 만료·recovery·gate effect는 모두 위 규칙의 같은 함수들(`finish_run`, `expire_run`, `recover_run`)을 통과한다.
- **REQUESTED는 lock을 쥐지 않는다.** 따라서 Runner가 준비되지 않았거나(`ALREADY_ACTIVE`·STOPPED) 오래 방치돼도 Task를 막지 않는다(M3). `expires_at` 경과 시 `EXPIRED`.

## 7. 요청·획득·실행

### 7.1 `POST /projects/{id}/test-runs` (DB 전용, 측정 없음)

검증(`validators.py`): Project ACTIVE, `test_policy` 존재. fingerprint 측정·파일 접근·lock 없음(M7). 응답 202.

**멱등 규칙(키 조회가 모든 검사보다 먼저):**

| 요청 | 응답 |
|---|---|
| 같은 `idempotency_key` + 같은 `request_fingerprint`, Run이 **진행 중** | 200, 그 Run(`replayed:true`) |
| 같은 키 + 같은 fingerprint, Run이 **종결** | 200, 그 Run의 최종 결과(`replayed:true`) |
| 같은 키 + **다른** fingerprint(정책 변경 등) | 409 `IDEMPOTENCY_KEY_REUSED` |
| 새 키/키 없음 + 이 Project에 비종결 Run 존재 | 409 `TEST_RUN_ACTIVE`(활성 Run id 포함) |
| 새 키/키 없음 + 비종결 Run 없음 | 202, 새 REQUESTED Run |

동시 동일 키 요청은 `UNIQUE(project_id, idempotency_key)` 충돌 시 키 조회로 되돌아가 위 표에 따라 응답한다(IntegrityError를 응답으로 노출하지 않음). 요청 시점의 "Task가 owner" 판정은 **하지 않는다**(권위 있는 판정은 획득 시점).

### 7.2 획득 (Runner, 측정 → proof → 예약)

Runner가 REQUESTED를 하나 집는다(Test Run 동시 실행 1개).
1. transaction 밖: Project 읽기(`OwnerView`, epoch, 상태) → `NONE`·`CLEAN`·GIT mode가 아니면 이유 코드로 Run 종결(`PROJECT_BUSY` 재시도 여부는 7.4), 맞으면 **fingerprint 측정**(`to_thread`, deadline).
2. 짧은 transaction: Project `FOR UPDATE` → `proof_matches`(owner·epoch) 확인 → `set_run_owner(expected_epoch)` CAS(`epoch+1`) + Run `REQUESTED→ACQUIRED`(`lease_epoch+1`, `runner_id`, `pre_fingerprint`)를 **함께** commit. 불일치면 `FingerprintProofStale`로 1로 되돌아가 `PROOF_MAX_ATTEMPTS`까지 재측정, 소진 시 `FAILED(reason=WORKSPACE_UNSTABLE)`(owner는 잡지 않았으므로 해제할 것 없음).
3. `ACQUIRED` 상태가 commit된 **뒤에** `LAUNCHING`을 commit하고 프로세스를 spawn한다(Execution의 STARTING-before-spawn 패턴). spawn 직후 pid·`process_create_time`을 기록(`LAUNCHED`).

GIT mode만 허용(E-2). 비 git(CONTENT) workspace는 `FAILED(TEST_RUN_UNSUPPORTED_WORKSPACE)`.

### 7.3 실행 주체·heartbeat·Runner 연동 (M3, 명시)

- **실행 주체는 기존 Runner 한 개.** Runner `_loop`는 `_dispatch`(Task) 뒤에 `_dispatch_test_runs`를 호출한다. Test Run 슬롯은 1개이며 별도 in-memory 집합 `_test_inflight`로 추적하고, `_busy_projects`에 그 Project를 포함해 Task dispatch가 건너뛴다.
- **heartbeat는 Run 실행 coroutine 내부**에서 `lease_expires_at`을 갱신한다(Runner와 같은 프로세스·같은 이벤트 루프). heartbeat가 실패하거나 fencing(`lease_epoch`)이 어긋나면 그 coroutine이 스스로 프로세스를 종료 시도하고 §8 규칙으로 넘어간다.
- **lease 만료 reconcile은 `_test_inflight`에 있는 Run을 절대 건드리지 않는다**(살아 있는 Runner 자신의 Run을 ABANDON하지 않는다).
- **startup recovery:** `Runner.start`가 advisory lock을 얻은 직후(단일 Runner 증명) `_test_inflight`에 없는 모든 `ACQUIRED/RUNNING/RECOVERY_REQUIRED` Run은 이전 incarnation의 고아다. lease 만료를 기다리지 않고 즉시 §8의 복구를 수행한다. 이 단계는 기존 `recover(startup=True)` 안에 편입하며 기존 RECOVERY 상태 기계(Checkpoint/Approval)를 우회하지 않는다.
- **`stop()`/drain:** 진행 중 Run을 취소 신호로 종료하고 §8 해제를 수행한다. 프로세스 종료가 확인되지 않으면 `RunnerStopIncomplete`에 해당 run id를 포함하고 owner를 유지한다(상태는 `RECOVERY_REQUIRED`).
- **`ALREADY_ACTIVE` 인스턴스:** 이 인스턴스는 REQUESTED 행만 만든다. 실행·owner 획득은 advisory lock을 가진 인스턴스의 Runner가 한다. 요청 경로에서 Runner 준비 여부를 검사하지 않으며, 진단은 RUNNER_STATE로 안내한다.

### 7.4 owner 불가(BUSY/DIRTY) 처리

획득 시 owner가 NONE이 아니면(Task 또는 다른 Run이 보유) 대기열을 만들지 않고 `FAILED(PROJECT_BUSY)`로 종결한다(E-3). `workspace_state=DIRTY_UNRESOLVED`이면 §9의 DIRTY gate를 연다.

## 8. 종료 확인 없이는 해제 없음 (B3)

**해제 사전조건(둘 중 하나일 때만 `release_owner` 가능):**
- (P1) `launch_state = NOT_LAUNCHED` — 프로세스를 띄운 적이 없음이 DB로 증명됨(ACQUIRED에서 취소·만료·crash가 났고 `LAUNCHING`이 commit되지 않음).
- (P2) `terminated_state ∈ CONFIRMED_GONE`(`TERMINATED` 또는 `ALREADY_EXITED`, `ProcessIdentity` 일치 검증 포함) — 프로세스 트리가 사라졌음이 확인됨.

그 외 — `LAUNCHING`인데 pid 미기록, `STILL_ALIVE`, `ACCESS_DENIED`, `IDENTITY_MISMATCH`, `UNKNOWN`, 종료 시도 중 timeout — 는 전부 **`RECOVERY_REQUIRED`**: owner를 유지하고(Project는 `TEST_RUN` 소유로 남아 모든 Task가 BLOCKED), Human Gate를 연다(§9). 어떤 경우에도 "DIRTY로 풀고 끝"이 아니다(살아 있는 프로세스가 해제 후에도 workspace를 바꿀 수 있기 때문).

- **트리 종료:** `CommandExecutor`의 프로세스 트리 종료를 사용하고, `ProcessIdentity`로 pid 재사용을 구분한다. spawn 시 환경변수 `ORCH_TEST_RUN_ID`를 부여해 pid가 기록되지 않은 고아를 찾는 **보조** 수단으로 쓴다(전 프로세스 열거가 `AccessDenied` 없이 완료되고 일치 프로세스가 없을 때만 "없음"의 증거로 인정; 그렇지 않으면 증명 실패).
- **해제 후 측정:** P1/P2 충족 후 fingerprint를 transaction 밖에서 측정 → 짧은 transaction에서 proof 검증 + `release_owner`. 측정값 == `write_baseline_fingerprint`이면 `CLEAN`, 그 외(변경·측정 불가) `DIRTY_UNRESOLVED`. **`accept_baseline`은 호출하지 않는다**(`baseline_fingerprint` 불변).
- timeout(`CommandExecutor`)과 취소도 동일 경로다.

## 9. Human Gate

### 9.1 필요한 gate

| 이유 코드 | 열리는 때 | 옵션 → effect |
|---|---|---|
| `PROJECT_DIRTY` (Test Run 대상) | 획득 시 `DIRTY_UNRESOLVED`(Test Run만 쓰는 Project의 막다른 길 해소, M2) | `continue` → 기존과 같은 proof·`dirty_epoch` 계약으로 `accept_baseline` 후 Run을 REQUESTED로 되돌림 / `abort`·reject → Run `CANCELLED`, Project는 DIRTY 유지 |
| `TEST_RUN_PROCESS_UNCONFIRMED` | §8 사전조건 미충족(`RECOVERY_REQUIRED`) | `retry_termination` → 종료 재시도, 확인되면 §8 해제 / `declare_gone` → 운영자 attestation을 기록하고 `DIRTY_UNRESOLVED`로 해제(자동 CLEAN 없음) / 보류 → 아무것도 하지 않음 |

### 9.2 스키마 영향 (migration 0006)

현재 `approvals.task_id NOT NULL`, `uq_approvals_project_dirty_pending`은 `task_id` 단위다. 확장:
- `approvals.task_id` nullable, `approvals.test_run_id uuid NULL FK`, CHECK **정확히 하나**(`task_id` XOR `test_run_id`).
- `uq_approvals_project_dirty_pending`을 (task_id 또는 test_run_id) 단위 부분 unique 두 개로 분리.
- `HumanGateService.open_recovery`/`decide`와 `RecoveryEffects` 레지스트리가 Run 대상 effect(`ProjectDirtyRecoveryEffect`의 Run 변형, `TestRunProcessRecoveryEffect`)를 받도록 일반화. Task 대상 경로의 동작·응답은 변하지 않는다.
- 이 변경은 Human Gate의 핵심이므로 gate 권한 완화 없음: 결정은 기존과 같은 `Idempotency-Key`·basis 검증을 거친다.

## 10. Task·Changes·Diagnostics 가시성

- **Task 경로:** §4.2/§4.3. owner `TEST_RUN`이면 지연.
- **Changes** (`change_service`): `lock_view`가 `OwnerView`를 포함한다. `workspace_relation`에 `TEST_RUN_OWNS` 추가(기존 값 불변), 이때 `allow_bodies=false`(본문 `WITHHELD(TEST_RUN_IN_PROGRESS)`). `lock_consistency`는 `(kind, owner id, epoch, state)` 전체를 비교. 종료된 Task의 Changes에는 이후 Test Run 부산물이 섞일 수 있으므로 `limitations[]`에 "그 사이 Test Run이 workspace를 바꿨을 수 있음"을 추가한다(`workspace_effect`가 CHANGED인 Run이 그 Task 종료 이후 존재하면 명시).
- **Diagnostics:** 응답에 `owner_kind`를 노출하고 `_lock_item`은 kind별 문구("다른 Task" / "Test Run"). 캐시 키에 `owner_kind`·owner id·`write_epoch`를 포함(Run의 CLEAN→CLEAN 예약/해제가 보이도록). 새 항목 `TEST_OUTPUT_IGNORED`: JUnit·build 출력 경로가 git ignore되지 않았으면 WARN(`git check-ignore -q`, 읽기 전용 git 화이트리스트에 추가) — 부산물로 인한 영구 DIRTY(M2)를 사전에 알린다.
- `workspace_effect=CHANGED`이면 `changed_paths`(상한·sanitize)를 Run 결과에 남겨 `.gitignore` 보완을 안내한다.

## 11. API

| 메서드 | 경로 | 비고 |
|---|---|---|
| POST | `/projects/{id}/test-runs` | 202/200/409 (§7.1). body: `idempotency_key`(선택)만. 명령·인자·환경·경로 없음 |
| GET | `/projects/{id}/test-runs` | 페이지네이션 계약 |
| GET | `/test-runs/{id}` | 상태·집계·`workspace_effect`·`process`(종료 확인 상태, pid 등 비노출)·`disclaimer` |
| POST | `/test-runs/{id}/cancellation` | REQUESTED → 즉시 `CANCELLED`(lock 없음·측정 없음). ACQUIRED/RUNNING → `cancel_requested_at`만 기록(Runner가 §8로 종료). 종결 Run은 200 멱등 |

라우터는 기존 규칙(≤10문, 로직 없음), 검증은 `validators.py`, 한도는 `policies.py`, 응답 크기·sanitizer 계약 준수. "PASSED"는 Task 완료·merge 가능을 뜻하지 않는다는 문구를 고정한다. Project archive는 비종결 Run이 있으면 409(`count_open_tasks`에 Run 개수 합산).

## 12. Migration과 배포 가능 상태

| 단계 | migration | 내용 | 이 단계만 배포했을 때 |
|---|---|---|---|
| **E1** lock-domain | 0005 | 아래 순서 | 동작 변화 없음(`TEST_RUN` owner 생성 경로 없음). Task 흐름·테스트 전부 동일. 4.2의 owner API·AST 테스트 포함 |
| **E2** run 도메인·Runner·gate | 0006 | `approvals` 일반화(§9.2) + 필요 시 `project_test_runs` 보조 컬럼 | API 없음. Run은 테스트 코드로만 생성. Runner 연동·복구·gate가 완결 |
| **E3** API·가시성·문서 | 없음 | §10, §11, 영·한 문서 | 기능 공개 |

**0005 순서(PostgreSQL):** ① `project_test_runs` 생성(+`UNIQUE(project_id,id)`) → ② `projects`에 `write_owner_kind`(DEFAULT 'NONE' NOT NULL)·`write_owner_test_run_id` 추가 → ③ `UPDATE … SET write_owner_kind='TASK' WHERE write_owner_task_id IS NOT NULL` → ④ 옛 CHECK 삭제 → ⑤ 새 CHECK 추가 → ⑥ 복합 FK 추가. ORM FK는 `use_alter=True`.
**0005 downgrade:** `write_owner_kind='TEST_RUN'` 행 또는 비종결 Run이 있으면 **거부**(데이터 유실 방지) → 복합 FK 삭제 → 새 CHECK 삭제 → 컬럼 2개 삭제 → 옛 CHECK 복원 → `project_test_runs` 삭제.
**0006 downgrade:** `task_id IS NULL`인 approvals가 있으면 거부 → 인덱스 복원 → CHECK·`test_run_id` 삭제 → `task_id NOT NULL` 복원.
각 단계는 up/down/up과 `alembic check`를 통과하고 전체 회귀가 통과해야 다음으로 간다.

## 13. 테스트 계획

**E1 (R1 증명, API 없이):** 테스트가 `TEST_RUN` owner 행을 직접 INSERT해 (1) Task `ensure_for_step`이 측정·`_mark_dirty_unmeasured` 없이 즉시 BLOCKED, (2) read-only step 지연, (3) `FingerprintProof`가 kind 다른 owner를 거부, (4) Changes·Diagnostics가 kind를 표시, (5) 새 CHECK의 모든 조합 거부(두 owner 동시, kind-id 불일치, baseline 불일치, 타 Project Run 복합 FK), (6) backfill, (7) up/down/up, TEST_RUN 존재 시 downgrade 거부, (8) AST 테스트(허용 위치 밖 직접 접근 금지) 및 기존 전체 lock 회귀.
**E2:** 획득 경합(Task 획득 vs Run 획득 → 정확히 하나), 획득 중 epoch 변경 → 재측정, 늦은 종료자가 새 owner를 풀지 못함(CAS·expected owner), 취소 vs 종료 vs reconcile 직렬화, startup recovery(고아 Run → 종료 확인 후 해제 / 확인 불가 → `RECOVERY_REQUIRED`·owner 유지·Task BLOCKED), spawn 직전/직후 crash(`NOT_LAUNCHED` vs `LAUNCHING` without pid), `stop()` drain(종료 미확인 → `RunnerStopIncomplete`), heartbeat 실패, in-flight Run이 reconcile 대상이 아님, 해제 후 CLEAN/DIRTY 판정(변경 없음/tracked 변경/미추적 비ignored 산출물/측정 불가), `accept_baseline` 미호출, DIRTY gate(`continue`/`abort`)와 `TEST_RUN_PROCESS_UNCONFIRMED` gate(옵션별 effect), approvals 일반화 up/down 및 Task gate 회귀.
**E3:** 멱등 표(5행 모두 + 동시 동일 키), 409 매트릭스, 취소 3경우, 명령·인자가 body로 전달되지 않음, 진단이 Test Run을 시작하지 않음, 응답 크기, `TEST_OUTPUT_IGNORED`.
**실행하지 않는 것:** 실제 JDK/Maven/Gradle 빌드, 실제 CLI E2E(환경 없으면 미검증으로 보고). 프로세스 종료 테스트는 대용 자식 프로세스로 하며 OS 수준 정지(커널 대기)는 모사하지 않는다.

## 14. 승인이 필요한 결정

| ID | 결정 | 권고 |
|---|---|---|
| E-1 | 모델: (a) owner 3상태 + 병렬 Run 기계(본안) vs **(d) test-only Task** | 독립 재검토에서 (a)와 (d)의 비용을 비교해 주십시오. (d)는 §7~§9 병렬 기계를 재사용으로 대체하나 Task 실행 모드 모델을 도입 |
| E-2 | 비 git(CONTENT) workspace | 거부 |
| E-3 | 대기열 | 없음. 획득 시 BUSY면 즉시 종결 |
| E-4 | `declare_gone`(운영자 attestation 후 DIRTY 해제)을 허용할지 | 허용(자동 CLEAN 없음, 근거 기록). 불허 시 종료 미확인 Run은 영구 `RECOVERY_REQUIRED` |
| E-5 | `workspace_relation`에 `TEST_RUN_OWNS` 추가 | 추가 |
| E-6 | `approvals` 일반화(§9.2) 수용 | 필요(Test Run만 쓰는 Project의 DIRTY 해소·종료 미확인 gate) |

## 15. 알려진 한계

- 단일 Runner(advisory lock) 전제. 다중 Runner는 범위 밖이다.
- OS 수준에서 멈춘 프로세스(커널 대기 등)는 강제 종료할 수 없다. 이 경우 owner를 유지하고 사람에게 묻는다(해제하지 않음). 이는 안전 쪽 실패이며 가용성은 낮아진다.
- 환경변수 마커 기반 고아 탐지는 보조 수단이며 권한 부족 시 "없음"을 증명하지 못한다(→ `RECOVERY_REQUIRED`).
- fingerprint는 백업이 아니다. 테스트가 바꾼 파일을 복구하지 않는다.

## 부록 A. v1 리뷰 지적 대응표

| v1 지적 | v2 대응 |
|---|---|
| B1 R1 대책 무력 | §4.2: `OwnerView` 3상태 API, 직접 접근 AST 금지(허용 목록 축소), proof에 owner 포함 |
| B2 단계·migration 모순 | §12: E1/E2/E3 분리, 0005 순서·downgrade 명시, `use_alter` |
| B3 프로세스 생존 미확인 해제 | §8: 종료 확인(P1/P2) 전 해제 금지, `RECOVERY_REQUIRED`·Human Gate, RECOVERY 우회 금지 |
| M1 read-only step 무지 | §4.3 |
| M2 Test Run 전용 DIRTY 막다른 길 | §9.1 DIRTY gate(Run 대상), §10 `TEST_OUTPUT_IGNORED`·`changed_paths` |
| M3 Runner 연동 미명세 | §7.3: `_dispatch_test_runs`, `_test_inflight`, heartbeat, startup recovery, stop/drain, `ALREADY_ACTIVE`, REQUESTED는 lock 미보유 |
| M4 멱등 모순 | §7.1 표(키 조회 우선) |
| M5 취소·해제 직렬화 | §6: Project→Run 잠금 순서, CAS, `release_owner(expected)`, REQUESTED 취소는 lock·측정 없음 |
| M6 Changes/Diagnostics kind | §10 |
| M7 요청 경로 측정 | §7.1 DB 전용, §7.2 Runner가 측정 |
| m1~m8 | `owned_by_finished_tasks`는 Run용 별도 쿼리(§7.3 recovery로 대체), 복합 FK 비대칭 명시(§4.1), archive에 Run 포함(§11), `test_policy` NULL 검증(§7.1), REQUESTED `expires_at`(§5·§6), `set_run_owner`는 accepted baseline 미설정(§4.2) |

## 부록 B. v2 독립 재검토 결과 (2차, 코드 대조)

판정: **재작업.** 재검토자는 (a)보다 **(d) test-only Task**를 권고했다(근거: (a)의 병렬 기계는 약 1500~2500줄 + 대규모 테스트이고 Human Gate·lock 소유·Runner 수명주기라는 가장 보수적이어야 할 영역을 건드림. (d)는 약 300~600줄). 결정은 사용자 몫이며 본 부록은 지적의 기록이다.

### BLOCKER
- **B1 §9.2 gate 일반화가 서술보다 훨씬 큼 + 잠금 순서 역전.** `HumanGateService._decide_once`가 전 구간 Task 전제(`:159` Task lock, `:160` `current_basis`, `:183` effect에 task, `:185-186` Task용 `ExecutionRequest`, `_prepare :133`). `ApprovalOut.task_id` 필수(`schemas/api.py ~195`)라 Run gate 조회는 500. 기존 decide는 approval→Task→Project 순이라 Run에 유추하면 approval→Run→Project로 §6(Project→Run)과 역전, `declare_gone`/`retry_termination`과 `finish_run`/`expire_run` 사이 **교착**. Run 전용 decide 경로(Project 먼저), Run용 basis·응답 스키마 필요.
- **B3 이유 코드 `PROJECT_DIRTY` 공유.** `RecoveryEffects`가 `reason_code`만으로 조회해 Run 승인이 Task effect로 가서 `task_id=None`에서 실패. 별도 코드(`TEST_RUN_PROJECT_DIRTY`) 또는 target kind 분기 필요. unique index도 `task_id`뿐.
- **B2 "종료 확인 전 해제 없음"이 현재 코드로는 강제 불가.**
  - P2는 루트 프로세스만 증명: `terminate_tree`는 `ALREADY_EXITED`면 조기 반환(`process_control.py:480-483`), 자손은 살아 있는 루트에서만 수집(`:486`). 정상 종료 시 `command_executor`는 종료 호출을 하지 않고(`:223-225`) `terminated_state` 값이 없다. Gradle daemon·surefire fork·백그라운드 손자가 남아도 P2가 성립. Windows Job Object 없음(`CREATE_NEW_PROCESS_GROUP`만), POSIX는 `killpg` 미사용. Task에도 같은 한계가 있다.
  - 코루틴 취소 시 자식 누수(`command_executor.py:207-221`), `on_started` 미보호(`:178-179`).
  - **오케스트레이터 TEST 명령은 pid·identity를 기록하지 않는다**(`test_policy_service.py:139`, `Execution`은 종료 후에만 삽입 `workflow_service.py ~776`, STARTING-before-spawn은 에이전트 CLI 전용 `step_runner.py:205-213`). `run_key`도 없어 취소·`_cancel_cli`가 닿지 않음. 이 구멍은 (a),(d) 공통이고 선행 작업이다.
  - `ORCH_TEST_RUN_ID` 환경변수 탐지는 Windows에서 `AccessDenied`로 사실상 증거가 되지 못함 → 폐기, `LAUNCHING`+pid 없음은 곧바로 gate.

### MAJOR
M1 `LAUNCHING`→`NOT_LAUNCHED` 전이 부재(실행 파일/JDK 없음 같은 흔한 실패가 항상 RECOVERY_REQUIRED), `spawn_failed` 플래그 필요, 빨리 끝난 프로세스는 `capture_identity`가 None / M2 `retry_termination`·`declare_gone`의 OS 작업이 API 프로세스에서 실행됨(ALREADY_ACTIVE 인스턴스 가능) → effect는 요청만 기록하고 Runner가 수행 / M3 승인 24h 만료 후 RECOVERY_REQUIRED 출구와 취소 의미 없음 / M4 AST 규칙 약함(keyword arg, dict 키, `text()` SQL, `write_owner_kind`·`write_baseline_fingerprint` 포함 필요) / M5 완결된 Run(+2 epoch)이 read-only step 사이에 끼면 epoch 변화로만 보여 §4.3 규칙이 발동하지 않음 → 의미 명시 필요 / M6 POST가 archive와 경쟁(Task 생성은 project row lock 사용), "새 키+비종결 Run → 409"는 check-then-insert라 동시 POST가 두 개 생성 → 부분 unique 또는 row lock / M7 recovery는 `_recovery_body`(트랜잭션 안)가 아니라 `_recover_rest`에 두어야 하고, ACQUIRED commit 전에 `_test_inflight` 등록, `_drain`·`in_flight`·`/health`·`_settle_stop`도 `_test_inflight` 인지 / M8 취소 전달 수단 없음(Task의 `_watch_cancel` 상당 + executor `run_key`) / M9 E1 "기존 테스트 불변"은 거짓(`test_project_lock.py:81,128,511,586`이 `write_owner_task_id`를 직접 씀).

### MINOR
`declare_gone`은 비어 있지 않은 `rationale` 필수 / `EXPIRED` 의미 충돌·`FAILED(PROJECT_BUSY)`는 reason code로 / 0005 downgrade가 종결 이력을 조용히 삭제 / 취소 URI는 기존 `cancellation-requests` 명명과 일치시킬 것 / acquire에서 Run CAS 0행이면 owner set까지 전체 롤백 / env marker는 `CommandSpec`에 파라미터 필요(폐기됨).

### (d)에 대한 재검토자 평가
- 필요: `tasks.kind`(DEVELOPMENT|TEST_ONLY)와 상태 간선, `create_test_task`, `WorkflowService._do_test_only`(acquire→mutating begin→`tests.run`→`Execution`+identity→test transition, 에이전트 호출 없음), **`release_on_terminal`이 TEST_ONLY의 COMPLETED를 accepted baseline으로 받아들이지 않도록** 분기 1개(현재 COMPLETED+유효 proof는 CLEAN+최종 fingerprint 수락이라 빌드 부산물이 조용히 수락됨), 위 B2 보강(공통), 소유 중일 때 무한 BLOCKED 대신 fail-fast(E-3).
- 공짜로 얻는 것: owner=Task 유지, PROJECT_DIRTY gate·Task abort, `reconcile`, `task_baselines`/Changes, 취소, 하트비트·lease takeover, Runner stop/drain.
- 비용: Task 목록 노이즈, `completion_checker`/`report_service`의 plan 가정, 후속 "모드" 설계와의 겹침, 완료 의미(테스트 실패 ≠ Task FAILED; COMPLETED + `output.passed=false`).
- 사실 보정: Task `abort`는 이미 CLI 사망 증명 없이 DIRTY로 owner를 해제하므로(`human_gate_service.py:196-200`), `declare_gone`은 기존 Task abort와 동급이며 약화가 아니다(본문 §8의 엄격성 서술이 과장).
