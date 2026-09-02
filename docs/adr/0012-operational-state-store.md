# ADR-012: Operational State Store

- Status: Accepted
- Date: 2026-09-02
- Accepted: 2026-09-02
- Decision owners: Project owner

Project owner가 PR #6 리뷰 과정에서 SQLite 선택의 장단점과 되돌리기 비용을 확인한 뒤 명시적으로 승인했습니다.

## Context

[ADR-005](0005-project-memory-storage.md)는 Task, Run, event, availability 같은 운영 상태를 relational database에 저장하는 방향을 `Proposed`로 제안했지만 구체적인 기술은 정하지 않았습니다. Issue polling과 claim을 구현하려면 process 재시작 후에도 유지되는 store와 원자적 claim이 필요하므로 이제 기술을 확정해야 합니다.

요구사항은 다음과 같습니다.

- process를 재시작해도 Task, revision, claim, lease가 유지됩니다.
- 같은 Task를 두 worker가 동시에 claim하면 하나만 성공합니다.
- [ADR-011](0011-initial-implementation-language.md)의 런타임 dependency 0개 원칙을 유지합니다.
- 단일 always-available server의 단일 worker에서 시작합니다.

## Decision

- 초기 operational state store는 Python 표준 라이브러리 `sqlite3`를 사용합니다.
- database 파일 경로는 `ATLAS_DB_PATH` 환경변수 또는 `--database` 옵션으로 주입하고 기본값은 작업 디렉터리의 `atlas.db`입니다. repository에 commit하지 않습니다.
- claim은 `BEGIN IMMEDIATE` 트랜잭션 안에서 수행하고, `claims(task_id) WHERE released_at IS NULL` partial unique index로 "한 Task에 active claim 하나"를 database 수준에서 강제합니다.
- 파일 database에는 `journal_mode = WAL`을 적용해 reader와 writer 경합을 줄입니다.
- append-only `events` 테이블로 registration, claim, lease 만료, release를 감사합니다. [ADR-004](0004-workflow-engine.md)의 append-only event 요구를 따릅니다.
- canonical documentation은 계속 Git Markdown입니다. 이 store는 운영 상태만 담고 Constitution·PRD·spec·ADR을 대체하지 않습니다.
- PostgreSQL, Redis, ORM은 도입하지 않습니다.

### 재검토 트리거

다음 중 하나가 결정되면 즉시 이 ADR을 재검토합니다.

- Control Plane과 worker를 서로 다른 host로 분리 배포하기로 결정 (Open Decision의 hosting 위치와 직결됩니다)
- 여러 worker가 같은 Task queue를 동시에 처리해야 하는 요구 발생
- store를 network로 접근해야 하는 요구 발생

## Alternatives Considered

### SQLite (표준 라이브러리)

- 장점: dependency 0개를 유지하고, 파일 하나로 backup·검사·삭제가 가능하며, 트랜잭션과 partial unique index로 atomic claim을 표현할 수 있습니다.
- 단점: 단일 파일 write lock 때문에 동시 writer 처리량이 낮고, 여러 host에서 같은 store를 공유할 수 없습니다.

### PostgreSQL

- 장점: 다중 worker와 원격 접근에 적합하고 advisory lock, `SELECT ... FOR UPDATE SKIP LOCKED` 같은 도구가 있습니다.
- 단점: server 운영, credential, network 경계가 추가되며 dependency 0개 원칙이 깨집니다. 현재 요구사항인 단일 worker에는 과도합니다.

### JSON 파일 또는 Git 파일

- 장점: 별도 기술 없이 사람이 읽을 수 있습니다.
- 단점: 원자적 claim과 동시성 제어를 직접 구현해야 하고, [ADR-005](0005-project-memory-storage.md)가 빈번한 Run event에 부적합하다고 이미 판단했습니다.

## Consequences

- 재시작 가능한 Task 상태와 원자적 claim을 dependency 없이 확보합니다.
- 동시 worker 처리량은 SQLite write lock에 제한됩니다. 현재 범위(한 번에 한 Task)에서는 문제가 되지 않지만 다중 worker 확장 시 재평가해야 합니다.
- 여러 host가 같은 store를 공유해야 하면 이 결정을 대체하는 ADR이 필요합니다.
- schema 변경에는 migration 절차가 필요합니다. 현재는 `schema_meta.schema_version`만 기록하고 migration runner는 만들지 않았습니다.
- database 파일에는 Issue 본문에서 정규화한 Task 내용이 들어갑니다. 파일 권한과 위치는 운영자가 관리해야 하며 public repository에 포함하지 않습니다.

## Security Impact

- database 파일을 repository에 commit하지 않도록 `.gitignore`에 등록합니다.
- credential, token, provider 응답 원문은 store에 기록하지 않습니다. 저장하는 값은 정규화된 Task, revision hash, claim/lease metadata, 분류된 event detail입니다.
- event detail에는 worker 식별자와 lease 시각만 남기고 raw provider 오류를 남기지 않습니다.
- Workspace와 Project 경계는 registration 이전 단계인 repository allowlist에서 강제합니다. store는 경계를 우회하는 조회 경로를 제공하지 않습니다.
- 다중 Workspace를 지원할 때 namespace 분리 또는 별도 database 파일이 필요한지 재검토해야 합니다.

## Follow-up Tasks

- [x] Project owner가 SQLite operational store 결정을 승인
- [ ] schema migration 절차와 version 검사 정의
- [ ] database 파일 retention, backup, 권한 정책 결정
- [ ] 다중 worker 동시성 요구가 생길 때 PostgreSQL 재평가 기준 수립
- [ ] Workspace별 store 분리 여부 결정
