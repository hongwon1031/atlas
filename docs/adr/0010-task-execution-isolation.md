# ADR-010: Task Execution Isolation

- Status: Partially Accepted
- Date: 2026-08-31
- Accepted: 2026-09-06 (filesystem/branch isolation), 2026-09-06 (process isolation, timeout, cancellation, process identity)
- Decision owners: Project owner

## Context

Atlas가 여러 Project와 Task를 처리하면 conversation, mutable filesystem, branch, process, log가 섞일 위험이 있습니다. 논리적인 Task ID만으로는 동시 실행 충돌, stale process, 다른 Project context 유출을 막을 수 없습니다.

branch와 worktree isolation, executor process isolation, timeout, cancellation, process identity가 구현됐습니다. provider별 정책과 credential injection은 아직 결정되지 않았습니다.

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

### Accepted (2026-09-06, process isolation)

Project owner가 executor runtime 구현을 지시하면서 process 격리 범위를 추가로 승인했습니다.

- Run마다 새 OS process를 시작하며 이전 shell, conversation, 환경을 재사용하지 않습니다.
- executor process의 작업 디렉터리는 반드시 해당 Run의 검증된 worktree입니다. repository root나 main worktree에서 실행하지 않으며 cwd fallback도 두지 않습니다.
- 환경은 상속하지 않고 allowlist로 구성합니다. 필요한 OS 기본 변수만 전달합니다.
- 모든 실행에 timeout이 있습니다. 만료하면 graceful 종료를 시도하고 grace period 뒤 강제 종료하며, 실패 분류는 `timeout`입니다.
- cancellation은 명시적 상태로 관리합니다. 사용자 요청, 승인 회수, claim 상실이 모두 취소 trigger입니다.
- 종료는 process 단위가 아니라 Run 단위 process tree로 수행합니다.
- process identity는 PID와 process 시작 시각을 함께 저장하고 확인합니다. **identity가 일치하지 않거나 확인할 수 없으면 절대 종료하지 않습니다.**
- stdout과 stderr는 크기 제한이 있는 Run별 log artifact로 수집합니다. raw 출력 전체를 event에 저장하지 않고 redaction을 적용합니다.

### Accepted (2026-09-06, Claude Code 실행 정책)

Project owner가 실제 Claude Code adapter 구현을 지시하면서 Claude Code에 한해 실행 정책을 승인했습니다.

- Claude Code는 비대화형(`--print`)으로만 실행하고 TTY에 의존하지 않습니다.
- prompt는 argv가 아니라 stdin으로 전달합니다. 사용자 유래 텍스트를 argv에 넣지 않습니다.
- 도구를 파일 편집 집합으로 제한하고 shell 실행 도구를 주지 않습니다. 권한 우회 옵션을 쓰지 않습니다.
- 세션을 디스크에 남기지 않습니다. Run 하나가 곧 대화 하나입니다.
- provider 세부사항은 adapter 경계 안에만 둡니다. core contract는 provider-neutral로 유지합니다.
- process 수명주기는 기존 executor runtime을 재사용합니다. provider별 process manager를 만들지 않습니다.

### Accepted (2026-09-06, validation 실행 경계)

Project owner가 validation pipeline 구현을 지시하면서 검증 실행 경계를 승인했습니다.

- 검증 명령은 repository에서 발견한 근거로만 선택합니다. 임의 shell 명령을 만들지 않습니다.
- 검증 process도 Run의 검증된 worktree에서만 실행하고 argv list로만 띄웁니다.
- 검증 process 환경은 executor보다 좁습니다. provider credential 환경을 넘기지 않습니다.
- dependency를 설치하지 않습니다.
- 검증 process도 같은 process identity·timeout·cancellation·reconciliation 경로를 씁니다. 별도 process manager를 만들지 않습니다.

### Accepted (2026-09-06, 게시 경계)

Project owner가 git publication 구현을 지시하면서 게시 경계를 승인했습니다.

- Atlas는 draft PR만 만들고 merge하지 않습니다. 사람이 최종 gate입니다.
- force push를 하지 않습니다. remote 충돌은 덮어쓰지 않고 recovery로 남깁니다.
- 보호 branch에 push하지 않습니다.
- push refspec은 명시적이고 remote identity를 정확히 검증합니다.
- 검증이 승인한 경로만 commit합니다.
- 외부 side effect마다 durable checkpoint를 남기고 모호한 상태를 자동으로 덮어쓰지 않습니다.

### 계속 Proposed

- Codex의 호출 형식과 옵션.
- credential injection과 회수 절차. 현재는 로그인된 CLI 세션을 쓰며 Atlas가 credential을 다루지 않습니다.
- clone per Task를 선택할 기준.
- cloud나 원격 host에서의 실행 정책.

### Open

- worktree와 log retention 기간, disk 사용량 정책.
- 여러 Project를 다룰 때 worker root를 Project별로 분리할 방식.
- 동시에 실행할 수 있는 Run 수와 자원 한도.

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
- [x] Project owner가 executor process isolation 범위를 승인
- [x] concurrent process ownership acceptance test 작성
- [x] orphan process recovery 판정 절차 구현 (자동 종료 없이 근거만 기록)
- [ ] provider별 executor 정책과 credential injection 결정
- [ ] worktree와 log retention, disk 사용량 정책 결정
- [ ] 동시 실행 Run 수와 자원 한도 결정
