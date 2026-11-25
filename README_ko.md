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
권한을 확대하는 설정(예: `bypassPermissions`)을 검사하며, 차단 대상이 발견되면 Runner를 중지합니다. 로그인 상태 자체는 검증하지 **않으며**(프로브는 CLI를 실행해 토큰을 사용함),
아래 진단에서 `UNKNOWN`으로 보고합니다.

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
| GET | `/projects/{id}/profile` | 활성 Project Profile 버전(*Project Profile* 참고) |
| GET/POST | `/projects/{id}/profile-versions` | 버전 목록(최신순, 페이지네이션) / 새 버전 생성 **및 활성화**: 201, 활성 버전과 같으면 200 `created: false` |
| GET | `/projects/{id}/diagnostics` | 읽기 전용 실행 준비 점검(*Health와 진단* 참고). `?refresh=true`는 30초 캐시를 우회(완전한 결과만 캐시) |
| GET | `/health` | API 생존 여부와 Runner 준비 상태(*Health와 진단* 참고) |
| GET/POST | `/tasks` | POST는 PENDING 작업을 **생성만** 하며(201), 아무 작업도 실행하지 않음 |
| GET/PATCH | `/tasks/{id}` | GET 응답에 현재 lease, 최신 checkpoint 및 미결 승인 포함 |
| POST | `/tasks/{id}/execution-requests` | 202. 요청을 저장하고 Runner가 실행함. 미결 요청이 있는 동안 멱등성 보장 |
| POST | `/tasks/{id}/cancellation-requests` | 202. 요청을 기록하며 CLI 프로세스 종료가 확인된 후에만 CANCELLED 처리 |
| GET | `/tasks/{id}/executions` · `/state-transitions` · `/approvals` | 이력 |
| GET | `/tasks/{id}/artifacts` · `/tasks/{id}/artifacts/{artifact_id}` | 저장된 단계 결과(페이지네이션: `kind`, `limit` ≤ 100, `offset`). *산출물과 완료 보고서* 참고 |
| GET | `/tasks/{id}/report` | 저장된 사실로 구성하는 결정적 완료 보고서(LLM 호출 없음) |
| GET | `/tasks/{id}/changes` | Task가 Project write lock을 잡은 시점부터의 누적 변경(`limit` ≤ 100, `offset`, `path`, `verify_workspace`). *Task 변경 내역* 참고 |
| GET | `/approvals/{id}` | |
| POST | `/approvals/{id}/decisions` | `Idempotency-Key` 헤더 필수. 신규 201, 동일 요청 재실행 200, 충돌/만료 데이터 409, 기한 만료 410 |
| WS | `/tasks/{id}/event-stream` | 실시간 이벤트 |

결정을 승인하면 결정 내용과 `RESUME` 실행 요청을 하나의 트랜잭션에 저장합니다. 따라서 승인 직후 장애가 발생해도 다음
Runner 시작 시 작업이 재개됩니다.

## 산출물과 완료 보고서

여기의 모든 내용은 이미 저장된 데이터(task step, execution, approval, state transition)에서 계산합니다. 만들기 위해
에이전트, 명령, LLM을 호출하지 않으며, 에이전트가 만들지 않은 것을 지어내지 않습니다.

* `GET /tasks/{id}/artifacts?kind=&limit=&offset=`은 저장된 단계 결과(`PLAN`, `DESIGN_REVIEW`, `DEVELOP`, `TEST`,
  `CODE_REVIEW`)를 오래된 순(`created_at`, 다음 `id`)으로 나열합니다. 각 항목은 `plan_revision`, `workflow_iteration`,
  `step_attempt`를 따로 가지며(재계획, 개선 회차, 기술적 재시도는 서로 다른 것), `is_latest_succeeded`(해당 종류에서
  가장 최근에 만들어진 SUCCEEDED 산출물이며 "현재 유효"를 뜻하지 **않음**)와 `validity`를 포함합니다.
* `validity.state`: `CURRENT`(저장된 기준이 아직 일치하는 계획/설계 검토), `STALE`(저장된 기준이 더 이상 일치하지 않거나
  이후 revision/회차/시도가 대체함. `reasons`가 이유를 설명), `UNVERIFIED`(workspace 내용을 설명하는 산출물: 저장된 기준은
  모두 일치하지만 live workspace와 비교하지 않았으므로 CURRENT라고 부르지 않음), `NOT_APPLICABLE`(단계가 성공하지 않음).
