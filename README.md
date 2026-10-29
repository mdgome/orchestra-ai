# 멀티 에이전트 오케스트레이터

하나의 결정론적 워크플로를 통해 **Codex CLI**와 **Claude Code CLI**를 구동하는 FastAPI 서비스입니다.

```
PLAN (Claude, Plan Mode) -> DESIGN_REVIEW (Codex, read-only) -> [Human Gate] -> DEVELOP (Claude)
      -> TEST (Claude writes tests, the Orchestrator runs them) -> CODE_REVIEW (Codex, read-only) -> COMPLETED
                         ^---------------------- up to 3 improvement iterations ----------------------'
```

오케스트레이터 자체는 순수 Python으로 구성되며 LLM을 사용하지 않습니다. 성공 여부는 에이전트가 작성한 설명이 아니라
종료 코드, JUnit 결과 및 구조화된 리뷰를 기준으로 판단합니다.

| 역할 | 제공자 | 권한 |
|---|---|---|
| PLANNER | Claude `--permission-mode plan` | 읽기/탐색만 가능 (`Read,Glob,Grep`) |
| DESIGN_REVIEWER | Codex `-s read-only` | 읽기 전용 |
| DEVELOPER | Claude | 편집 + 제한된 Bash |
| TESTER | Claude | `tests/` 아래 편집 + `pytest`만 가능(오케스트레이터가 스냅샷 차이로 검증) |
| CODE_REVIEWER | Codex `-s read-only` | 읽기 전용 |

## 지원 범위(먼저 읽어주세요)

* **단일 Runner, 신뢰할 수 있는 로컬 환경.** 데이터베이스당 하나의 Runner 프로세스만 사용할 수 있습니다(PostgreSQL advisory
  lock으로 강제되며, 두 번째 프로세스는 Runner 시작을 거부합니다). `uvicorn app.main:app --workers 1`로 실행하세요.
* CLI 권한 플래그는 **도구 정책이지 OS 샌드박스가 아닙니다.** 악의적이거나 손상된 모델 또는 CLI 버그를 격리하지
  못합니다. OS/컨테이너 격리는 3단계 항목입니다. 잃어서는 안 되는 비밀 정보가 저장된 시스템에서 신뢰할 수 없는
  요구사항을 대상으로 실행하지 마세요.
* **정확히 한 번 실행을 보장하지 않습니다.** 외부 부수 효과는 최소 한 번 발생할 수 있습니다. 오케스트레이터는 작업공간
  변경을 감지하며, 아무 작업도 발생하지 않았음을 입증할 수 없는 경우 파일을 변경하는 단계를 자동으로 다시 실행하지
  않고 대신 `RECOVERY` Human Gate를 엽니다.
* WebSocket 이벤트는 최선형으로 제공되며 PostgreSQL이 최종 데이터 원본입니다. 연결이 끊어진 후에는
  `GET /tasks/{id}`, `/executions`, `/state-transitions`를 다시 조회하세요.
* 포함되지 않은 항목(3단계): Redis, Celery, Git worktree, 컨테이너 격리, 분산 Runner, 사용량 추적.

## 실행

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"      # Windows; POSIX에서는 .venv/bin/pip 사용
docker compose up -d postgres                                       # 또는 PostgreSQL 15 이상
cp .env.example .env
.venv/Scripts/alembic upgrade head
.venv/Scripts/uvicorn app.main:app --workers 1
```

`codex`와 `claude`가 설치되어 있고 로그인된 상태여야 합니다. 시작 시 사전 점검을 통해 설치된 버전이 사용하는 모든
플래그를 지원하는지 확인합니다. 또한 `~/.claude/settings*.json`, 프로젝트의 `.claude/`, `~/.codex/config.toml`에서
권한을 확대하는 설정(예: `bypassPermissions`)을 검사하며, 차단 대상이 발견되면 Runner를 중지합니다.

## API

URI에는 명사를 사용하고 동작은 HTTP 메서드로 표현합니다. 오류는 항상 `{"error": {"code", "message", "details"}}` 형식을 사용합니다.

| 메서드 | URI | 설명 |
|---|---|---|
| POST | `/conversations` | 201 |
| GET | `/conversations/{id}` | |
| POST | `/conversations/{id}/messages` | 201, 대화가 CLOSED/EXPIRED 상태이면 409 |
| GET/POST | `/tasks` | POST는 PENDING 작업을 **생성만** 하며(201), 아무 작업도 실행하지 않음 |
| GET/PATCH | `/tasks/{id}` | GET 응답에 현재 lease, 최신 checkpoint 및 미결 승인 포함 |
| POST | `/tasks/{id}/execution-requests` | 202. 요청을 저장하고 Runner가 실행함. 미결 요청이 있는 동안 멱등성 보장 |
| POST | `/tasks/{id}/cancellation-requests` | 202. 요청을 기록하며 CLI 프로세스 종료가 확인된 후에만 CANCELLED 처리 |
| GET | `/tasks/{id}/executions` · `/state-transitions` · `/approvals` | 이력 |
| GET | `/approvals/{id}` | |
| POST | `/approvals/{id}/decisions` | `Idempotency-Key` 헤더 필수. 신규 201, 동일 요청 재실행 200, 충돌/만료 데이터 409, 기한 만료 410 |
| WS | `/tasks/{id}/event-stream` | 실시간 이벤트 |

결정을 승인하면 결정 내용과 `RESUME` 실행 요청을 하나의 트랜잭션에 저장합니다. 따라서 승인 직후 장애가 발생해도 다음
Runner 시작 시 작업이 재개됩니다.

## 테스트

```bash
.venv/Scripts/python -m pytest                    # 실제 CLI를 제외한 전체 테스트(Docker 및 설치된 PostgreSQL 불필요)
.venv/Scripts/python -m pytest -m e2e_cli         # 실제 Codex + Claude CLI 테스트(느리며 토큰 사용)
```

PostgreSQL 통합 테스트는 **내장 PostgreSQL**(`pixeltable-pgserver`, 테스트 전용 개발 의존성)을 시작하므로 Docker가
필요하지 않습니다. 시작할 수 없는 경우 테스트는 명확한 오류를 발생시키며 어떤 항목도 조용히 건너뛰지 않습니다.
테스트 스키마는 실제 Alembic 마이그레이션으로 생성되고, `alembic check`로 모델과 스키마가 계속 일치하는지 검증합니다.

설계 결정과 **검증되지 않는** 항목 목록은 `docs/ARCHITECTURE.md`를 참고하세요.
