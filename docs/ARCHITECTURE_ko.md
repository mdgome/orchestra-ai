# 아키텍처 및 의사결정 기록

> English: [ARCHITECTURE.md](ARCHITECTURE.md)

## 계층 구조(DDD를 사용하지 않는 Layered Architecture)

```
routers/       HTTP 처리 전용: 파싱, 단일 서비스 메서드 호출, 상태 코드 매핑
schemas/       요청/응답 형식(비즈니스 규칙 없음)
validation/    policies.py(모든 제한값), validators.py, exceptions.py, error_handlers.py
services/      워크플로, Step Runner, Lease, Human Gate, Session, Test Policy, Runner, Task Service
repositories/  SQLAlchemy 쿼리. commit하지 않음(트랜잭션은 서비스가 소유). session_store는 작은 Protocol
providers/     Claude/Codex 어댑터, 역할별 권한 매핑, 사전 점검, Mock
infra/         명령 실행기, 프로세스 제어, 작업공간 보호/스냅샷, 이벤트 버스
```

추상화는 두 번째 구현체가 실제로 존재하는 경우에만 사용합니다. 해당되는 것은 `CliProvider`(Codex, Claude, Mock)와
`SessionStore`(현재 PostgreSQL, 향후 읽기 캐시)입니다. Generic Repository와 Domain Layer는 사용하지 않습니다.
`tests/architecture`는 *명백한* 위반을 기계적으로 검사하며, 의미론적 검토는 Code Reviewer가 담당합니다.

## 상태 머신

* **Task**: `PENDING, PLANNING, DESIGN_REVIEWING, DEVELOPING, TESTING, REVIEWING, RETRYING, WAITING_APPROVAL,
  COMPLETED, FAILED, CANCELLED`. 상태 변경은 `state_service.change_task_state`를 통해 수행합니다. 변경 내용은
  검증 후 `state_transitions`에 추가되며, 데이터베이스 트리거가 해당 테이블을 append-only로 유지합니다.
* **Step**: `PENDING, RUNNING, WAITING_APPROVAL, SUCCEEDED, FAILED, INTERRUPTED, NEEDS_MANUAL_RECOVERY, CANCELLED`.
  사람의 결정을 기다리는 상태는 실패가 아닙니다.
* 세 카운터는 독립적으로 관리합니다. `step_attempt`는 기술적 재시도, `workflow_iteration`은 개선 루프(최대 3회),
  `plan_revision`/`replan_count`는 재계획을 나타냅니다. Human Gate 대기는 어떤 카운터도 증가시키지 않습니다.

## Project Profile

* `project_profiles`는 불변 버전을 보관하고(trigger가 UPDATE/DELETE를 거부), `projects.profile_version`은 활성 포인터이며 복합
  FK이므로 같은 project의 버전만 가리킬 수 있습니다. Task는 버전, hash, 내용을 복사합니다(`tasks.profile_*`: 모두 NULL이거나,
  project와 64자 hash가 있는 상태로 모두 존재해야 함. CHECK로 강제).
* Profile은 프롬프트를 조정하는 데이터입니다. 오케스트레이터 메커닉(`prompts.MECHANICS`)은 결코 포함되지 않으며, 오케스트레이터
  자체의 개발 규칙은 AGENTS.md / CLAUDE.md에 남습니다. 권한은 role과 test preset만의 함수이고(`providers/permissions.py`),
  `validate_profile`은 schema 밖의 모든 key를 거부합니다.
* Profile이 없는 Task는 내장 `RULES`와 원래 hash를 바이트 단위로 유지합니다(hash가 NULL이면 `profile_part` /
  `profile_criteria_of`는 아무것도 더하지 않음). `legacy` v1은 `RULES`와 똑같이 렌더되고 `neutral` v1은 아키텍처를 강제하지
  않습니다.
* `completion_criteria`: 리뷰어가 저장한 평가는 `completion_checker.profile_criteria_problems`가 추가 조건으로 결정적으로
  검사합니다. criterion 충족 여부 자체는 리뷰어의 판단으로 남습니다.

## 산출물과 보고서 조회

