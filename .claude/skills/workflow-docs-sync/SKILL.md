---
name: workflow-docs-sync
description: "설계나 실행 흐름을 변경하는 코드 작업 후 docs/workflow(Mermaid 포함)를 실제 구현과 동기화할 때 사용한다. 동작을 바꾸지 않는 리팩터링, 포매팅, 주석·테스트 데이터 변경, 검토 전용 요청에는 사용하지 않는다."
---

# workflow-docs-sync

## Purpose
설계 또는 실행 흐름을 바꾸는 코드 작업을 감지하고, 실제 구현을 기준으로 `docs/workflow` 본문과 Mermaid를 동기화한다.

## Use When
- Workflow 단계, 상태 또는 전이 변경
- Runner, Lease, Session, Human Gate 또는 Recovery 변경
- Agent 역할, Provider, Test Policy 또는 Completion 변경
- API, WebSocket 또는 Component 책임 변경
- 구현과 Workflow 문서의 불일치가 확인된 경우

## Do Not Use When
- 동작을 바꾸지 않는 리팩터링
- 변수명, 포매팅 또는 주석 변경
- 테스트 데이터만 변경
- `docs/workflow`와 무관한 변경
- 검토만 요청하고 문서 수정은 요청하지 않은 경우

## Core Workflow
1. 사용자 요청과 `git diff`에서 실행 의미 변경을 확인한다.
2. 관련 구현과 테스트를 읽는다.
3. 영향받는 Workflow 문서를 선택한다.
4. 구현을 Source of Truth로 문서와 Mermaid를 수정한다.
5. 관련 상태, API, 책임 경계와 예외 흐름을 검증한다.
6. 수정 문서, 반영한 구현 변경, 현재 구현 제한, 검증 결과를 보고한다.

## Reference Routing
필요한 reference만 읽는다. 작업 시작 시 일괄 로드하지 않고 관련 없는 reference는 읽지 않는다.
- [workflow-map.md](references/workflow-map.md): 영향 문서를 선택할 때 읽는다. 영향 문서가 명확하고 단일 문서 안에서 완결되면 생략할 수 있다. 전체 순서, 상태 전이, Component 책임, Human Gate, Recovery, Lease, Session, Completion 변경은 여러 문서에 영향을 주므로 읽는다.
- [implementation-invariants.md](references/implementation-invariants.md): 변경된 흐름이 현재 불변 조건이나 알려진 제한에 닿을 때 읽는다.
- [mermaid-validation.md](references/mermaid-validation.md): Mermaid를 생성, 수정, 분리하거나 구조를 검증할 때만 읽는다. 본문 문구만 수정하고 Mermaid가 바뀌지 않으면 읽지 않는다.

읽기 규칙:
- 현재 turn에서 최신 파일 전체를 이미 읽었고 이후 변경이 없으면 반복해서 읽지 않는다.
- 이전 대화, 요약 또는 압축된 컨텍스트에 내용이 있다는 이유만으로 현재 파일 읽기를 생략하지 않는다.
- 부분 발췌만 읽었다면 사용 전에 전체 파일을 읽는다. 이번 작업 중 수정했다면 최종 내용을 다시 확인한다.
- reference를 읽지 않았다는 이유로 구현 검증을 생략하지 않는다. 실제 코드가 최종 기준이다.

## Core Constraints
- 현재 실행 코드가 문서보다 우선한다. 구현 의도와 충돌하면 차이를 보고하고 코드를 임의로 수정하지 않는다.
- 구현되지 않은 흐름을 완료 기능처럼 기록하지 않는다. 코드에 없는 Service, 상태 또는 책임을 만들지 않는다.
- 구현 결함을 문서로 숨기지 않고 「현재 구현 제한」으로 기록한다.
- 사용자가 수정 범위를 특정 문서로 제한했다면 그 밖은 수정하지 않는다. 범위 밖 영향 문서와 예상 불일치는 보고한다. 문서 이름이 언급됐다는 이유만으로 상위 개요나 연결 문서의 영향을 무시하지 않는다.
- 문서 수정이 필요하지 않으면 파일을 바꾸지 않고 근거를 보고한다.
- 사용자 요청 없이 코드를 수정하거나 stage, commit하지 않는다. 기존 사용자 변경을 되돌리거나 정리하지 않는다.

## 수정 범위와 Git 기준선
작업 트리가 깨끗하다고 가정하지 않는다.
1. 시작 시 `git status --short`와 관련 diff를 기준선으로 기록한다.
2. 기존 코드, 테스트, 문서 변경을 보존한다.
3. 수정은 영향받는 `docs/workflow` 문서로 한정한다. 사실이 틀려진 경우에 한해 관련 reference도 갱신할 수 있다.
4. 종료 시 기준선과 비교해 이번 동기화가 만든 변경만 구분한다.

reference를 갱신했다면 해당 reference를 전체 다시 읽고, 이 파일의 링크와 routing 설명, 다른 reference와의 중복·충돌을 확인한다. 현재 세션이 reference를 자동 재로딩한다고 가정하지 않는다. 이 `SKILL.md`는 사용자가 Skill 수정을 요청하지 않는 한 변경하지 않는다. reference 갱신의 권한 판단과 보류 규칙은 implementation-invariants.md를 따른다.

## Report
- 수정한 Workflow 문서
- 반영한 구현 변경
- 현재 구현 제한
- Mermaid 및 식별자 검증 결과
- 코드·테스트 무변경 확인(기준선 대비)
- 갱신하거나 갱신을 보류한 reference와 근거 (해당 시)
