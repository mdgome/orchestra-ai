# 아키텍처 및 의사결정 기록

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

## Lease 및 인계(`lease_service`)

Task마다 하나의 RUNNING `task_runs` 행만 허용하며 부분 Unique Index로 보장합니다. 만료된 Lease의 인계는 세 단계로
수행합니다. 짧은 트랜잭션에서 이전 Run을 읽고, **트랜잭션 없이** OS 프로세스를 검사·종료한 다음, 다시 짧은
트랜잭션에서 Task를 잠그고 동일한 Run과 버전인지 재검증한 뒤 Run을 교체합니다. 이전 CLI가 종료되었음을 입증할 수
없는 경우(접근 거부, 여전히 실행 중, 신원 불일치 또는 PID를 알 수 없는 `STARTING` Execution) 인계 결과는
`UNCERTAIN`이 됩니다. 이 경우 아무 작업도 실행하지 않고 Task를 `RECOVERY` Gate에 대기시킵니다. 프로세스는 PID
단독이 아니라 `(pid, 생성 시각)`으로 식별합니다.

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
  실제 재현이 아니라 시뮬레이션으로 검증했습니다.
* 부하 테스트는 수행하지 않았으며 단일 Runner만 지원합니다.
