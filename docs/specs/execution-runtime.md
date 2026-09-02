# Execution Runtime Specification v0.1

이 문서는 Atlas worker가 한 Task를 하나의 Run으로 실행할 때 따라야 할 runtime, isolation, recovery 계약을 정의합니다. [ADR-009](../adr/0009-worker-process-supervision.md)와 [ADR-010](../adr/0010-task-execution-isolation.md)은 아직 `Proposed`이며, worker와 executor invocation은 구현되지 않았습니다.

## Current and Target Status

| 항목 | 상태 | 설명 |
| --- | --- | --- |
| 사람이 Executor에 Task 전달 | Proven Manually | 사람이 prompt를 전달하고 Executor가 branch와 PR을 생성 |
| Atlas worker polling·claim·lease | Complete | Issue polling, Task persistence, atomic claim, lease TTL, 승인 회수 구현. live E2E는 [Verification Log](../verification-log.md) 참조 |
| Run record와 process 관리 | Not Implemented | Run 생성, worktree, executor process 없음 |
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

| 리소스 | 요구사항 |
| --- | --- |
| `task_id` | 원본 Task의 안정적인 ID |
| `run_id` | 시도마다 새로 발급되는 unique ID |
| branch | Run이 단독 수정하는 Task 전용 branch |
| worktree/clone | 허용된 Project root 아래의 전용 mutable workspace |
| executor process | Task마다 새로 시작하며 이전 conversation이나 shell state를 상속하지 않음 |
| log scope | stdout, stderr, event, validation evidence를 Run별로 분리 |
| timeout | 시작 전에 고정하고 만료 시 cancellation과 cleanup 수행 |
| cancellation | 요청, 시각, actor, process 종료와 cleanup 결과 기록 |

여러 Project가 하나의 executor conversation을 공유하거나, 여러 Task가 mutable worktree를 공유하거나, 여러 Run이 같은 branch를 동시에 수정해서는 안 됩니다.

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

## Claim, Lease, and Idempotency

- Task claim은 atomic한 비교·갱신 또는 같은 효과의 primitive를 사용해야 합니다.
- active lease가 있는 Task에 새 Run을 만들지 않습니다.
- source Issue, approval/queue command, Task revision을 묶은 idempotency key를 유지합니다.
- 같은 poll result나 command를 반복 처리하면 기존 Task, Run, PR 결과를 반환합니다.
- PR delivery도 `task_id`, `run_id`, head branch 또는 delivery key로 중복을 방지합니다.
- lease expiry만으로 즉시 재실행하지 않고 worker heartbeat와 process ownership을 확인합니다. 현재 구현은 TTL 만료와 설정 가능한 grace period까지이며 heartbeat와 process identity 확인은 미구현입니다. 회수 시 이전 owner와 expiry를 event로 남깁니다.

## Restart and Recovery

worker 시작 시 다음 순서로 reconciliation합니다.

1. 자신이 소유했거나 만료된 active Run record를 조회합니다.
2. 기록된 PID의 identity, start time, Run marker를 확인해 PID 재사용을 구분합니다.
3. process가 살아 있고 안전하게 재연결할 수 있으면 lease를 갱신하고 monitoring을 복구합니다.
4. process 상태를 증명할 수 없으면 새 side effect를 허용하지 않고 Run을 recovery review 또는 `Failed`로 기록합니다.
5. stale lease는 policy grace period 뒤 회수하되 이전 owner, expiry, 판단 근거를 event로 남깁니다.
6. orphan process와 stale worktree를 탐지해 강제 종료·삭제 전에 Project, Run, resolved path를 재검증합니다.
7. retry는 새 Run ID를 사용하고 `previous_run_id`와 redacted failure reason을 기록합니다.

복구나 retry 중 Acceptance Criteria, allowed scope, base revision을 조용히 바꾸지 않습니다. 변경이 필요하면 사람 승인 또는 revision workflow로 돌아갑니다.

## Cleanup Matrix

| 종료 유형 | process | worktree/clone | branch | logs/artifacts |
| --- | --- | --- | --- | --- |
| 성공/PR 생성 | child까지 종료 | push와 evidence 확인 후 제거 가능 | PR lifecycle 동안 유지 | retention policy 적용 |
| validation 실패 | 종료 | 진단 기간 동안 제한 보존 후 제거 | retry 판단까지 보존 가능 | redacted evidence 보존 |
| timeout/cancel | graceful 후 강제 종료 | side effect 확인 후 제거 | push되지 않은 상태를 기록 | 원인과 cleanup 결과 보존 |
| worker crash | startup reconciliation | 자동 삭제 전 ownership 확인 | concurrent Run 금지 | heartbeat 중단을 기록 |

cleanup 실패는 성공으로 숨기지 않으며 별도 상태와 operator action을 남깁니다.

## Open Questions

- lease duration, heartbeat interval, recovery grace period
- worktree와 clone의 Project별 선택 기준
- default timeout과 cancel escalation 순서
- stable supervisor로 systemd와 Docker 중 무엇을 선택할지
- log와 failed workspace retention 기간