* `GET /tasks/{id}/artifacts/{artifact_id}`는 정화된 저장 출력과 함께 산출물 하나를 반환합니다. 다른 Task의 id는 `404`입니다.
* `GET /tasks/{id}/report`는 결과와 `outcome_note`, 계획, 설계 검토, 구현, 테스트(오케스트레이터가 실행한 명령과 결과,
  `run_by: ORCHESTRATOR`), 코드 검토, 회차별 지적 사항, Human Gate 이력, 마지막 전이, `next_actions`를 반환합니다.
* `COMPLETED`는 오케스트레이터의 완료 조건을 충족했다는 뜻입니다. 사용자가 결과를 인수하거나 merge·배포했다는 뜻이
  **아닙니다**.
* `historical_completion`은 저장된 COMPLETED 전이만 보여 줍니다. 그 시점의 Definition-of-Done 상세는 저장된 적이 없으므로
  `conditions`는 `UNKNOWN`이며 재구성하지 않습니다. `current_re_evaluation`은 같은 조건을 지금 저장된 산출물에 대해
  평가하며(`evaluated_at`, `evaluation_basis`), 과거 완료를 확인하거나 취소하지 않고 live workspace와도 비교하지 않습니다.
* 응답에는 native session id, 자격 증명, step manifest가 포함되지 않습니다. 하나의 재귀 sanitizer가 해당 Task의 알려진
  session id(일반 UUID는 유지), URL 자격 증명과 토큰을 마스킹하고, 금지된 key를 제거하며, 깊이·key 수·배열 항목·문자열
  길이·크기를 제한합니다. 잘린 내용은 데이터 안이 아니라 `sanitization` / `SanitizedJson.reasons`에 명시됩니다. 크기 제한을
  넘는 페이지는 뒤쪽 항목을 생략하며 `total`은 DB 건수 그대로이고 `returned`, `omitted_items`, `size_limited`, `has_more`,
  `next_offset`이 상황을 알려 줍니다. 생략된 항목은 `next_offset`부터 다시 조회할 수 있습니다. 한도가 답 하나도 담지 못하면
  애플리케이션이 시작되지 않습니다.

## Task 변경 내역

Task가 Project write lock을 잡을 때 오케스트레이터는 같은 트랜잭션에서 workspace의 **baseline**을 기록합니다. Changes
manifest(`CHANGES_MANIFEST_V1`: 경로마다 `FILE` {sha256, executable}, `SYMLINK` {target}, `GITLINK` {oid}), HEAD(`COMMIT` 또는
`UNBORN`), 그리고 이미 dirty였던 경로입니다. **hash만 저장하고 파일 내용은 저장하지 않습니다.** `GET /tasks/{id}/changes`는
live workspace를 이 baseline과 비교합니다.

* **측정 둘, 실패 정책 둘.** Project fingerprint(`WORKSPACE_FINGERPRINT_V1`, 변경 없음)는 Task가 Project를 소유해도 되는지를
  결정합니다. 측정할 수 없으면 기존 fail-closed 경로(owner 없음, baseline 없음, `DIRTY_UNRESOLVED`, `PROJECT_DIRTY` gate)를
  따릅니다. manifest, HEAD, dirty 경로는 보조 자료이며, 이것만 실패하면 Task는 **계속 실행**되고 이유 코드(`TOO_MANY_FILES`,
  `DIRTY_PATHS_LIMIT`, `DEADLINE`, `GIT_ENUMERATION_FAILED` 등)가 있는 `UNAVAILABLE` baseline 행이 owner 설정과 같은
  트랜잭션에 기록됩니다. 순서는 fingerprint, manifest, fingerprint 재측정이며 두 fingerprint가 다르면 측정이 stale이므로 기존
  bounded retry 안에서 다시 측정합니다.
