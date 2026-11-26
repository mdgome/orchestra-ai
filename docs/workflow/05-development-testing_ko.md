# Development, Testing 및 Code Review

> English: [05-development-testing.md](05-development-testing.md)

> 현재 구현 코드를 기준으로 정리한 흐름입니다. 코드로 확인되지 않았거나 검증되지 않은 동작은 현재 구현 제한으로 구분합니다.

이 문서는 두 개의 다이어그램으로 나뉩니다. 첫 번째는 DEVELOPING부터 테스트 결과 판정까지, 두 번째는 REVIEWING부터 종료·재시도·재계획까지입니다. 중심 클래스는 `WorkflowService`(`_do_develop`, `_do_test`, `_do_code_review`, `_review_transition`, `_to_retry`)입니다.

## 1. Development 및 Testing

정상 흐름과 Tester 위반·테스트 실패 분기를 보여줍니다. Tester Agent 실행과 Orchestrator의 독립 테스트 실행은 같은 `TESTING` Step 안에서 순서대로 수행되지만 서로 다른 구역으로 구분합니다.

```mermaid
flowchart TD
    subgraph DEVELOPMENT["1. Development"]
        direction TB
        A(["DEVELOPING"]) --> B{"Design Review PASS 가 현재 Basis 와 일치?"}
        B -->|"Yes"| PG["프로젝트 Task - ProjectLockService.ensure_for_step - write lock 획득 또는 BLOCKED 또는 PROJECT_DIRTY Gate - Step attempt 생성 전"]
        PG --> C["Claude Developer - 별도 Native Session"]
        C --> D{"human_gate_request?"}
    end

    subgraph TESTER["2. Tester 작업"]
        direction TB
        T(["TESTING"]) --> E["Claude Tester - test preset 별 테스트 경로 수정과 해당 테스트 명령만 허용"]
        E --> F{"Snapshot 차이에 테스트 파일 이외 변경?"}
        F -->|"Yes"| PV["TESTER_MODIFIED_NON_TEST_FILES - FAILED"]
    end

    subgraph ORCH["3. Orchestrator 독립 테스트"]
        direction TB
        G["TestPolicyService.run - 필수 테스트 독립 실행 - exit code 와 JUnit"] --> H{"테스트 PASS?"}
    end

    DR(["DESIGN_REVIEWING 로 복귀"])
    HG["DESIGN Human Gate - checkpoint DEVELOP"]
    HGT["DESIGN Human Gate - checkpoint TEST"]
    RT["RETRYING - 아래 다이어그램 2 로 이어짐"]
    R(["REVIEWING - 다이어그램 2 로 이어짐"])

    B -->|"No"| DR
    D -->|"Yes"| HG
    D -->|"No"| T
    F -->|"No"| TG{"Tester human_gate_request?"}
    TG -->|"Yes"| HGT
    TG -->|"No"| G
    H -->|"No"| RT
    H -->|"Yes"| R
```

- Developer는 유효한 Design Review PASS(현재 Basis와 일치)가 있을 때만 실행합니다. 일치하지 않으면 Development로 가지 않고 DESIGN_REVIEWING으로 되돌아갑니다.
- Tester는 테스트 파일만 수정할 수 있고 `WorkflowService._do_test`가 Snapshot 차이로 이를 검증합니다. 위반하면 `TESTER_MODIFIED_NON_TEST_FILES`로 중단됩니다. 이 검사가 Tester의 Gate 요청보다 먼저 수행됩니다.
- 필수 테스트는 Tester Agent가 아니라 Orchestrator(`TestPolicyService.run`)가 독립적으로 실행합니다. exit code가 0이 아니거나, 실행된 테스트가 0건이거나, 전부 skip이거나, **이번 실행이 만든(신규 또는 변경된) JUnit 결과가 없거나** 해석할 수 없으면 PASS가 아닙니다.
- 프로젝트 Task의 변경 Step(Developer, Tester)과 Code Review는 Project write lock을 가진 Task만 실행합니다. 다른 Task가 보유 중이면 Step attempt를 만들지 않고 요청이 지연 requeue되며(`BLOCKED`), 이전 Task가 미해결 변경을 남겼으면 PROJECT_DIRTY Gate가 먼저 열립니다([08](08-human-gate_ko.md)). Orchestrator 테스트 실행 직전에도 소유 여부를 다시 확인합니다(`assert_owner`).

