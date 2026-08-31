# ADR-010: Task Execution Isolation

- Status: Proposed
- Date: 2026-08-31
- Decision owners: Project owner

## Context

Atlas가 여러 Project와 Task를 처리하면 conversation, mutable filesystem, branch, process, log가 섞일 위험이 있습니다. 논리적인 Task ID만으로는 동시 실행 충돌, stale process, 다른 Project context 유출을 막을 수 없습니다.

현재 자동 Runner와 isolation enforcement는 구현되지 않았습니다.

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

- [ ] Project owner가 Task/Run isolation 제안을 승인
- [ ] branch naming, worktree root, Run ID format 결정
- [ ] concurrent branch lock과 process ownership acceptance test 작성
- [ ] success, failure, cancel, timeout별 cleanup matrix 정의
- [ ] stale worktree와 orphan process recovery 절차 검증
