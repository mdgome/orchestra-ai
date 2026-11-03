# 멀티 에이전트 오케스트레이터

> English: [README.md](README.md)

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

* **하나의 Runner(Coordinator) 프로세스, 신뢰할 수 있는 로컬 환경.** 데이터베이스당 하나의 Runner 프로세스만 사용할 수
  있습니다(PostgreSQL advisory lock으로 강제되며, 두 번째 프로세스는 Runner 시작을 거부합니다).
  `uvicorn app.main:app --workers 1`로 실행하세요. 병렬성은 이 프로세스 **안의** Task 단위입니다(서로 다른 Project의
  Task는 병렬, 같은 Project의 변경 Step은 Project write lock으로 직렬).
* **Project 사용 규칙.** 활성 Project Task가 있는 동안의 외부 프로세스(에디터, 빌드 도구 등)의 직접 수정은 **지원하지
  않습니다.** 파일시스템과 DB는 하나의 원자 트랜잭션이 될 수 없고 측정과 커밋 사이의 변경은 완전히 탐지되지 않을 수
  있습니다(감지되면 읽기 단계는 재시도, 변경 단계는 Recovery 대상). Task 사이(Project가 유휴)의 사용자 수정은
  허용되며 다음 Task가 처음 write lock을 얻을 때 baseline으로 수용됩니다. 오케스트레이터는 Project 파일을 `reset`,
  `clean`, `checkout`하거나 삭제하지 않습니다.
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
| POST | `/projects` | 201. `workspace_path`(기존 로컬 Workspace) **또는** `repository_url`(`PROJECT_CLONE_ROOT` 아래로 clone)을 정확히 하나 지정. 선택 `test_policy`(예: `{"preset": "maven"}`) |
| GET | `/projects` · `/projects/{id}` | |
| PATCH | `/projects/{id}` | `name`, `status`(`ARCHIVED`는 미종료 Task가 있으면 409), `test_policy`(생략=유지, 객체=검증 후 교체, `null`=제거, `name`/`status`의 `null`은 422. 이미 만든 Task의 정책 복사본은 바뀌지 않음) |
| GET/POST | `/projects/{id}/tasks` | Project Workspace를 공유하는 Task 목록/생성(201, 실행은 하지 않음) |
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

## Project 사용

```bash
# 1) 기존 로컬 프로젝트를 등록(절대 경로, 링크/junction 불가, 시스템·오케스트레이터 경로·다른 Project와 겹치는 경로 불가)
curl -X POST localhost:8000/projects -H 'content-type: application/json' \
     -d '{"name": "Service A", "workspace_path": "D:\\Projects\\Service-A", "test_policy": {"preset": "pytest"}}'
# 2) 자연어 Task 생성 -> 실행 요청
curl -X POST localhost:8000/projects/{id}/tasks -d '{"requirements": "..."}' -H 'content-type: application/json'
curl -X POST localhost:8000/tasks/{task_id}/execution-requests
```

* `repository_url`은 `https://`, `ssh://`, `git@host:path` 형식만 허용하며 자격 증명이 포함된 URL(`user:token@host`)은
  거부합니다. 인증은 시스템 credential helper 또는 ssh-agent에 맡기며, URL·오류 메시지의 자격 증명은 로그, DB,
  API 응답에서 마스킹합니다.
* Test Policy preset: `pytest`(기본), `maven`, `gradle`. Task 생성 시 프로젝트의 `mvnw`/`gradlew`(없으면 시스템 도구)
  하나로 해석되며, 도구가 없으면 Task 생성이 422로 거부됩니다. 테스트 결과(JUnit XML)는 삭제하지 않고 **이번 실행이
  새로 만들었거나 바꾼 파일만** 판정에 사용합니다.