### Test Policy와 preset

Test Policy는 Orchestrator가 소유하며 Agent가 바꿀 수 없습니다(해시가 모든 Tester 입력에 포함). 우선순위는 Task > Project > 기본값이고, **Task 생성 시 하나의 실행 가능한 명령으로 해석해 Task에 복사**합니다(`test_policy_resolver.resolve_test_policy`, `test_policy_hash`는 해석된 값 기준).

| preset | 명령(정적 인자) | JUnit 결과 | Tester 쓰기 경로 |
|---|---|---|---|
| `pytest`(기본) | `python -m pytest -q --junitxml=reports/junit.xml` | `reports/junit.xml` | `tests/**` |
| `maven` | 프로젝트의 `mvnw`/`mvnw.cmd`, 없으면 시스템 `mvn` + `-B -q test` | `**/target/surefire-reports/TEST-*.xml` | `src/test/**` |
| `gradle` | 프로젝트의 `gradlew`/`gradlew.bat`, 없으면 시스템 `gradle` + `cleanTest test --console=plain --no-build-cache` | `**/build/test-results/test/TEST-*.xml` | `src/test/**` |

- wrapper가 우선이고, 없으면 `PATH`의 도구를 씁니다. 둘 다 없으면 Task 생성이 422로 거부됩니다(워크플로 중간에 실패하지 않음). argv에는 "mvn 또는 mvnw" 같은 대안이 남지 않습니다. wrapper는 Workspace 직속의 일반 파일이어야 하며(링크 거부) POSIX에서는 실행 권한이 필요합니다. `.cmd`/`.bat`은 preset이 해석한 wrapper에만 허용되고 직접 지정한 명령으로는 거부됩니다. 어떤 경우에도 shell 문자열로 실행하지 않습니다.
- `gradle` preset은 먼저 `cleanTest`를 실행하고 빌드 캐시를 끄므로 `test` 태스크가 UP-TO-DATE나 FROM-CACHE가 되지 않고 매 실행마다 XML을 다시 씁니다. `cleanTest`는 Gradle 자신의 테스트 결과 디렉터리만 지우며, 아래 신선도 규칙은 그대로이고 완화되지 않습니다. 해석된 명령이 `test_policy_hash`에 포함되므로, 이 변경 이후 만든 Task는 같은 `gradle` preset이어도 이전과 다른 hash를 가집니다. 이미 만든 Task는 자신에게 복사된 명령을 유지하므로 영향이 없습니다.
- 결과 파일은 **삭제하지 않습니다**(`target/`, `build/`, `reports/`는 사용자 영역). 실행 전 `(경로, size, mtime_ns, sha256)`을 기록하고, 실행 뒤 새로 생기거나 바뀐 파일만 이번 실행의 결과로 합산합니다. 이전 실행의 XML만 있으면 실패합니다.
- 테스트 프로세스에는 CLI 인증 변수(`ANTHROPIC_API_KEY` 등)가 전달되지 않으며, preset이 이름으로 명시한 툴체인 변수(`JAVA_HOME`, `MAVEN_HOME`, `M2_HOME`, `GRADLE_HOME`, `GRADLE_USER_HOME` 중 일부)만 추가됩니다. wildcard는 허용되지 않습니다.
- Tester의 변경 허용 범위(`is_test_path`)와 CLI 도구 권한(`permissions_for`)이 preset에 따라 달라집니다. 빌드 파일과 wrapper(`pom.xml`, `mvnw`, `build.gradle`, `gradlew` ...)는 Tester가 바꿀 수 없습니다(`TESTER_MODIFIED_NON_TEST_FILES`).
- 이전 SUCCEEDED 테스트 결과의 `requirements_hash`, `test_policy_hash`, `code_fingerprint`가 현재와 모두 같으면 Tester를 다시 실행하지 않고 결과를 재사용합니다 ([10](10-invalidation_ko.md)).
- 테스트 약화 신호(필수 테스트 ID 감소 등)는 `test_signals`로 Code Reviewer에게 전달됩니다.
- Gate에서 재개되는 경로는 [08](08-human-gate_ko.md)을 참고하십시오.