* 새 테이블은 없습니다. 산출물은 `task_steps` 행(`artifact_id` = step id)이며, 보고서는 `ArtifactRepository`를 통해 step,
  execution, approval, transition을 읽습니다(manifest는 defer되어 로드하지 않음). `ReportService`가 payload를 만들고
  `schemas/reports.py`가 검증하며 크기 계약을 적용합니다. LLM 호출도, 쓰기도 없습니다.
* `completion_checker.evaluate_definition_of_done`은 모든 조건을 각자의 판정과 함께 반환하며 `check_definition_of_done`의
  유일한 근거입니다(동작 불변). 보고서의 `current_re_evaluation`은 아무것도 결정하지 않고 이를 재사용합니다. 저장된 COMPLETED
  전이는 `historical_completion`(상세 UNKNOWN)으로 따로 보고합니다.
* 마스킹 primitive(`session_fingerprint`, 알려진 id 치환)는 `infra/redaction.py`에 있으며 provider와 응답 sanitizer가 함께
  씁니다(`infra`는 `providers`를 import하지 않고, `providers.base.redact`는 얇은 wrapper).
* 응답마다의 순서: 올바른 타입의 정화된 값으로 payload 구성 -> strict 검증 -> 크기 -> schema가 예약한 필드(`sanitization`,
  `omitted_items`, `omitted_sections`, `size_limited`, `SanitizedJson.reasons`)로만 재절단 -> 다시 strict 검증 -> 크기
  (최대 `RESPONSE_SHRINK_MAX_ROUNDS`회) -> 제한된 fallback -> 명시적 오류. `validate_response_limits()`는 답 하나도 담지 못하는
  한도로는 시작하지 않게 합니다.

## 런타임 상태와 진단

* `runtime_status.runtime_snapshot(lifecycle, startup_outcome, recovery_unsettled, state=None, *, background_degraded=False,
  supervisor_degraded=False, components=())`가 `/health`의 유일한 판정입니다. `state`는 백그라운드 입력 묶음
  (`RunnerRuntimeState`: supervisor 상태, degraded component, sweeper stall과 설정 상태)이며, snapshot은 `warnings`,
  `runner.background_degraded`, `runner.sweeper`, `SUPERVISOR_DEGRADED` blocker를 추가합니다.
  `runner_active`와 `ready`는 `Runner.lifecycle`에서 계산합니다. Runner가 시작되지 않은 이유(preflight 차단, advisory lock
  점유, `start()` 실패)는 lifecycle에 보이지 않으므로 lifespan이 `StartupOutcome`을 기록합니다. `START_FAILED`는 예약
  상태입니다. `Runner.start`의 일반 예외는 지금도 lifespan을 중단시킵니다(결정 D6-a).
* `diagnostics_service`는 읽기 전용입니다(도구 버전 확인과 파일 읽기만; 에이전트, 인증 프로브, 설치 프로그램, 프로젝트
  테스트는 실행하지 않음). cache key마다 공유 `asyncio.Task` 하나를 짧은 `asyncio.Lock` 아래에서 만들며, 이 lock은 진단이
  실행되는 동안 잡지 않습니다. 완전한 결과는 task가 끝나기 전에 캐시되고(부분 결과, 즉 UNKNOWN 항목은 반환하되 캐시하지 않음) done callback은 자기 task만
  제거합니다. waiter는
  shield되어 취소된 waiter가 진단을 취소하지 않습니다.
* subprocess는 executor의 timeout으로 제한되고 종료됩니다. 파일 점검은 worker thread를 쓰며 timeout은 **cooperative/
  best-effort**입니다. thread는 강제 종료할 수 없으므로 Project당 점검 1개와 전역 상한이 worker 수를 제한하고, 슬롯은
  thread가 실제로 끝날 때만 반납됩니다. 전체 실행은 `DIAGNOSTICS_DEADLINE_SECONDS`로 cooperative하게 제한되므로(각 단계는
  자신의 timeout과 남은 시간 중 작은 값을 받음) blocking 파일시스템 thread가 남을 수 있습니다.
