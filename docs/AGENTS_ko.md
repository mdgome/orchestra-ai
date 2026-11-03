# Multi-Agent Orchestrator — 공통 에이전트 지침

> 영문 정본: [AGENTS.md](../AGENTS.md) — 이 파일은 참고 번역본이며 도구 지침 정본은 루트 영문 파일입니다.

## 목표 및 범위
Claude Code와 Codex CLI를 조율하는 로컬, 단일 러너(single-runner) FastAPI/Python 오케스트레이터(Phase 1+2)를 구축한다. 승인된 v2.3 구현 계획을 따르며, 재설계하는 대신 구현하고 검증한다.

## 워크플로
1. **PLANNER — Claude Code (Plan Mode):** 소스 파일을 변경하지 않고 구조화된 구현 계획을 작성한다.
2. **DESIGN_REVIEWER — Codex CLI (읽기 전용):** 저장된 Plan 아티팩트와 그 검토 기준(review basis)을 검토한다.
3. **Human Gate (필요 시):** 중대한 설계 트레이드오프를 승인한다; 승인이 CLI 권한을 확장하지는 않는다.
4. **DEVELOPER — Claude Code:** Design Review를 통과한 Plan만 구현한다.
5. **TESTER — Claude Code:** 제한된 권한 하에서 테스트를 작성/수정한다.
6. **Orchestrator:** 필요한 테스트를 독립적으로 실행하고 검증한다.
7. **CODE_REVIEWER — Codex CLI (읽기 전용):** 코드와 테스트 결과를 검토한다.
8. 별도의 replan/기술적 재시도(technical-retry) 예산에 따라 최대 **3**회까지 개선을 반복한다.

Plan 내용 또는 그 검토 기준(요구사항, 계획, 승인된 결정사항, 검토 정책)이 변경되면 재검토한다. 검토되지 않은 Plan에 대해서는 절대 구현하지 않는다.

## 아키텍처
- 계층형 아키텍처: **Router → Service → 구체적인 Repository / Provider**. Router는 HTTP와 상태 매핑만 처리한다.
- 명사 기반 REST URI; 중앙화된 검증(validation)과 공통 오류 포맷(error envelope).
- Phase 1+2에서는 DDD, 범용(generic) 리포지토리, 범용 워크플로 엔진, Redis/Celery, Git Worktrees, 분산 러너, OS/컨테이너 격리를 추가하지 **않는다**.
- 추상화는 작고 명시적으로 유지한다; 상당한 설계/유지보수 트레이드오프는 Human Gate가 필요하다.

## 영속성 및 세션
- **PostgreSQL이 진실의 원천(source of truth)**이다 — Conversations, Agent Sessions, Workflow, Checkpoints, Approvals, Execution Requests에 대해.
- `conversations`: 최소한의 대화 메타데이터; `messages`: 저장된 사용자/시스템 메시지로, 세션 메타데이터와는 별도이다.
- `agent_sessions`: **태스크 + 에이전트 역할**당 하나의 현재 네이티브 resume ID를 가진다; 다섯 가지 역할은 PLANNER, DESIGN_REVIEWER, DEVELOPER, TESTER, CODE_REVIEWER이다.
- 네이티브 CLI 세션은 보조적인 컨텍스트일 뿐, **권위 있는(authoritative)** 상태나 권한이 **아니다**. Task/Conversation, Lease, Approval, Checkpoint, 역할/프로바이더 검증 이후에만 재개(resume)한다. 네이티브 ID는 API 응답이나 로그에 절대 노출하지 않는다.
- `SessionStore`는 좁은 범위의 프로토콜이며, Phase 1+2에서는 PostgreSQL 구현만 존재한다. Redis는 추후 **읽기 캐시**로 추가될 수 있으나, 진실의 원천으로는 사용되지 않는다.
- 서비스가 데이터베이스 트랜잭션을 소유하며, 리포지토리는 독립적으로 커밋하지 않는다. CLI 명령을 실행하는 동안 DB 트랜잭션을 유지하지 않는다.

## 실행, 안전성 및 복구
- 실행 요청(execution requests)을 PostgreSQL에 저장하고, 하나의 로컬 Runner가 이를 소비하며 Task Lease를 획득한다.
- 세션 수준의 PostgreSQL 어드바이저리 락(advisory lock)을 Runner 수명 동안 전용 커넥션에 유지한다. 소유권을 상실하면 CLI 작업 개시를 중단하고 복구 규칙을 적용한다.
- Checkpoint는 입력/출력 해시를 사용한다; WorkspaceSnapshot은 변경 사항을 감지하고 중단된 단계의 재실행을 방지한다. 스냅샷 해시는 백업이 **아니다**.
- 프로세스 종료, 외부 부작용(side effects), 또는 워크스페이스 안전성이 불확실한 경우 **자동 재실행하지 않는다**: RECOVERY Human Gate로 진입한다.
- CLI 권한은 역할별로 지정되며 실제 프리플라이트(preflight) 프로브로 검증된다. 타임아웃이나 유휴 출력으로부터 권한 요청을 추론하지 않는다.
- 완료되려면 독립적으로 실행된 테스트가 필요하다; 실패, 수집된 테스트가 0건이거나 결과가 유효하지 않으면 통과할 수 없다.

## 전달(Delivery)
- 오래된 계획 텍스트와 상충할 경우 가장 최신의 *Final Implementation* 규칙을 따른다.
- 실제로 실행된 테스트와 남아 있는 미검증 가정을 보고한다. 실제로 테스트하지 않은 Windows/CLI 동작을 테스트했다고 주장하지 않는다.
