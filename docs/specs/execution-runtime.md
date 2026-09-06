# Execution Runtime Specification v0.1

이 문서는 Atlas worker가 한 Task를 하나의 Run으로 실행할 때 따라야 할 runtime, isolation, recovery 계약을 정의합니다. Run record, heartbeat, restart reconciliation, branch/worktree isolation은 구현됐습니다. [ADR-010](../adr/0010-task-execution-isolation.md)의 filesystem/branch 격리는 `Accepted`이고 executor process 격리는 `Proposed`입니다. [ADR-009](../adr/0009-worker-process-supervision.md)는 `Proposed`이며 executor process invocation은 구현되지 않았습니다.

## Current and Target Status

| 항목 | 상태 | 설명 |
| --- | --- | --- |
| 사람이 Executor에 Task 전달 | Proven Manually | 사람이 prompt를 전달하고 Executor가 branch와 PR을 생성 |
| Atlas worker polling·claim·lease | Complete | Issue polling, Task persistence, atomic claim, lease TTL, 승인 회수 구현. live E2E는 [Verification Log](../verification-log.md) 참조 |
| Run record와 heartbeat | Complete | Run lifecycle, heartbeat, restart reconciliation 구현. [Verification Log](../verification-log.md) 참조 |
| branch와 worktree 격리 | Complete | Run별 전용 branch/worktree, 경계 검증, cleanup, reconciliation 구현 |
| executor process | Not Implemented | worktree는 준비되지만 아직 아무 process도 실행하지 않음 |
| self-hosted Claude Code invocation | Planned | Target MVP primary automated executor |
| tmux worker PoC | Planned | process persistence 용도; service manager가 아님 |
| systemd 또는 Docker supervision | Planned | stable operation에서 별도 결정 |

## Hosting Model

- Atlas worker는 운영자가 관리하는 always-available server에서 실행합니다.
- server를 사용하면 개인 PC는 켜져 있을 필요가 없습니다.
- PoC는 `tmux`를 사용할 수 있지만 Task나 Project isolation을 tmux pane에 위임하지 않습니다.
- stable operation은 systemd 또는 Docker 중 하나를 후속 결정해 startup, restart, logging, health, shutdown을 관리합니다.
- 이 문서는 server 주소, provider, account, provisioning 방법을 지정하지 않습니다.

## Run Boundary

모든 Run은 다음 리소스를 독점합니다.

| 리소스 | 요구사항 | 구현 상태 |
| --- | --- | --- |
| `task_id` | 원본 Task의 안정적인 ID | 구현됨 |
| `run_id` | 시도마다 새로 발급되는 unique ID | 구현됨 |
| branch | Run이 단독 수정하는 Task 전용 branch | 구현됨 |
| worktree/clone | 허용된 Project root 아래의 전용 mutable workspace | worktree 구현됨, clone 미채택 |
| executor process | Task마다 새로 시작하며 이전 conversation이나 shell state를 상속하지 않음 | 미구현 |
| log scope | stdout, stderr, event, validation evidence를 Run별로 분리 | event만 구현됨 |
| timeout | 시작 전에 고정하고 만료 시 cancellation과 cleanup 수행 | 미구현 |
| cancellation | 요청, 시각, actor, process 종료와 cleanup 결과 기록 | terminal 전이만 구현됨 |

여러 Project가 하나의 executor conversation을 공유하거나, 여러 Task가 mutable worktree를 공유하거나, 여러 Run이 같은 branch를 동시에 수정해서는 안 됩니다.

**한 Task에 active Run은 최대 하나입니다.** `Pending`과 `Running`을 active로 보며, operational store의 partial unique index가 database 수준에서 강제합니다.

## Run Lifecycle

| 상태 | 의미 | 다음 상태 |
| --- | --- | --- |
| `Pending` | Run record가 예약됐고 executor는 아직 시작되지 않음 | `Running`, terminal |
| `Running` | executor가 살아 있고 heartbeat가 갱신되는 중 | terminal |
| `Succeeded` | 실행이 성공적으로 끝남 | terminal |
| `Failed` | 실행이 실패했고 분류된 사유가 기록됨 | terminal |
| `Cancelled` | 사람 요청이나 정책으로 중단됨 | terminal |
| `Orphaned` | heartbeat가 끊겨 상태를 증명할 수 없음. recovery review 대상 | terminal |