* 이전 Task가 미해결 변경을 남긴 Project(`workspace_state: DIRTY_UNRESOLVED`)에서 다음 Task가 변경 Step을 시작하면
  `PROJECT_DIRTY` RECOVERY Gate가 열립니다. `continue`는 현재 상태를 새 baseline으로 수용하고, `abort`는 Task를
  실패시킵니다(파일은 건드리지 않음).

설정(`.env`): `MAX_CONCURRENT_TASKS`(기본 2), `MAX_TASKS_PER_PROJECT`(기본 1), `MAX_CONCURRENT_CLI_PROCESSES`(기본 4),
`RUNNER_SHUTDOWN_GRACE_SECONDS`(기본 30), `PROJECT_CLONE_ROOT`(기본 `./project-clones`).

Request recovery timeout(`.env`): `RECOVERY_STATEMENT_TIMEOUT_SECONDS`(기본 15, PostgreSQL statement 제한),
`RUNNER_RECOVERY_TIMEOUT_SECONDS`(기본 20, recovery 작업을 기다리는 시간), `RUNNER_RECOVERY_SHUTDOWN_WAIT_SECONDS`(기본 5,
취소 요청 뒤 정산을 기다리는 시간), `DB_POOL_TIMEOUT_SECONDS`(기본 30, connection checkout). 모두 0보다 커야 하고
statement는 recovery timeout보다 작아야 합니다. recovery 대기와 취소 후 정산은 bounded이지만 Runner.stop 전체가 bounded한 것은 아니며, PostgreSQL advisory lock connection의 close에는 현재 별도 상한이 없습니다.

**DB pool** (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`): 이 절이 정본입니다(`.env.example`은 키와 기본값만 적습니다).

* 필수 조건은 하나뿐입니다. `DB_POOL_SIZE + DB_MAX_OVERFLOW`(총 capacity)가 **2 이상**이어야 합니다. Runner의
  advisory lock이 연결 하나를 Runner 수명 동안 점유하므로, 업무 트랜잭션용 연결이 하나 더 있어야 하기 때문입니다.
  `DB_POOL_SIZE`는 1 이상, `DB_MAX_OVERFLOW`는 0 이상이며 `0`(고정 pool)도 유효합니다.
* `DB_POOL_SIZE`를 지정하지 않으면 **권장값** `4 × MAX_CONCURRENT_TASKS + 6`을 씁니다(기본 14). 권장값은 처리량과
  Lease heartbeat 지연 위험을 낮추기 위한 **추정치**이며 강제되지 않습니다: 실행 중인 Task마다 짧은 트랜잭션
  (Step 기록·Lease heartbeat·취소 확인)이 겹칠 수 있고, Runner loop·API 요청이 추가로 연결을 씁니다. 권장값보다 작은
  유효한 pool도 거부하지 않습니다. 작은 pool은 오류가 아니라 **대기**로 나타납니다(연결을 얻을 때까지 기다림).
* `MAX_CONCURRENT_TASKS`를 올리면 명시하지 않은 pool 크기도 함께 늘어납니다. 명시한 `DB_POOL_SIZE`는 바뀌지 않습니다.

## 테스트

```bash
.venv/Scripts/python -m pytest                    # 실제 CLI를 제외한 전체 테스트(Docker 및 설치된 PostgreSQL 불필요)
.venv/Scripts/python -m pytest -m e2e_cli         # 실제 Codex + Claude CLI 테스트(느리며 토큰 사용)
```

PostgreSQL 통합 테스트는 **내장 PostgreSQL**(`pixeltable-pgserver`, 테스트 전용 개발 의존성)을 시작하므로 Docker가
필요하지 않습니다. 시작할 수 없는 경우 테스트는 명확한 오류를 발생시키며 어떤 항목도 조용히 건너뛰지 않습니다.
테스트 스키마는 실제 Alembic 마이그레이션으로 생성되고, `alembic check`로 모델과 스키마가 계속 일치하는지 검증합니다.

설계 결정과 **검증되지 않는** 항목 목록은 `docs/ARCHITECTURE.md`를 참고하세요.