* 등록된 Project는 자신의 root부터 검증합니다(`resolve_project_root` 후 `resolve_inside_root`). `WORKSPACE_ROOT` 기준의
  `WorkspaceGuard`는 사용하지 않습니다. 끼워 넣은 링크/junction, 바뀐 canonical path, 사라지거나 접근 불가한 root는
  탐지하지만, 같은 canonical path의 실제 디렉터리가 다른 디렉터리로 교체된 경우는 탐지하지 않습니다(저장된 filesystem
  identity가 필요함).

## Lease 및 인계(`lease_service`)

Task마다 하나의 RUNNING `task_runs` 행만 허용하며 부분 Unique Index로 보장합니다. 만료된 Lease의 인계는 세 단계로
수행합니다. 짧은 트랜잭션에서 이전 Run을 읽고, **트랜잭션 없이** OS 프로세스를 검사·종료한 다음, 다시 짧은
트랜잭션에서 Task를 잠그고 동일한 Run과 버전인지 재검증한 뒤 Run을 교체합니다. 이전 CLI가 종료되었음을 입증할 수
없는 경우(접근 거부, 여전히 실행 중, 신원 불일치 또는 PID를 알 수 없는 `STARTING` Execution) 인계 결과는
`UNCERTAIN`이 됩니다. 이 경우 아무 작업도 실행하지 않고 Task를 `RECOVERY` Gate에 대기시킵니다. untracked 프로세스
(에이전트 CLI 호출)는 PID 단독이 아니라 `(pid, 생성 시각)`으로 식별합니다. **tracked** execution 행(Job Object로 실행한
테스트 명령)은 다르게 판정합니다. Job(`JOB_EMPTY`) 또는 kill-on-close 가정(소유 Runner 프로세스가 사라졌음이 입증되고, root가
사라졌고, 생존 프로세스가 없음)으로 판정합니다. pid가 없는 tracked 행은 입증되지 않은 spawn 증거가 없고 같은 Runner
프로세스에 보유된 Job도 없을 때에만(`retained_job_verdict`) `NEVER_RAN`으로 닫히며, 그렇지 않으면
`SPAWNED_PROCESS_UNPROVEN`입니다. 세부 내용: `docs/workflow/07-lease_ko.md`.

## Tracked execution과 백그라운드 unit

* `TrackedCommandSession`(`step_runner`)은 테스트 명령을 spawn하기 전에 tracked `executions` 행(`detail.tracked`)을 등록하고
  fence된 식별과 닫는 proof를 기록합니다. `process_control.TreeJob`은 Job 추상화이고 `windows_job.WindowsJob`은 그 Windows
  Job Object 구현입니다(`KILL_ON_JOB_CLOSE`, 프로세스를 suspended로 spawn하고, 할당하고, 식별하고, 저장한 뒤 resume).
  `CommandExecutor`는 비었음이 입증되지 않은 트리의 Job을 보유합니다.
* `BackgroundSupervisor`는 프로세스 내부 unit인 `OrphanSweeper`(미해결·anomaly tracked 행을 정리), `ProcessRiskWorker`
  (`PROJECT_DIRTY` gate의 위험 옵션을 검증), capability 갱신을 소유합니다. unit 실패는 budget 안에서 재시작하며, degraded된
  supervisor만 dispatch를 막습니다. PostgreSQL이 계속 Source of Truth입니다.
* migration `0005`는 `runtime_capabilities`, `process_risk_requests`, 부분 index `ix_executions_tracked_unresolved`와
  `ix_executions_tracked_anomaly`, `uq_approvals_lineage_successor`(approval당 후속 gate는 최대 하나)를 추가합니다.
* 세부 내용: `docs/workflow/03`, `07`, `08`, `09`.

## 실행 원칙(`step_runner`)

`짧은 트랜잭션(Lease 검증, STARTING 기록)` → 트랜잭션 밖에서 CLI 실행 → `짧은 트랜잭션(Lease 검증, 결과 기록)`
순서로 처리합니다. Lease 소유권을 잃은 후 도착한 결과는 `discarded_result`로만 저장하며 Step은 `INTERRUPTED`가
됩니다.

## Checkpoint