Run 상태는 Task 상태와 다릅니다. Run이 `Succeeded`여도 Task는 사람 승인과 merge 전까지 `Completed`가 아닙니다. `Orphaned`는 "process 상태를 증명할 수 없으면 새 side effect를 허용하지 않고 recovery review로 기록한다"는 아래 Restart and Recovery 요구를 구현한 상태입니다.

전이 규칙은 다음과 같습니다.

- Run 생성은 승인된 Task, active claim, 유효한 lease, lease owner 일치를 모두 요구합니다.
- 첫 heartbeat가 `Pending`을 `Running`으로 올립니다. executor가 실제로 살아 있다는 증거이기 때문입니다.
- heartbeat는 Run owner만 보낼 수 있고 terminal Run에는 허용하지 않습니다.
- terminal 전이는 되돌릴 수 없습니다. 재시도는 새 `run_id`로 만들고 `previous_run_id`로 연결합니다.
- `Failed`와 `Orphaned`는 분류된 failure 사유를 요구합니다. 분류 어휘는 [Task State Machine](task-state-machine.md)의 Failure Taxonomy를 따릅니다.

## Conceptual Worker Lifecycle

1. [GitHub Event Ingestion](github-event-ingestion.md)이 approved 또는 queued Task 후보를 찾습니다.
2. Task Schema, actor permission, Project allowlist, scope, risk를 검증합니다.
3. 유효한 claim lease를 획득하고 unique Run ID를 생성합니다.
4. repository identity와 base revision을 확인한 뒤 전용 worktree 또는 clone과 branch를 준비합니다.
5. Run별 environment와 credential scope로 새 executor process를 시작합니다.
6. stdout, stderr, process metadata, heartbeat를 redaction boundary 안에서 수집합니다.
7. timeout, cancel, retry policy를 적용하고 child process까지 종료합니다.
8. 계획된 validation을 실행하고 evidence를 Run에 연결합니다.
9. 성공하면 branch를 push하고 PR을 생성하며 Issue에 mobile-friendly summary를 보고합니다.
10. 성공, 실패, timeout, cancel 각각의 cleanup을 수행하고 결과를 기록합니다.

## Minimum Run Record

```yaml
task_id: ATLAS-0001
run_id: run-0001
previous_run_id: null
worker_id: worker-redacted-id
lease_owner: worker-redacted-id
lease_expires_at: 2026-08-31T00:10:00Z
process_id: 12345
branch: docs/example
worktree_path: <worker-root>/<project>/<run-id>
started_at: 2026-08-31T00:00:00Z
last_heartbeat_at: 2026-08-31T00:00:30Z
timeout_at: 2026-08-31T01:00:00Z
cancellation_state: none
status: Running
```

실제 public event와 PR에는 server path, OS account, token, private repository 정보가 노출되지 않도록 path와 identity를 일반화하거나 생략합니다.

현재 구현이 저장하는 필드는 `run_id`, `task_id`, `fingerprint`, `claim_id`, `worker_id`, `status`, `created_at`, `heartbeat_at`, `started_at`, `finished_at`, `failure_category`, `failure_message`, `previous_run_id`입니다. `lease_owner`와 `lease_expires_at`은 `claim_id`로 claim record를 참조해 얻습니다. `process_id`, `branch`, `worktree_path`, `timeout_at`, `cancellation_state`는 worktree와 executor process를 만드는 후속 slice에서 추가합니다.

## Claim, Lease, and Idempotency

- Task claim은 atomic한 비교·갱신 또는 같은 효과의 primitive를 사용해야 합니다.
- active lease가 있는 Task에 새 Run을 만들지 않습니다.
- source Issue, approval/queue command, Task revision을 묶은 idempotency key를 유지합니다.
- 같은 poll result나 command를 반복 처리하면 기존 Task, Run, PR 결과를 반환합니다.
- PR delivery도 `task_id`, `run_id`, head branch 또는 delivery key로 중복을 방지합니다.
- lease expiry만으로 즉시 재실행하지 않고 worker heartbeat와 process ownership을 확인합니다. 현재 구현은 TTL 만료와 설정 가능한 grace period까지이며 heartbeat와 process identity 확인은 미구현입니다. 회수 시 이전 owner와 expiry를 event로 남깁니다.

## Restart and Recovery

