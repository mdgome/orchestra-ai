# Development, Testing 및 Code Review

> 현재 구현 코드를 기준으로 정리한 흐름입니다. 코드로 확인되지 않았거나 검증되지 않은 동작은 현재 구현 제한으로 구분합니다.

이 문서는 두 개의 다이어그램으로 나뉩니다. 첫 번째는 DEVELOPING부터 테스트 결과 판정까지, 두 번째는 REVIEWING부터 종료·재시도·재계획까지입니다. 중심 클래스는 `WorkflowService`(`_do_develop`, `_do_test`, `_do_code_review`, `_review_transition`, `_to_retry`)입니다.

## 1. Development 및 Testing

정상 흐름과 Tester 위반·테스트 실패 분기를 보여줍니다. Tester Agent 실행과 Orchestrator의 독립 테스트 실행은 같은 `TESTING` Step 안에서 순서대로 수행되지만 서로 다른 구역으로 구분합니다.

```mermaid
flowchart TD
    subgraph DEVELOPMENT["1. Development"]
        direction TB
        A(["DEVELOPING"]) --> B{"Design Review PASS 가 현재 Basis 와 일치?"}
        B -->|"Yes"| C["Claude Developer - 별도 Native Session"]
        C --> D{"human_gate_request?"}
    end

    subgraph TESTER["2. Tester 작업"]
        direction TB
        T(["TESTING"]) --> E["Claude Tester - tests 경로 수정과 pytest 만 허용"]
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
- 필수 테스트는 Tester Agent가 아니라 Orchestrator(`TestPolicyService.run`)가 독립적으로 실행합니다. exit code가 0이 아니거나, 실행된 테스트가 0건이거나, 전부 skip이거나, JUnit이 없거나 해석할 수 없으면 PASS가 아닙니다.
- 이전 SUCCEEDED 테스트 결과의 `requirements_hash`, `test_policy_hash`, `code_fingerprint`가 현재와 모두 같으면 Tester를 다시 실행하지 않고 결과를 재사용합니다 ([10](10-invalidation.md)).
- 테스트 약화 신호(필수 테스트 ID 감소 등)는 `test_signals`로 Code Reviewer에게 전달됩니다.
- Gate에서 재개되는 경로는 [08](08-human-gate.md)을 참고하십시오.

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
- Completion Checker는 별도의 Agent가 아닙니다. `WorkflowService._review_transition`이 Code Review PASS를 처리하면서 호출하는 `check_definition_of_done` 함수이며, 검사 조건은 [11](11-task-lifecycle.md)에 있습니다.
- Reviewer에게는 현재 코드(`code_fingerprint`)로 만든 테스트 결과만 전달됩니다. 불일치하면 TESTING부터 다시 수행합니다.
- 다이어그램 1의 `RETRYING`·`REVIEWING` 노드가 이 다이어그램의 진입점입니다.