SUCCEEDED Step은 `input_hash`가 변경되지 않았고 `stable_hash(output) == output_hash`인 경우에만 재사용합니다.
`WorkspaceSnapshot`은 tracked, modified, untracked 파일의 경로와 내용 hash를 기록하며 `reports/`, 캐시 등의 변동성
산출물은 별도로 분리합니다. 스냅샷은 중단된 Step을 다시 실행해도 되는지 판단하는 용도로만 사용합니다. 이는 변경
감지 수단이며 파일을 복원할 수 없습니다. Tester 결과는 요구사항, 코드 fingerprint 및 Test Policy hash가 모두 같을
때만 재사용합니다. Code Reviewer에는 항상 현재 코드에서 생성된 테스트 결과만 전달합니다.

## Human Gate

`DESIGN`, `RECOVERY`, `PERMISSION`은 서로 분리되어 있습니다. DESIGN 승인은 CLI 권한을 확대하지 않습니다. 결정은
행 잠금 아래에서 처리합니다. 이전 결정과 동일하면 기존 결과를 먼저 반환하고(200), 다른 결정이면 충돌로 처리합니다
(409). 만료 여부(410)와 basis 변경 여부(409)는 *새로운* 결정에 대해서만 검사합니다. 결정과 RESUME 요청은 하나의
트랜잭션에서 함께 commit합니다. PERMISSION 승인은 하나의 Execution과 정확한 명령 fingerprint에 결속되며
`used_at`을 이용해 원자적으로 소진합니다. CLI가 일회성 권한을 실제로 강제할 수 있을 때만 승인된 권한을 사용합니다
(Claude의 정확한 패턴이 지정된 `--allowedTools`). 강제할 수 없다면 실행을 보류하고 권한을 확대하지 않습니다.

## Session

PostgreSQL이 유일한 Source of Truth입니다. `conversations`에는 메타데이터만 저장하고, `agent_sessions`는 다섯 개의
독립 역할에 대해 `UNIQUE(task_id, agent_role)`과 compare-and-set을 위한 `version`을 사용합니다. 메시지 본문은
`messages`에 저장되며 다른 테이블에서 참조하지 않으므로 보관 정책은 해당 행만 삭제합니다. Native ID는 API로
반환하지 않고 로그나 감사 이벤트에도 기록하지 않습니다. 이벤트에는 되돌릴 수 없는 `sess:xxxxxxxx` 태그만 기록합니다.
CLI가 세션이 존재하지 않는다고 명시적으로 보고한 경우에만 Session을 무효화합니다. Timeout과 일시적 오류는 Session을
무효화하지 않습니다.

## Project, 병렬 실행 및 write lock

* `projects`는 Task보다 상위의 실행 단위입니다(`tasks.project_id`, 기존 Task는 `NULL`로 그대로 동작). Project Workspace는
  `WORKSPACE_ROOT` 밖의 임의 절대 경로일 수 있으며, 등록과 **매 실행 전**에 절대 경로·symlink/junction 부재·접근
  권한을 검증하고 시스템 경로, 오케스트레이터 경로, 다른 Project와의 중복·중첩을 거부합니다.
* `Runner`는 한 프로세스에서 Task를 병렬 실행합니다. 슬롯을 먼저 확인한 뒤 claim하고, claim마다 `claim_token`을 발급해
  뒤늦은 이전 coroutine을 fence합니다. 지연(backoff)은 `execution_requests.defer_count/not_before`로 영속화됩니다.
  전역 advisory lock은 Runner 프로세스 단일화 용도로 유지합니다.
* 같은 Project의 변경 Step(Developer, Tester)과 Code Review는 `projects.write_owner_task_id`를 가진 Task 하나만
  실행합니다. 순서는 `claim → Task Lease → Project gate → Step attempt → CLI`이며 terminal 전이와 같은 트랜잭션에서
  `project_terminal.release_on_terminal`이 lock과 write baseline을 정리합니다. `workspace_state`는 `CLEAN` 또는
  `DIRTY_UNRESOLVED`이고, 증명할 수 없으면 절대 `CLEAN`이 되지 않습니다. 해결 경로는 `PROJECT_DIRTY` RECOVERY Gate의
  `continue`이며, `continue`는 Project에 미해결 tracked execution도 anomaly tracked execution도 없을 때에만 제공됩니다(그
  외에는 그 gate의 위험 옵션이 적용되고, 해소할 수 없는 행이 있으면 `abort`만 남습니다).