* **Epoch.** `proof_epoch` = fingerprint를 측정한 시점(acquisition 전)의 project epoch, `acquired_epoch` = acquisition이 만든
  epoch(`proof_epoch + 1`), `recorded_epoch` = 행을 기록한 시점의 epoch입니다. `set_owner`가 자신이 만든 epoch를 명시하며 +1을
  추측하지 않습니다. baseline 행 없이 이미 lock을 소유한 Task(crash gap, 또는 이 기능 이전의 owner)는 행 없이 READY가 되지
  않습니다. 아무것도 바꾸지 않았고 fingerprint가 write baseline과 같으면 manifest를 복구하고, 아니면 proof/acquired epoch가
  **null**인 명시적 `UNAVAILABLE` 행(`BASELINE_MISSING_AT_REENTRY`)을 기록합니다. 원래 epoch를 지어내지 않습니다.
* **범위.** GIT mode는 tracked 파일과 untracked non-ignored 파일을 다루고(ignored build output 제외), CONTENT mode(사용 가능한
  git 없음)는 workspace policy walk를 다룹니다. `executable`은 working-tree의 mode bit이며 Windows에서는 항상 false입니다. 다루지
  않는 것: ignored 파일, submodule 내부(commit id만), staged만 된 mode 변경. 응답에 `limitations`가 나열됩니다.
* **본문.** 텍스트 diff는 GIT mode에서 head가 `COMMIT`이고 시작 시 clean이었던 파일에만 있습니다(옛 내용은 `git cat-file`에서 옴).
  그 외에는 `diff_status`가 이유와 함께 `UNAVAILABLE`(`BASELINE_CONTENT_NOT_STORED`, `UNBORN_HEAD`,
  `NO_BASELINE_CONTENT_WITHOUT_GIT`, `BINARY`, `LFS_POINTER`, `SYMLINK_TARGET_ONLY`, `SUBMODULE_CONTENT_NOT_STORED`), `WITHHELD`
  (`SENSITIVE_PATH`: `.env*`, 키, 인증서, credentials 등은 경로만 나열하고 내용은 절대 반환하지 않음,
  `PROJECT_OWNED_BY_OTHER_TASK`), `TRUNCATED`(`FILE_TOO_LARGE`, `BODY_BUDGET`, `DEADLINE`, `SIZE_LIMIT`)입니다. 이미 dirty였던
  파일은 `pre_existing_dirty`로 표시하며 Task의 변경으로 단정하지 않습니다. 본문은 credential이 마스킹됩니다. `rev-parse`,
  `ls-files`, `status`, `cat-file`만 실행하며 reset, clean, checkout, commit, push는 하지 않습니다.
* **snapshot이 아니라 live 비교.** `lock_consistency`(조회 전후 owner·write epoch·workspace state 비교, 변하면 1회 재시도)와
  `workspace_consistency`는 별개입니다. 외부 프로세스는 DB를 건드리지 않고 파일을 바꿀 수 있기 때문입니다.
  `workspace_consistency`는 `verify_workspace=true`에서 실제 측정 두 번이 일치할 때만 `STABLE`이고, 그 외에는 이유와 함께
  `UNVERIFIED`입니다. 종료된 Task의 답도 다른 Task가 workspace를 바꾼 뒤에는 확정된 것이 아닙니다(`workspace_relation`).
* **제한.** deadline 하나가 측정 코드 자체까지 전달됩니다. git은 `min(자체 timeout, 남은 시간)`으로 종료되고 walk와 hashing은
  entry·chunk 사이에서 확인합니다. 이 filesystem deadline은 **cooperative**입니다. OS 안에서 멈춘 read는 중단할 수 없어 worker가
  deadline 뒤에도 남을 수 있습니다. Project당 측정 1개, 전체 `CHANGES_MAX_CONCURRENT_GLOBAL`개까지만 실행하고, 슬롯은 요청이
  포기한 시점이 아니라 worker thread가 실제로 끝났을 때 반납합니다. busy 응답은 `503 CHANGES_MEASUREMENT_BUSY`이며 timeout 뒤에는
  cooldown 동안 `verify_workspace`를 거부합니다(`UNVERIFIED`, 이유 `COOLDOWN`). Project가 없는 Task는 `UNAVAILABLE`(`NO_PROJECT`),
  lock을 잡은 적 없는 Task는 `NO_BASELINE`입니다.

## Health와 진단

`GET /health`는 API 프로세스가 살아 있으면 200으로 응답합니다. 기존 키의 의미는 그대로이고 `ready`, `warnings`, `runner`가 추가되었습니다.