## 2. Code Review, Definition of Done 및 개선·재계획

테스트가 통과한 뒤의 Code Review 판정과 그 결과에 따른 COMPLETED / 개선 / 재계획 / FAILED 경로입니다. 최초 실행은 iteration 0이며, 개선은 최대 `max_workflow_iterations`(기본 3)회입니다.

```mermaid
flowchart TD
    subgraph CODEREVIEW["1. Code Review"]
        direction TB
        R(["REVIEWING"]) --> S{"현재 code_fingerprint 로 만든 테스트 결과?"}
        S -->|"Yes"| I["Codex Code Reviewer - read-only"]
        I --> J{"verdict"}
    end

    subgraph DOD["2. Definition of Done"]
        direction TB
        K["Code Review PASS 처리 안의 check_definition_of_done 호출"] --> L{"완료 조건 충족?"}
    end

    subgraph IMPROVE["3. Improvement"]
        direction TB
        RT["RETRYING - Review 실패 처리 또는 테스트 실패"] --> M{"workflow_iteration 이 max_workflow_iterations 미만?"}
        M -->|"Yes - workflow_iteration 증가"| N["DEVELOPING - 이전 테스트 및 리뷰 피드백 전달"]
    end

    subgraph REPLAN["4. Replan"]
        direction TB
        PL["plan_defect - WorkflowService._to_retry replan"] --> PM{"replan_count 가 max_replans 미만?"}
        PM -->|"Yes - replan_count 및 plan_revision 증가"| PLAN(["PLANNING"])
    end

    subgraph TERMINAL["종료 상태"]
        direction TB
        DONE(["COMPLETED"])
        FAIL(["FAILED - MAX_ITERATIONS_EXCEEDED"])
        FAIL2(["FAILED - MAX_REPLANS_EXCEEDED"])
    end

    TST(["TESTING"])
    HG2["DESIGN Human Gate - checkpoint DEVELOP"]

    S -->|"No"| TST
    J -->|"NEEDS_HUMAN_REVIEW"| HG2
    J -->|"PASS"| K
    L -->|"Yes"| DONE
    L -->|"No - verdict 를 FAIL 로 처리"| RT
    J -->|"FAIL"| RT
    J -->|"plan_defect"| PL
    M -->|"No"| FAIL
    PM -->|"No"| FAIL2
```

- 테스트 실패와 Code Review 실패(`FAIL` 또는 DoD 미충족)는 모두 `workflow_iteration`을 증가시키는 개선 반복입니다.
- Code Reviewer가 `plan_defect`를 반환하면 개선 반복이 아니라 재계획 경로로 이동합니다 (`replan_count`와 `plan_revision` 증가, `resume_step_key=PLAN`, `RETRYING`을 거쳐 PLANNING). 한도는 `max_replans`이며 초과하면 `MAX_REPLANS_EXCEEDED`로 FAILED 됩니다.
- Code Review `PASS`라도 CRITICAL issue 또는 `test_policy_concern`이 있으면 `FAIL`로 처리됩니다.
- Completion Checker는 별도의 Agent가 아닙니다. `WorkflowService._review_transition`이 Code Review PASS를 처리하면서 호출하는 `check_definition_of_done` 함수이며, 검사 조건은 [11](11-task-lifecycle_ko.md)에 있습니다.
- Reviewer에게는 현재 코드(`code_fingerprint`)로 만든 테스트 결과만 전달됩니다. 불일치하면 TESTING부터 다시 수행합니다.
- 다이어그램 1의 `RETRYING`·`REVIEWING` 노드가 이 다이어그램의 진입점입니다.

