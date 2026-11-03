---
name: code-review-test
description: "구현 완료 후 정합성·동시성·상태 전이·트랜잭션·recovery·취소 경계를 코드 리뷰하고, 발견된 결함의 회귀 테스트를 작성하며, 테스트의 결함 검출력과 안정성을 검증하거나 실패 원인(코드·테스트 설계·환경)을 구분할 때 사용한다. 문구·문서만 수정하는 작업, 동작을 바꾸지 않는 formatting, 요청 없는 실제 CLI·유료 E2E 실행, 요청 없는 stage·commit에는 사용하지 않는다."
---

# code-review-test

## Purpose
구현 코드의 정합성·동시성·오류 경계를 리뷰하고, 발견된 위험을 재현하는 테스트를 작성한다. 작성한 테스트가 실제 결함을 검출하는지까지 검증한다.

## Use When
- 구현 완료 후 코드 리뷰가 필요한 경우
- 동시성, 상태 전이, 트랜잭션, recovery, 취소 경계를 검토하는 경우
- 발견된 결함의 회귀 테스트를 작성하는 경우
- 기존 테스트가 실제 요구사항을 검증하는지 확인하는 경우
- 테스트 실패가 코드 결함인지 환경 문제인지 구분하는 경우

## Do Not Use When
- 단순 문구나 문서만 수정하는 작업
- 코드 의미가 바뀌지 않는 순수 formatting
- 사용자 요청 없이 실제 외부 CLI 또는 유료 E2E를 실행하는 작업
- 사용자 요청 없이 stage 또는 commit하는 작업
- 테스트 작성 없이 단순 구현만 요청된 작업에서 테스트 범위가 명시적으로 제외된 경우

## Reference Routing
필요한 reference만 읽는다. 작업 시작 시 일괄 로드하지 않는다.
- [review-checklist.md](references/review-checklist.md): 코드 리뷰를 수행할 때만 읽는다.
- [test-design.md](references/test-design.md): 테스트를 새로 작성하거나 변경할 때 읽는다.
- [test-validation.md](references/test-validation.md): 테스트의 검출력, 결정성, 격리성 또는 flakiness를 검증할 때 읽는다.
- [project-invariants.md](references/project-invariants.md): 프로젝트 고유 상태 전이, lease, claim, recovery, project lock, snapshot 의미와 관련될 때만 읽는다.

읽기 규칙:
- 현재 컨텍스트에 충분한 내용이 이미 있으면 같은 reference를 다시 읽지 않는다.
- reference를 읽지 않았다는 이유로 실제 코드와 테스트 확인을 생략하지 않는다. 최종 기준은 현재 코드와 테스트다.

## 작업 권한 경계
요청 유형별 쓰기 범위가 아래 모든 일반 규칙보다 우선한다.
- **review-only**: 읽기 전용으로 검토한다. production code와 tests를 수정하지 않는다.
- **test-authoring**: 승인된 tests 범위만 수정한다. production code는 수정하지 않는다.
- **fix-and-test**: 구현 수정까지 요청된 경우에만 승인된 production code와 관련 tests를 수정한다.
- **test-validation**: 기존 코드와 테스트를 읽고 실행해 검출력, 결정성, 격리성, 정리 보장을 검증한다. 이 유형만 요청되면 production code, tests, docs, migrations, 설정 파일을 수정하지 않는다. monkeypatch 기반 오류 주입은 테스트 프로세스 내부에서만 하며 파일을 수정하지 않는다. tests 수정은 사용자가 명시적으로 요청한 경우에만 승인된 범위에서 한다. production code 수정이 필요해 보이면 수정하지 않고 발견 사항과 필요한 변경을 보고한다.
- **진단 요청**: 원인을 확인하고 보고한다. 요청 없이 수정하지 않는다.

공통:
- 테스트 통과, 문서 동기화, 정리 편의를 이유로 요청 범위를 자동 확대하지 않는다.
- 문서, migration, 설정 파일은 명시 요청이 있거나 과제 완료에 필수일 때만 최소 범위로 수정한다. 현재 요청 유형에서 쓰기가 허용되지 않으면 수정하지 않고 필요성만 보고한다.
- 요청 없이 git add, commit, reset, clean, checkout, 파일 삭제, 외부 E2E를 수행하지 않는다.

## Core Workflow
1. 요청 유형을 review-only, test-authoring, fix-and-test, test-validation 중 하나로 판정하고 허용된 읽기·쓰기 범위를 확정한다.
2. `git status --porcelain=v1 -uall`과 `git diff --stat`, 필요한 관련 diff를 실행해 기준선을 확보한다. 결과는 작업 컨텍스트에만 두고 파일로 기록하지 않는다.
3. 요청, 계획, 변경 diff, 관련 구현과 기존 테스트를 읽는다.
4. 정상 경로보다 상태 전이, 실패 경계, 취소, 재시작, 중복 실행, 부분 커밋을 우선 리뷰한다.
5. 발견 사항을 심각도와 근거 파일·위치로 정리한다. 발견 사항과 수정 권한은 분리한다. 수정 권한이 없으면 발견 사항과 필요한 회귀 테스트만 보고한다.
6. 수정 권한이 있으면 최소 범위로 수정하고, 결함을 재현하거나 계약을 고정하는 최소 테스트를 작성한다.
7. 테스트가 결함 검출력, 결정성, 격리성, 정리 보장을 갖는지 검증한다.
8. 관련 테스트부터 실행하고 이후 넓은 범위로 확장한다.
9. 코드 실패, 테스트 설계 실패, 환경 실패를 분리한다.
10. 종료 시 같은 git 명령을 다시 실행해 기준선과 비교하고, 이번 작업의 변경만 구분한다.

## Core Constraints
- 현재 구현과 명시된 계약이 최종 기준이다.
- 미확인 추정을 확정된 결함으로 표현하지 않는다.
- 리뷰 발견 사항 없이 임의의 리팩터링을 추가하지 않는다.
- 테스트를 통과시키기 위해 assertion을 약화하지 않는다.
- 실제 동작 대신 private 내부 값만 확인하는 테스트를 우선하지 않는다.
- sleep 길이에 의존하는 동시성 테스트를 작성하지 않는다. Event, barrier, controllable future, monkeypatch 등으로 경합 순서를 결정적으로 구성한다.
- production 코드에 테스트 전용 분기, flag, hook을 추가하지 않는다.
- 테스트는 생성한 task, process, connection, temporary resource를 finally 또는 fixture teardown에서 정리한다.
- 사용자 변경을 되돌리거나 정리하지 않는다.
- 요청 없이 실제 Claude/Codex E2E, 네트워크 호출, 패키지 설치를 수행하지 않는다.

## Report
- 요청 유형과 권한 경계 준수 여부
- 심각도순 리뷰 발견 사항
- 수정한 파일과 수정 이유
- 추가하거나 변경한 테스트와 검증 계약
- 실행한 명령과 통과·실패·skip 수
- 환경 문제로 실행하지 못한 범위
- 남은 위험과 후속 확인 항목
- 사용자 변경 보존 여부
- stage·commit 수행 여부