* 측정이 필요한 전이는 트랜잭션 밖에서 `FingerprintProof`(`WORKSPACE_FINGERPRINT_V1`)를 측정하고, 짧은 트랜잭션에서
  Project 행 잠금 아래 owner·`write_epoch`를 검증합니다. stale이면 롤백 후 재측정(최대 3회), 이후 `UNMEASURED` 종료입니다.
  행 잠금을 잡은 채 fingerprint를 계산하지 않습니다.
* Test Policy preset(pytest/Maven/Gradle)은 Task 생성 시 하나의 명령으로 해석되어 Task에 복사됩니다. 결과 파일은 삭제하지
  않고 이번 실행에서 새로 생기거나 바뀐 파일만 사용하며, 테스트 프로세스에는 CLI 인증 변수가 전달되지 않습니다.
* 상세한 흐름·Mermaid·불변 조건은 `docs/workflow/`(특히 03, 07, 08, 09, 10, 11)에 있습니다.

## Task baseline과 변경 내역

* `task_baselines`(migration 0004): Task당 한 행이며 `ProjectLockService`가 `set_owner`와 **같은 트랜잭션**에서 기록합니다
  (`ON CONFLICT (task_id) DO NOTHING` 후 실제 행을 identity로 비교: 같은 acquisition이면 idempotent, 다르면 fail closed하고 owner도
  rollback). AVAILABLE 행은 gzip manifest와 HEAD를, UNAVAILABLE 행은 이유만 담고 payload는 없습니다. CHECK는 NULL-safe
  (`... IS TRUE`)합니다. CHECK는 NULL이면 통과하므로 NULL 항이 섞인 단순 conjunction은 잘못된 행을 허용했을 것입니다.
* 두 결과를 분리합니다(`LockMeasurement`): fingerprint proof(fail closed, 변경 없음)와 보조 manifest(실패 = UNAVAILABLE 행, Task는
  계속). `WORKSPACE_FINGERPRINT_V1`과 `STEP_SNAPSHOT_V1`은 건드리지 않으며, manifest는 자체 walk, 한도(비압축 바이트, 파일 수,
  경로·target 길이), bounded 압축 해제를 가진 별도의 `CHANGES_MANIFEST_V1` schema입니다.
* `ChangeService`는 읽기 전용입니다(git: `rev-parse`, `ls-files`, `status`, `cat-file`). `Deadline` 하나를 측정 코드에 전달합니다
  (git은 `min(기본, 남은 시간)`, walk/hash는 entry·chunk 사이 확인). filesystem에서는 cooperative이며 OS 호출이 멈추는 경우를
  막아 주지 않습니다. `MeasurementGate`가 Project당·전체 worker 수를 제한하고 worker가 끝날 때 슬롯을 반납하며, timeout은
  `verify_workspace`의 cooldown을 엽니다.
* `lock_consistency`(DB, 전후 비교)와 `workspace_consistency`(파일, 측정 두 번)는 별개 필드입니다.

## 의사결정 기록

| 결정 | 이유 |
|---|---|
| Prompt는 stdin으로 전달하고 argv list만 사용 | Shell을 사용하지 않으며 인자 길이와 quoting 문제가 없음. 두 CLI가 지원하는 것을 검증함 |
| `psutil` 추가 | 플랫폼 간 프로세스 신원 확인 및 프로세스 트리 종료(Human Gate에서 승인됨) |
| 테스트에 내장 PostgreSQL 사용(`pixeltable-pgserver`) | 개발 시스템에 Docker daemon이나 PostgreSQL 설치가 필요 없음(Human Gate에서 승인됨). `pgserver` 자체는 Python 3.14 빌드가 없음 |
| 모든 텍스트에 UTF-8 사용 | Windows 기본 인코딩은 cp949임 |
| 동사형 URI를 `execution-requests`/`cancellation-requests`/`decisions`로 교체 | URI에는 명사를 사용한다는 프로젝트 규칙 준수 |
| Tester가 아니라 Orchestrator가 테스트 실행 | 성공 여부는 에이전트 설명이 아닌 종료 코드와 JUnit 결과로 판단해야 함 |
| Planner 출력은 에이전트가 파일로 쓰지 않고 CLI 결과에서 추출 | Plan Mode에는 쓰기 도구가 전혀 없음 |