worker 시작 시 또는 `reconcile` command로 다음 순서를 수행합니다.

1. 자신이 소유했거나 만료된 active Run record를 조회합니다.
2. 기록된 PID의 identity, start time, Run marker를 확인해 PID 재사용을 구분합니다.
3. process가 살아 있고 안전하게 재연결할 수 있으면 lease를 갱신하고 monitoring을 복구합니다.
4. process 상태를 증명할 수 없으면 새 side effect를 허용하지 않고 Run을 recovery review 또는 `Failed`로 기록합니다.
5. stale lease는 policy grace period 뒤 회수하되 이전 owner, expiry, 판단 근거를 event로 남깁니다.
6. orphan process와 stale worktree를 탐지해 강제 종료·삭제 전에 Project, Run, resolved path를 재검증합니다.
7. retry는 새 Run ID를 사용하고 `previous_run_id`와 redacted failure reason을 기록합니다.

복구나 retry 중 Acceptance Criteria, allowed scope, base revision을 조용히 바꾸지 않습니다. 변경이 필요하면 사람 승인 또는 revision workflow로 돌아갑니다.

### 현재 구현 범위

1, 4, 5, 7번은 구현됐습니다. active Run을 조회하고, heartbeat가 stale threshold를 넘으면 `Orphaned`로 기록하며, 판단 근거를 event로 남기고, 재시도는 `previous_run_id`로 연결합니다.

6번의 stale worktree 탐지도 구현됐습니다. 기록된 worktree 경로의 존재 여부, git worktree 등록 여부, 기대한 branch와의 일치, worker root 안에 있는지를 확인합니다. **불일치를 발견해도 임의로 복구하거나 삭제하지 않고** `workspace_recovery_required` event로 근거만 남깁니다. orphan process 탐지는 executor process가 없어 해당하지 않습니다.

**stale Run을 자동으로 재실행하지 않습니다.** 판정과 기록만 하고 새 Run 생성은 사람이나 상위 정책이 명시적으로 요청해야 합니다.

2, 3, 6번은 executor process와 worktree가 없어 수행할 수 없습니다. 판정 근거 event에 `process_identity_checked: false`를 남겨 이 한계를 감사 기록에 명시합니다. process identity 확인이 없는 동안 판정은 heartbeat 경과와 claim/lease 상태만으로 이루어집니다.

heartbeat interval과 stale threshold는 `RunConfig`로 설정합니다. 아래 Open Questions의 "heartbeat interval, recovery grace period"는 기본값을 두되 운영 측정 후 조정해야 합니다.

### 승인 회수와 claim 해제 이후의 cancellation (executor slice 요구사항)

**아직 구현되지 않았습니다.** executor process가 없어 이번 단계에서는 취소할 대상이 없기 때문입니다. executor를 실행하는 slice에서 반드시 함께 구현해야 하는 요구사항이므로 여기에 계약으로 남깁니다.

현재 Task 승인은 회수될 수 있고(`atlas:queued` label 제거, Issue 종료, 내용이 invalid로 변경) claim도 해제될 수 있습니다. 그런데 이때 **이미 실행 중인 executor는 그대로 살아 있습니다.** 승인 근거가 사라진 뒤에도 executor가 파일을 쓰고 branch를 밀고 PR을 만들 수 있다는 뜻입니다. 이는 [Constitution](../constitution.md)의 "사람 승인 없는 반영 금지"와 정면으로 충돌합니다.

따라서 executor slice는 다음을 만족해야 합니다.

