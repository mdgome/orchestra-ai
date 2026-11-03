> 영문 정본: [CLAUDE.md](../CLAUDE.md) — 이 파일은 참고 번역본이며 도구 지침 정본은 루트 영문 파일입니다.

# Claude Code — 프로젝트별 지침

공통 아키텍처, 영속성, 워크플로우 및 복구 규칙은 `AGENTS.md`를 따르십시오. 이 파일은 **Claude 전용** 동작만 추가합니다.

## PLANNER — 플랜 모드
- **플랜 모드**(`--permission-mode plan`, 설치된 CLI에서 지원 여부를 확인한 후)로 실행합니다. 프로젝트 조사/읽기는 허용하고; 소스, 테스트 및 구성 파일 쓰기는 차단합니다.
- `requirements`, `design_direction`, `changed_files`, `implementation_steps`, `test_plan`, `acceptance_criteria`, `tradeoffs`를 포함하는 **구조화된 Plan 아티팩트**를 생성합니다.
- Plan을 Orchestrator에 반환합니다; **Plan 아티팩트를 직접 작업 공간(workspace)에 기록하지 마십시오**. Orchestrator가 검증, 저장 및 해시 처리를 수행합니다.
- Codex **DESIGN_REVIEWER**는 구현이 시작되기 전에 현재 리뷰 기준에 대해 PASS를 반환해야 합니다.
- NEEDS_REVISION인 경우, 유효하다면 Planner 자신의 네이티브 세션을 재개(resume)합니다; Codex 피드백을 반영하여 새 Plan을 재검토에 제출합니다. `max_replans`를 준수합니다.
- 중대한 설계 트레이드오프에 사용자의 선택이 필요한 경우 **DESIGN Human Gate**를 요청합니다; 절대 임의로 조용히 결정하지 마십시오.

## DEVELOPER — 구현
- PLANNER와는 **별도의 네이티브 세션**을 사용하며, PostgreSQL 기반 Session Service를 통해 resume ID를 획득합니다.
- 검토된 Plan과 승인된 결정 사항만 구현합니다. 요구사항, Plan 해시 또는 리뷰 기준이 변경된 경우, 중단하고 재검토를 요청합니다.
- 승인된 범위와 기존 계층형 아키텍처(Layered Architecture) 내에서 변경 사항을 유지합니다; DDD나 범용(generic) repository, 불필요한 프레임워크 추가는 금지합니다.
- 예상치 못한 복잡성이나 권한이 필요한 경우, 제약을 우회하는 대신 적절한 Human Gate를 요청합니다.

## TESTER — 테스트
- 독립적인 **TESTER** 네이티브 세션과 제한된 편집/실행 권한을 사용합니다.
- 어서션을 약화시키거나 필수 테스트를 우회하지 않고, 의미 있는 테스트를 추가하고 수정합니다.
- 완료 여부는 Claude가 아니라 Orchestrator가 필수 테스트를 독립적으로 실행하고 결과를 검토하여 결정합니다.

## 세션, 권한 및 복구 규칙
- 네이티브 세션 ID는 해당 **작업(task), 제공자(provider), 역할(role)**에 귀속됩니다; Planner의 세션을 Developer에게, 또는 한 역할의 세션을 다른 역할에 재사용하지 마십시오.
- PostgreSQL의 Checkpoint, Approval 및 Lease 상태는 네이티브 CLI 세션이 기억하는 그 어떤 것보다 우선합니다.
- resume 시도가 세션이 없음을 명확히 보고하는 경우, 저장된 공유 컨텍스트를 사용하는 통제된 신규 세션 경로를 따릅니다. 타임아웃이나 실행 상태가 불확실한 경우에는 무작정 재시도하지 말고 **중단한 뒤 RECOVERY를 사용**하십시오.
- 네이티브 세션 ID, 토큰 또는 비밀 정보를 아티팩트, API 응답 또는 로그에 노출하지 마십시오.
- `--tools`, `--allowedTools`, `--disallowedTools`는 서로 다른 의미를 가집니다: 실행 전에 설치된 Claude CLI preflight로 정확한 동작을 확인하십시오.

## 최종 체크리스트
- Planner가 **작업 공간 코드를 변경하지 않았음**.
- Codex가 **현재 정확한 Plan과 리뷰 기준**을 승인했음.
- Developer와 Tester가 **각자의 세션과 허용된 도구**를 사용했음.
- 필수 테스트가 독립적으로 실행되었고 결과가 정확하게 보고되었음.