## Profile 완료 기준(completion criteria)

Task의 Profile snapshot이 `completion_criteria`를 정의하면 코드 리뷰 schema에 필수 `profile_criteria`(criterion마다 `{id, met, evidence}`)가 추가되고 저장된 리뷰에 `profile_hash`가 담깁니다. 그러면 `check_definition_of_done`이 **조건 하나를 더**(`profile_criteria_met`) 저장된 평가에 대해 결정적으로 검사합니다. 평가는 현재 Profile의 것이고, 현재 id들을 정확히 한 번씩 담아야 하며, 모든 항목이 `met`이고 `evidence`가 비어 있지 않아야 합니다. 아니면 리뷰는 `FAIL`로 처리되어(`Definition of Done: ...`) 일반 개선 루프가 적용됩니다. criterion이 실제로 충족되었는지는 리뷰어의 판단이며 오케스트레이터는 검증하지 않습니다. 다른 어떤 조건도 약화되지 않습니다.

## Tracked 테스트 명령 (Windows Job Object)

Orchestrator가 `TESTING`에서 직접 실행하는 명령(전후 pytest `--collect-only`와 테스트 실행. `WorkflowService._tracked`가 명령 그룹마다 `TrackedCommandSession`을 만듭니다)은 **tracked command**로 실행됩니다. Planner, Developer, Tester, Reviewer의 Agent CLI 호출은 tracked가 아니며 `(pid, create_time)` 식별을 그대로 사용합니다.

- **Capability gate**(`TrackedCommandSession.check_capability`, 프로세스 실행 전): `SUPPORTED`는 tracked로 실행합니다. `UNSUPPORTED_PLATFORM`(Windows가 아닌 모든 호스트)은 untracked 실행으로 degrade하며 Orchestrator 테스트 Execution에 `tracking = UNTRACKED_PLATFORM`을 기록합니다. `UNKNOWN`과 `UNAVAILABLE`은 `ProcessTrackingUnavailable`을 발생시켜 Task가 `PROCESS_TRACKING_UNAVAILABLE`로 `FAILED`가 되며 명령은 실행되지 않습니다. capability는 Runner 시작 self-test에서 옵니다([03](03-task-runner_ko.md), [11](11-task-lifecycle_ko.md)).
- **등록**: 트랜잭션(Task row lock, Lease 확인, Task가 terminal이 아님, 프로젝트 write lock owner 확인)이 `executions` 행(`outcome=STARTING`, `tracked=true`·`v`·`checkpoint_step_key`·`runner_process`가 든 `detail`)을 삽입합니다. 실행 사실은 새 컬럼이 아니라 `executions.detail`에 둡니다.
- **Spawn 절차**(`TrackedRun`을 쓰는 `CommandExecutor`, `app/infra/windows_job.py`의 `TreeJob`): suspended 상태로 spawn(`CREATE_SUSPENDED`) → kill-on-close가 걸리고 breakaway가 없는 익명·상속 불가 Job Object에 할당 → 식별 캡처 → fenced update로 `pid`와 `process_create_time` 저장(행이 닫혔거나 Lease를 잃으면 `IdentityFenced`: suspended 프로세스를 종료하고 아무것도 실행하지 않음) → primary thread resume. resume 이전의 실패는 `SPAWN_FAILED`이며 명령은 실행되지 않은 것입니다. 인프라 사유(`JOB_CREATE_FAILED`, `JOB_ASSIGN_FAILED`, `IDENTITY_UNAVAILABLE`, `IDENTITY_PERSIST_FAILED`, `IDENTITY_FENCED`, `RESUME_FAILED`)는 Task를 `PROCESS_TRACKING_UNAVAILABLE`로 fail-closed 시키며 테스트 실패로 보고하지 않습니다. resume 전의 취소는 취소이지 테스트 실패가 아닙니다.
- **트리 정리**: 정상 종료 후에는 짧은 시간 동안 Job을 폴링해 일시적 helper를 survivor로 오인하지 않습니다. survivor와 취소·타임아웃된 명령은 pipe drain 전에 `TerminateJobObject`(고정 exit code)로 종료합니다. 트리가 비었다는 증명은 grace 시간 안에 `ActiveProcesses`가 0이 될 때만 성립합니다. 그렇지 않으면 결과는 `TERMINATION_UNCONFIRMED`입니다. Job handle은 이 프로세스에 유지되고, `TerminationUnconfirmed`가 발생하며, Task는 RECOVERY 경로로 갑니다([09](09-recovery_ko.md)). 통과로 해석하지 않습니다.
- **행 닫기**(`TrackedCommandSession`, 보호된 `finally`, Lease에 의존하지 않는 하나의 UPDATE, proof는 `detail`에 JSONB merge): 정상 종료 또는 종료된 명령에 트리가 비었으면 proof `JOB_EMPTY`, `SPAWN_FAILED` 또는 미시작이면 `NEVER_RAN`을 기록합니다. `TERMINATION_UNCONFIRMED`, 결과 누락(`RESULT_MISSING`), lifecycle 누락(`LIFECYCLE_MISSING`)은 행을 **열린 채**(unresolved)로 둡니다. 닫는 쓰기가 실패해도 행은 열린 채로 남고, sweeper와 terminal 규칙이 처리합니다([09](09-recovery_ko.md)).
- `ProcessLifecycle`(`NOT_STARTED`, `STARTING`, `RUNNING`, `EXITED`, `CANCEL_REQUESTED`, `TERMINATING`, `TERMINATED`, `TERMINATION_UNCONFIRMED`, `SPAWN_FAILED`)과 `TreeProof`(`EMPTY`, `NOT_EMPTY`, `UNKNOWN`)는 서술용이며, 해제 결정은 `TreeProof`가 아니라 `detail.proof`를 읽습니다.