```json
{"status": "ok", "runner_active": false, "ready": false, "warnings": [],
 "runner": {"lifecycle": "STOPPED", "startup_outcome": "PREFLIGHT_BLOCKED", "blockers": ["PREFLIGHT_FAILED"],
            "background_degraded": false,
            "sweeper": {"stalled": false, "consecutive_failures": 0, "last_failure_code": null, "last_failure_at": null,
                        "next_retry_at": null, "config_invalid_active": false, "clearance_pending": false}}}
```

* `status`는 API 생존 여부만 뜻합니다. `runner_active`와 `ready`는 **`Runner.lifecycle`에서 계산**하며(직접 대입하지 않음),
  `runner_active`는 Runner 루프가 도는 동안만 true이고 `ready`는 추가로 막는 요인이 없어야 합니다(예: 미확정 복구).
* Runner가 준비되지 않았어도 `POST /tasks/{id}/execution-requests`는 202로 응답하고, 요청은 Runner가 준비될 때까지
  `PENDING`으로 남습니다. `runner.blockers`가 이유를 설명합니다: `PREFLIGHT_FAILED`, `ADVISORY_LOCK_HELD`, `STOPPED`,
  `STARTING`, `SHUTTING_DOWN`, `STOP_INCOMPLETE`, `RECOVERY_UNSETTLED`, `SUPERVISOR_DEGRADED`(백그라운드 supervisor가
  종료됨, Runner는 새 작업을 claim하지 않음). `START_FAILED`는 **예약 상태**입니다. `Runner.start`의 일반 예외는 지금도 API
  기동을 중단시키므로(결정 D6), 실행 중인 API가 이를 보고하는 일은 없습니다.
* `warnings`는 `SWEEPER_STALLED`, `SWEEPER_CONFIG_INVALID`, `BACKGROUND_COMPONENT_DEGRADED`를 정렬한 목록입니다.
  `runner.background_degraded`는 supervisor 또는 필수 백그라운드 unit(orphan sweeper, risk worker, capability 갱신)이 실제로
  degraded인 동안에만 true이며, supervisor가 아닌 unit의 degraded는 dispatch를 막지 않습니다. `runner.sweeper`는 orphan
  sweeper의 stall과 설정 상태를 보고합니다.

`GET /projects/{id}/diagnostics`는 **읽기 전용 빠른 점검**입니다. 에이전트, 로그인 프로브, 설치 프로그램, 프로젝트 테스트를
실행하지 않으며, 도구 버전 확인(`git --version`, `java -version`)과 파일 읽기만 수행합니다. 각 항목은 `code`,
`status`(`PASS`/`WARN`/`FAIL`/`UNKNOWN`), `checked_at`, `cause`, `recommendation`, `blocks_execution`을 가지며,
`can_start`는 실행을 막는 항목이 FAIL일 때만 false입니다. Task 실행 가능이나 성공을 **보장하지 않습니다**. 참고:

* `CLI_AUTH`는 항상 `UNKNOWN`입니다. `PYTHON_INTERPRETER`는 발견된 프로젝트 `.venv`를 보고하지만 테스트는 오케스트레이터
  자신의 인터프리터로 실행됩니다. `JUNIT_COLLECTION`은 Gradle이 UP-TO-DATE 테스트 태스크를 건너뛸 수 있음을 경고합니다.
* Project 경로는 등록된 root부터 다시 검증하며, 그 안의 `.claude/settings.json`과 `.claude/settings.local.json`만 읽습니다.
  거기에 링크나 junction이 있으면 차단 대상 발견입니다. 같은 canonical path의 실제 디렉터리가 다른 디렉터리로 교체된
  경우는 탐지하지 **않습니다**.
* Project당 진단은 한 번에 하나만 실행되고 동시 요청은 그 결과를 공유합니다. 파일 점검은 worker thread에서 best-effort
  timeout으로 실행되며, 멈춘 파일시스템은 중단시킬 수 없으므로 전체 deadline은 hard deadline이 **아닙니다**. 그런
  worker는 실제로 끝날 때까지 슬롯을 유지하고, 살아 있는 동안 파일 기반 항목은 `UNKNOWN`입니다.