- 승인 회수 또는 claim 해제를 관찰하면 해당 Task의 active Run에 cancellation을 요청합니다.
- executor process와 child process를 종료하고 종료 결과를 Run에 기록합니다.
- 종료 이후 side effect(파일 변경, branch push, PR 생성, comment 작성)를 허용하지 않습니다.
- 이미 발생한 side effect는 [Cleanup Matrix](#cleanup-matrix)의 timeout/cancel 항목에 따라 정리하고 결과를 기록합니다.
- executor가 즉시 멈추지 못하는 경우를 위해 graceful 종료와 강제 종료의 escalation 순서를 정합니다.
- side effect를 만들기 직전(branch push, PR 생성)에 승인과 claim이 여전히 유효한지 다시 확인합니다. reconciliation은 주기적이므로 그 사이의 창을 닫으려면 delivery 직전 재확인이 필요합니다.

현재 구현이 제공하는 것은 여기까지입니다.

- 승인이 회수되면 active claim이 함께 해제됩니다.
- claim이 해제된 Run은 reconciliation이 `Orphaned` 후보로 판정합니다.
- 판정 근거 event에 `claim_released`와 `lease_expired`가 남습니다.

즉 **기록은 되지만 실행은 멈추지 않습니다.** 이 간극을 executor slice 전에 닫아야 합니다.

## Run Workspace

Run마다 전용 branch와 git worktree를 준비합니다. [ADR-010](../adr/0010-task-execution-isolation.md)의 Accepted 범위입니다.

### Branch naming

`atlas/<task-id>/<run-id-short>` 형식입니다.

- `atlas/` namespace가 Atlas 소유임을 나타냅니다. 이 namespace 밖의 branch는 만들지도 삭제하지도 않습니다.
- Task ID와 Run ID는 `[A-Za-z0-9._-]` 밖의 문자를 치환해 sanitize합니다. Issue에서 온 값을 ref에 그대로 넣지 않습니다.
- `run_id`가 Run마다 고유하므로 같은 Task의 retry Run도 서로 다른 branch를 씁니다.
- `git check-ref-format`으로 유효성을 확인하고 이미 있는 branch면 거부합니다.
- `main`, `master`, `HEAD`, `trunk`, `develop`은 어떤 경우에도 Run branch로 쓰지 않습니다.

### Repository와 경로 경계

- 대상 repository의 local root를 명시적으로 받습니다. 현재 작업 디렉터리를 추측하지 않습니다.
- root가 실제 git repository이고 그 repository의 toplevel인지 확인합니다.
- `origin` remote가 있으면 Task repository와 일치하는지 확인합니다. remote가 없으면 network를 쓰지 않고 통과시킵니다.
- remote 비교는 suffix가 아니라 canonical `owner/repo` 정확 일치입니다. HTTPS와 SSH 형식을 모두 parsing하고 `.git`을 제거하며 host가 GitHub인지 확인합니다. `https://github.com/evil/owner/repo.git`처럼 경로 조각이 두 개가 아닌 URL과 해석할 수 없는 remote는 거부합니다.
- worktree는 Project별 worker root 아래에만 만듭니다. 기본값은 `<repository-root>/.atlas/worktrees`이며 operator가 바꿀 수 있습니다. 이 경로는 대상 repository에서 ignore돼야 합니다.
- 모든 경로는 `resolve()` 후 worker root 아래인지 확인합니다. `resolve()`가 symlink를 따라가므로 symlink escape도 함께 걸립니다.

### 단계별 lifecycle

git side effect를 database transaction 안에서 잡지 않습니다. 대신 단계를 나눠 부분 실패를 식별합니다.

| 단계 | 의미 |
| --- | --- |
| `none` | 아직 workspace가 없음 |
| `preparing` | branch와 경로를 확정해 기록함. git 작업은 이 뒤에 수행 |
| `ready` | git 작업과 검증이 모두 끝남 |
| `failed` | git 작업 중 실패. 기록된 branch/경로가 정리 대상 |
| `removed` | worktree를 제거함 |

`preparing` 기록이 git보다 먼저 남으므로, 중간에 프로세스가 죽어도 어떤 branch와 경로를 정리해야 하는지 database만 보고 알 수 있습니다. 이것이 orphan 리소스 식별의 근거입니다.

### 생성 후 검증

worktree를 만든 뒤 다음을 모두 확인하고, 하나라도 어긋나면 `ready`로 올리지 않습니다.

- `git rev-parse --show-toplevel`이 기대한 worktree 경로와 같습니다.
- 현재 branch가 계획한 branch와 같습니다.
- HEAD가 생성 시점에 고정한 base revision과 같습니다.
- resolved 경로가 worker root 아래입니다.
- `git rev-parse --git-common-dir`가 대상 repository와 같습니다.

### Idempotency와 재사용 시 재검증

같은 Run에 workspace 생성을 두 번 호출해도 중복 branch나 worktree를 만들지 않고 기존 workspace를 돌려줍니다. 판정 근거는 operational store의 `workspace_status`이므로 프로세스를 재시작해도 같은 Run의 workspace를 재식별합니다.

**`ready` 기록만 믿고 돌려주지 않습니다.** executor는 이 경로를 process working directory로 신뢰할 예정이므로, stale하거나 손상된 workspace를 정상으로 반환하면 안 됩니다. 재사용 전에 저장된 `branch`와 `worktree_path`로 실제 상태를 다시 확인합니다.

| 확인 항목 | 의미 |
| --- | --- |
| `path_exists` | 기록된 경로가 실제로 있습니다 |
| `path_within_root` | worker root 경계 안입니다 |
| `registered_worktree` | 이 repository에 등록된 worktree입니다 |
| `branch_matches` | 현재 branch가 기록된 branch와 같습니다 |
| `toplevel_matches` | `rev-parse --show-toplevel`이 기록된 경로와 같습니다 |
| `repository_matches` | `git-common-dir`가 대상 repository와 같습니다 |

생성 직후 검증과 달리 **HEAD가 base revision과 같은지는 보지 않습니다.** 이미 작업이 진행돼 commit이 쌓였을 수 있고 그것은 정상입니다.

하나라도 어긋나면 **자동으로 복구하거나 다시 만들지 않고** `workspace_recovery_required`로 거부합니다. 어떤 항목이 깨졌는지는 boolean으로만 event에 남기므로 절대 경로가 노출되지 않습니다.

### Ownership

Atlas가 만들었다고 **증명할 수 있는** 리소스만 정리합니다. 증명은 다음 두 가지가 함께 성립할 때만 인정합니다.

1. branch가 `atlas/` namespace에 있습니다.
2. operational store에 이 Run이 그 branch와 경로를 만들었다는 기록이 있습니다.

둘 중 하나라도 어긋나면 삭제하지 않고 거부합니다. 사용자가 만든 branch는 어떤 경우에도 삭제하지 않습니다.

## Cleanup Matrix

| 종료 유형 | process | worktree/clone | branch | logs/artifacts |
| --- | --- | --- | --- | --- |
| 성공/PR 생성 | child까지 종료 | push와 evidence 확인 후 제거 가능 | PR lifecycle 동안 유지 | retention policy 적용 |
| validation 실패 | 종료 | 진단 기간 동안 제한 보존 후 제거 | retry 판단까지 보존 가능 | redacted evidence 보존 |
| timeout/cancel | graceful 후 강제 종료 | side effect 확인 후 제거 | push되지 않은 상태를 기록 | 원인과 cleanup 결과 보존 |
| worker crash | startup reconciliation | 자동 삭제 전 ownership 확인 | concurrent Run 금지 | heartbeat 중단을 기록 |

cleanup 실패는 성공으로 숨기지 않으며 별도 상태와 operator action을 남깁니다.

### 구현된 cleanup 정책

Run 상태별 branch 보존 여부입니다. 작업 내용이 남아 있을 수 있으므로 **기본은 모두 보존**입니다.

| Run 상태 | worktree | branch | 근거 |
| --- | --- | --- | --- |
| `Succeeded` | 제거 | 보존 | PR lifecycle 동안 필요 |
| `Failed` | 제거 | 보존 | retry 판단까지 필요 |
| `Cancelled` | 제거 | 보존 | push되지 않은 작업이 남아 있을 수 있음 |
| `Orphaned` | 제거 | 보존 | 사람 확인 전까지 판단 근거 |

추가 규칙입니다.

- 저장되지 않은 변경이 있는 worktree는 제거하지 않습니다. operator가 명시적으로 허용해야 제거합니다.
- branch 삭제는 기본적으로 하지 않습니다. operator가 명시적으로 요청해야 하며 그때도 Atlas namespace 안에서만 삭제합니다.
- 실행 중인 Run의 workspace는 정리하지 않습니다. terminal 상태여야 합니다.
- cleanup 실패는 성공으로 처리하지 않고 event로 남긴 뒤 실패를 그대로 보고합니다.

## Open Questions

- lease duration, heartbeat interval, recovery grace period
- worktree와 clone의 Project별 선택 기준
- default timeout과 cancel escalation 순서
- stable supervisor로 systemd와 Docker 중 무엇을 선택할지
- log와 failed workspace retention 기간
- 승인 회수와 delivery 사이의 재확인 시점(주기적 reconciliation만으로 충분한지, side effect 직전 재확인이 필요한지)