## 검증된 항목과 검증되지 않은 항목

실제로 설치된 CLI(Codex 0.146.0, Claude Code 2.1.218, Windows 11)를 사용해 다음을 검증했습니다. JSON/JSONL 출력
형식과 Session ID 위치, `resume` 동작, 정확한 "session does not exist" 오류, 두 CLI의 구조화된 출력, 사용하는 모든
플래그의 `--help` 지원 여부, Plan Mode와 `-s read-only`의 파일 쓰기 거부, stdin을 통한 Prompt 전달입니다.

**검증되지 않은 항목 및 알려진 제한 사항**

* Codex의 승인 요청 JSON 이벤트는 실제로 관찰되지 않았습니다(`approval_policy="never"`에서는 이벤트가 발생하지
  않음). 어댑터는 이벤트 유형에 "approval"이 포함되면 명시적 권한 요청으로 처리합니다.
* Read-only 강제는 파일이 생성되지 않는 동작을 통해 확인했습니다. Windows Codex Sandbox가 OS 수준의 격리 장벽인지
  여부는 **검증하지 않았으므로** CLI 정책으로 취급해야 합니다.
* `Edit(tests/**)`와 같은 Claude 경로 규칙에 의존하지 않습니다. Orchestrator가 Tester의 변경사항을 다시 검사합니다.
* Windows의 프로세스 종료는 단위 테스트에서 프로세스 트리 종료와 신원 불일치를 검증했습니다. `ACCESS_DENIED` 동작은
  실제 재현이 아니라 시뮬레이션으로 검증했습니다. tracked execution(Windows Job Object)은 Windows에서 실제 Job Object
  테스트로 검증했습니다(`tests/unit/test_windows_job.py`, `tests/integration_pg/test_never_ran_crash_points.py`,
  `tests/integration_pg/test_tracked_services.py`). POSIX 정책은 코드에만 있습니다(지원하지 않는 플랫폼은 untracked로
  degrade, 알 수 없거나 사용할 수 없는 capability는 fail-closed). POSIX 런타임, 실제 Gradle/Maven 빌드, 실제 CLI
  end-to-end 실행, 무작위 race stress, EXPLAIN/generic plan, 실제 deadlock 테스트는 검증하지 않았습니다.
* tracked execution의 알려진 제한: Job이 없는 suspended 프로세스(CP2)는 `NEVER_RAN` 종료 뒤에도 살아남을 수 있습니다(설계상
  수용). pid 없는 `SPAWNED_PROCESS_UNPROVEN` 행에는 지원되는 해소 경로가 없습니다(gate는 `accept_unproven_risk`를
  `TARGET_WITHOUT_PID`로 숨기고 직접 제출은 409 `OPTION_NOT_AVAILABLE`이며, `abort`는 Task만 종료하고 `DIRTY_UNRESOLVED`를
  해소하지 않음. `docs/workflow/08-human-gate_ko.md` 참조).
* 부하 테스트는 수행하지 않았으며 Runner(Coordinator) 프로세스는 하나만 지원합니다(병렬성은 한 프로세스 안의 Task 단위).
* 활성 Project Task가 있는 동안의 외부 프로세스 수정은 지원하지 않으며 fingerprint로도 완전 탐지되지 않습니다.
* Maven·Gradle 실제 빌드는 이 환경에 JDK/Maven/Gradle이 없어 **실행하지 못했습니다**(JUnit 해석, wrapper 해석, 권한,
  환경 규칙은 단위 테스트로 검증). Windows에서 파일 symlink를 만들 수 없는 계정에서는 해당 테스트가 skip됩니다.
* 인증이 필요한 private 저장소 clone은 검증하지 않았습니다(clone 보상과 자격 증명 마스킹은 Fake executor로 검증).
