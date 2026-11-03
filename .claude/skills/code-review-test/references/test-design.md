# 테스트 설계 원칙

## 구조
- 결함 하나당 가능한 한 하나의 명확한 테스트 계약을 둔다.
- 정상 경로와 실패 경로를 별도 테스트로 분리한다.
- 테스트 이름에 조건과 기대 결과를 표현한다.
- 여러 구현 세부를 한 테스트에 과도하게 결합하지 않는다.

## 검증 대상
- 반환값뿐 아니라 외부에서 관찰 가능한 상태 전이를 검증한다.
- DB 변경은 commit 이후 새로 조회해 검증한다.
- transaction rollback과 부분 commit 여부를 검증한다.
- 동일 요청 재처리와 recovery의 멱등성을 검증한다.
- stale claim token이 상태를 변경하지 못하는지 검증한다.
- cancellation 이후 permit, lock, task, process, connection이 남지 않는지 검증한다.
- private 속성 검증이 필요하면 반드시 외부 동작 검증과 함께 사용한다.

## 동시성과 시간
- 동시성 테스트는 Event나 barrier로 실행 순서를 통제한다.
- timeout은 테스트가 무한 대기하지 않게 하는 안전장치로만 사용하고 판정 수단으로 쓰지 않는다.

## mock과 환경
- mock은 경계(외부 프로세스, 시계, 네트워크 등)를 제어하는 데만 쓰고, 테스트 대상 로직 자체는 mock하지 않는다.
- 플랫폼 의존 동작은 주입 가능한 오류나 adapter 경계에서 검증한다.
- 실제 CLI E2E는 명시적 승인 없이는 실행하지 않는다.

## 프로젝트 테스트 계층
- `tests/unit`: 순수 정책, 상태 계산, 예외 변환, semaphore 정산
- `tests/architecture`: import 방향, Router 규칙, 계층 제약
- `tests/api`: 요청 검증, 생략과 null, 상태 코드, DB 결과
- `tests/integration_pg`: transaction, lock, claim, recovery, 병렬 dispatch
- `tests/e2e_cli`: 실제 provider 통합. 명시적 요청이 있을 때만 실행한다.

결함이 속한 가장 낮은 계층에서 먼저 고정하고, 계층을 넘는 상호작용일 때만 상위 계층을 쓴다.