* `PROCESS_TRACKING`은 Runner의 프로세스 추적 capability를 보고합니다(`PASS` 지원됨, 지원하지 않는 플랫폼은 `WARN`이며
  tracked 명령이 untracked로 degrade, self-test 실패는 `blocks_execution`과 함께 `FAIL`, 확인할 수 없으면 `UNKNOWN`).
  지원되지 않으면 항목은 `blocks_features: ["TEST_RUN"]`을 가집니다. `TRACKED_EXECUTIONS`는 Project의 미해결 또는 anomaly
  tracked execution을 `causes`로 나열합니다(각각 `reason_code`, severity, `blocks_execution`, `recommendation`, 관련
  execution/request id, anomaly는 제한된 목록). 항목 status는 cause 중 가장 높은 severity입니다. 점검 항목이 바뀌었으므로
  `DIAGNOSTICS_POLICY_VERSION`은 2입니다(cache key의 일부).

## Project Profile

예전에는 내장 개발 규칙이 모든 프로젝트의 모든 프롬프트에 주입되었습니다. Project Profile은 프로젝트별로 이를 조정합니다.
Profile은 **데이터**이며 버전마다 불변입니다.

```json
{"description": "Billing service", "languages": ["Java"], "frameworks": ["Spring"],
 "architecture_rules": ["Hexagonal modules"], "coding_rules": ["No static state"],
 "completion_criteria": [{"id": "DOC", "text": "the public API is documented"}], "test_policy_ref": "project"}
```

* `profile` 없이 만든 **새** Project는 **neutral** v1(아키텍처를 강제하지 않음)을 받습니다. Profile 도입 **이전에 있던**
  Project는 `legacy` v1(migration `0003`)을 받으며 그 규칙은 기존 내장 문구와 똑같이 렌더됩니다. Project가 없거나 Profile
  도입 이전에 만든 Task는 내장 규칙을 그대로 유지합니다(`profile_*` 필드가 null).
* Project 행, v1, 활성 포인터는 한 트랜잭션에서 만들어집니다. `POST /projects/{id}/profile-versions`는 project 행 lock 아래에서
  버전을 **만들고 활성화**하며, 활성 버전과 내용이 같으면 아무것도 쓰지 않습니다(200, `created: false`). ARCHIVED Project는
  새 버전을 받지 않지만 조회는 가능하고, profile 행은 삭제되지 않습니다.
* Task는 생성 시 같은 lock 아래에서 활성 버전(번호, hash, 내용)을 **복사**합니다. 이후 변경은 이미 있는 Task를 바꾸지
  않습니다(Task에 `profile_version`, `profile_hash`가 있음).
* Profile hash는 **Profile이 있는 Task에 한해** design-review basis와 PLAN·DEVELOP·CODE_REVIEW input hash에 포함됩니다.
  따라서 한 Profile에서 만든 결과는 다른 Profile에서 재사용되지 않고, Profile이 없는 Task의 hash는 이전과 완전히 같습니다.
* 오케스트레이터 자체의 메커닉(Human Gate 규칙, "받은 것보다 많은 도구를 쓰지 않는다")은 Profile의 일부가 아니며 Profile로
  제거할 수 없습니다. 알 수 없는 key는 **거부**됩니다. Profile은 권한, 도구, 모델, 경로, interpreter를 담을 수 없고(신뢰된
  정책인 `providers/permissions.py`에 남음), `test_policy_ref`는 `"project"`만 가능합니다. 프로젝트의 `test_policy`를
  참조할 뿐 재정의하지 않습니다.