## 현재 구현 제한

- tracked command 동작의 검증 범위: Windows에서 실제 Job Object, 실제 자식·손자 프로세스로 **검증됨**(`tests/unit/test_windows_job.py`, `tests/integration_pg/test_never_ran_crash_points.py`, `tests/integration_pg/test_tracked_services.py`). POSIX 정책은 코드에만 존재하며(`UNSUPPORTED_PLATFORM`은 untracked로 degrade, `UNKNOWN`/`UNAVAILABLE`은 fail-closed) POSIX 런타임 동작은 **검증하지 않았습니다**. **검증하지 않은 것**: POSIX 런타임, 실제 Gradle/Maven 빌드, 실제 CLI E2E, 무작위 race stress, EXPLAIN/generic plan, 실제 deadlock 테스트.
- CP2 잔여 위험: 프로세스가 (suspended로) spawn된 뒤 Job에 할당되기 전, 그리고 식별이 기록되기 전에 Runner가 죽으면 행에는 `pid`가 없고 `NEVER_RAN`으로 닫히며, suspended 프로세스는 그 종료 처리 뒤에도 살아남을 수 있습니다. 설계상 수용된 한계입니다(`SUSPENDED_ORPHAN_POSSIBLE`).
- Windows가 아닌 호스트에서는 Orchestrator 테스트 명령이 untracked로 실행되며, 위의 추적 보장은 적용되지 않습니다.
- Maven·Gradle preset은 JUnit 파싱, wrapper 해석(Windows `mvnw.cmd` 포함), 권한·환경 규칙을 단위 테스트로만 검증했습니다. 이 환경에는 JDK/Maven/Gradle이 없어 **실제 Maven·Gradle 빌드는 실행하지 못했습니다.**
- pytest가 아닌 preset에서는 테스트 ID 수집(`collect_ids`)을 하지 않으므로 "테스트 개수 감소" 신호(`removed_test_ids`)가 없습니다. 테스트 파일 변경은 Snapshot 차이로만 보고됩니다.
- 기본 pytest preset의 JUnit 파일(`reports/junit.xml`)은 외부 Workspace에도 남습니다(삭제하지 않는 정책의 결과).
