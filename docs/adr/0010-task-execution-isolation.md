# ADR-010: Task Execution Isolation

- Status: Partially Accepted
- Date: 2026-08-31
- Accepted: 2026-09-06 (filesystem/branch isolation 부분)
- Decision owners: Project owner

## Context

Atlas가 여러 Project와 Task를 처리하면 conversation, mutable filesystem, branch, process, log가 섞일 위험이 있습니다. 논리적인 Task ID만으로는 동시 실행 충돌, stale process, 다른 Project context 유출을 막을 수 없습니다.

branch와 worktree isolation은 구현됐습니다. executor process isolation은 구현되지 않았습니다.

## Accepted Scope (2026-09-06)

Project owner가 worktree/branch isolation 구현을 지시하면서 **filesystem과 branch 격리 부분만** 승인했습니다. process와 executor 관련 결정은 실행할 executor가 아직 없으므로 `Proposed`로 남깁니다.

### Accepted

- Run마다 unique Task ID와 unique Run ID를 가집니다.
- Run마다 dedicated branch를 사용합니다. 이름은 `atlas/<task-id>/<run-id-short>`이며 Atlas namespace(`atlas/`) 밖의 branch는 만들지도 삭제하지도 않습니다.
- Run마다 dedicated git worktree를 사용합니다. clone은 이번에 채택하지 않았고 필요해지면 별도로 결정합니다.
- worktree의 resolved path는 Project별 worker root 아래여야 하며 path traversal과 symlink escape를 거부합니다.
- `main`을 포함한 보호 branch를 직접 checkout하거나 수정하지 않습니다.
- Atlas가 만들었다고 증명할 수 있는 리소스만 정리합니다. 증명은 Atlas branch namespace와 operational store의 provenance 기록이 함께 성립할 때만 인정합니다.
- 여러 Task가 하나의 mutable worktree를 공유하지 않고, 여러 Run이 같은 branch를 동시에 수정하지 않습니다.

### 계속 Proposed

- dedicated executor process와 이전 shell/conversation 재사용 금지. executor를 실행하는 slice에서 결정합니다.
- explicit timeout과 cancellation state.
- clone per Task를 선택할 기준.
- credential injection과 회수 절차.
- orphan process 탐지와 강제 종료.

### Open

- worktree retention 기간과 disk 사용량 정책.
- 여러 Project를 다룰 때 worker root를 Project별로 분리할 방식.

## Proposed Decision

모든 Task execution은 최소한 다음 경계를 가집니다.

- unique Task ID와 unique Run ID
- dedicated branch
- dedicated Git worktree 또는 clone
- dedicated executor process
- dedicated log scope와 artifact scope
- explicit timeout과 cancellation state

다음을 금지합니다.

- 여러 Project가 하나의 executor conversation을 공유하는 것
- 여러 Task가 하나의 mutable worktree를 공유하는 것
- 여러 Run이 같은 branch를 동시에 수정하는 것
- 이전 Run의 shell, process, working memory를 다음 Run의 실행 컨텍스트로 재사용하는 것

worker는 Run 종료 시 child process를 중지하고, credential을 해제하고, temporary file과 artifact retention을 적용하고, 안전한 worktree만 제거합니다. 실패한 cleanup은 숨기지 않고 recovery 대상으로 기록합니다.

## Alternatives Considered

### Shared checkout and persistent conversation

- 장점: setup 시간과 context 재구성 비용이 작을 수 있습니다.
- 단점: branch 충돌, hidden state, Project leakage, 재현 불가능성이 발생합니다.

### Worktree per Task

- 장점: object database를 공유하면서 branch와 working directory를 분리합니다.
- 단점: stale worktree와 branch reference cleanup이 필요합니다.

### Clone per Task

- 장점: filesystem 경계가 단순하고 독립적입니다.
- 단점: network와 disk 사용량이 크고 credential 사용 지점이 늘어납니다.

## Consequences

- Run을 Task, branch, process, log와 일대일로 추적할 수 있습니다.
- 작업 준비와 cleanup 비용이 증가합니다.
- worktree와 clone 중 선택은 Project policy와 Runner capability에 따라 달라질 수 있습니다.
- retry는 새 Run ID와 process를 사용하고 이전 Run과 실패 이유를 참조해야 합니다.

## Security Impact

- worktree 또는 clone의 resolved path는 Project별 허용 root 아래에 있어야 합니다.
- path traversal, symlink escape, forbidden path, repository identity를 실행 전에 검증합니다.
- process environment에는 해당 Run에 필요한 credential만 주입하고 종료 후 회수합니다.
- log와 artifact는 Run별로 분리하고 secret과 개인정보를 redact하며 retention 만료 후 정리합니다.

## Follow-up Tasks

- [x] Project owner가 filesystem/branch isolation 범위를 승인
- [x] branch naming(`atlas/<task-id>/<run-id-short>`)과 worktree root 결정
- [x] success, failure, cancel, orphaned별 branch 보존 정책 정의
- [x] stale worktree recovery 판정 절차 구현 (임의 복구 없이 근거만 기록)
- [ ] Project owner가 executor process isolation 범위를 승인
- [ ] concurrent process ownership acceptance test 작성
- [ ] orphan process recovery 절차 검증
- [ ] worktree retention과 disk 사용량 정책 결정