* `completion_criteria`는 Definition-of-Done의 **추가** 조건입니다. 코드 리뷰어는 criterion마다 정확히 하나의
  `{id, met, evidence}`를 담은 `profile_criteria`를 반환해야 하며 결과는 Profile hash와 함께 저장됩니다. 오케스트레이터는
  저장된 평가가 현재 Profile의 것이고 정확히 그 id들만 담았으며(누락·중복·알 수 없는 id 없음) 각각 `met`이고 `evidence`가
  비어 있지 않은지를 **결정적으로** 검사합니다. criterion이 실제로 충족되었는지는 **리뷰어의 판단**이며 오케스트레이터가
  독립적으로 검증하지 않습니다. 다른 모든 조건(테스트, 리뷰 verdict, fingerprint, 설계 검토, Human Gate)은 그대로 적용됩니다.

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
  `PROJECT_DIRTY` RECOVERY Gate가 열립니다. `abort`는 Task를 실패시킵니다(파일은 건드리지 않음). `continue`는 현재
  상태를 새 baseline으로 수용하지만, **Project에 미해결 tracked 테스트 execution도 anomaly tracked 테스트 execution도
  없을 때에만** 가능하며, 그렇지 않으면 숨겨집니다(`options_hidden`, 이유 `UNRESOLVED_ROWS_PRESENT` 또는
  `TRACKED_ANOMALY_PRESENT`). 미해결 tracked execution이 있는 Project에서는 대신 `accept_unproven_risk`(위험 HIGH, 사라졌음이
  입증되지 않은 프로세스의 위험을 운영자가 수용, `rationale` 필요), `recheck_processes`(살아 있는 프로세스가 관찰된 뒤에만),
  오래된 gate에는 `refresh_gate`를 제공합니다. 두 위험 옵션 모두 Project를 `CLEAN`으로 만들지 않으며 `continue`만 만듭니다.
* **Tracked 테스트 명령.** Windows에서 오케스트레이터 자신의 테스트 명령은 tracked execution으로 실행됩니다. 프로세스는
  suspended로 spawn되어 Windows Job Object(`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`)에 할당되고, 식별이 `executions` 행에
  fence와 함께 저장(`detail.tracked`)된 뒤에야 resume되므로, `pid`가 없는 행은 사용자 코드를 실행한 적이 없습니다. 트리는
  Job으로 종료합니다. 트리가 비었음을 입증하지 못하면 Job은 Runner 프로세스에 보유되고 행은 미해결로 남습니다. Runner
  재시작 뒤에는 소유 프로세스가 끝날 때 OS가 Job의 프로세스를 종료합니다(복구 판정이 가정으로 취급하며 끌 수 있는 조건부
  OS 보장). 백그라운드 orphan sweeper와 risk worker가 그런 행을 정리합니다. migration `0005`는 `runtime_capabilities`,
  `process_risk_requests`, 부분 index `ix_executions_tracked_unresolved`와 `ix_executions_tracked_anomaly`,
  `uq_approvals_lineage_successor`를 추가합니다.
* **검증 범위.** Windows에서 테스트로 검증됨: 실제 Job Object 동작(트리 종료, 소유 프로세스 사망, spawn 프로토콜의 crash
  지점). POSIX는 정책만 있습니다. 지원하지 않는 플랫폼은 tracked 명령을 untracked 실행으로 degrade하고, 알 수 없거나 사용할
  수 없는 capability는 fail-closed입니다(`PROCESS_TRACKING_UNAVAILABLE`). 그 런타임 동작은 검증하지 않았습니다. 검증하지
  않은 것: POSIX 런타임, 실제 Gradle/Maven 빌드, 실제 CLI end-to-end 실행, 무작위 race stress, EXPLAIN/generic plan, 실제
  deadlock 테스트.
* **Tracked execution의 알려진 제한.** spawn되었지만 아직 Job에 할당되지 않은 프로세스(CP2 구간)는 행이 "아무것도 spawn되지
  않음"과 구별되지 않습니다. 사용자 코드는 실행될 수 없었지만, Job이 없는 suspended 프로세스는 `NEVER_RAN` 종료 뒤에도 살아남을
  수 있으며, 이는 설계상 수용된 한계입니다. `SPAWNED_PROCESS_UNPROVEN`으로 기록된 pid 없는 행은 재시작으로 해소되지 않으며
  **지원되는 해소 경로가 없습니다**. 유효한 수용 기록에는 `pid`가 필요하므로 gate는 `accept_unproven_risk`를
  숨기고(`TARGET_WITHOUT_PID`) 직접 제출은 409 `OPTION_NOT_AVAILABLE`로 거부되며 아무것도 기록되지 않습니다. `abort`는
  Task만 종료하며 `DIRTY_UNRESOLVED`를 해소하지 않습니다. 행은 프로세스를 정리하고 데이터베이스를 직접 수정해야만
  정리할 수 있습니다(재시작 Windows 테스트는 별도의 미검증 후속 항목, 세부 내용은 `docs/workflow/08-human-gate_ko.md`와 `09-recovery_ko.md`).

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
