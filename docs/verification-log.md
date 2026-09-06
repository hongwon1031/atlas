# Verification Log

구현된 기능을 실제 환경에서 확인한 기록입니다. 단위 테스트로 대체할 수 없는 검증, 특히 실제 GitHub API를 사용한 end-to-end 확인을 남깁니다.

[ADR-001](adr/0001-documentation-source-of-truth.md)에 따라 merge된 이 문서가 canonical 기록입니다. Pull Request comment는 검토 과정의 근거일 뿐 시간이 지나면 찾기 어렵습니다.

각 항목은 무엇을 확인했는지, 무엇을 확인하지 못했는지 함께 기록합니다.

## 2026-09-02 — Polling, 등록, atomic claim, 승인 회수

- 대상 구현: `src/atlas/{polling,store,intake,issue_source}.py`
- 검증 방법: 실제 GitHub REST API + `hongwon1031/atlas` Issue #7
- 관련 결정: [ADR-008](adr/0008-initial-github-event-ingestion.md), [ADR-012](adr/0012-operational-state-store.md)

Issue #7은 이 검증을 위해 만든 Atlas Task Form Issue이며 `atlas:queued` label을 부착했습니다.

### 확인된 항목

| 단계 | 확인 내용 | 결과 |
| --- | --- | --- |
| 단건 검증 | `show`가 Issue를 `Draft` Task로 변환 | `ATLAS-0007`, Acceptance Criteria 4건, Validation 3건, scope가 path/operation으로 분류됨 |
| polling 등록 | 후보 인식과 Task 저장 | `registered` 1건 |
| 반복 polling | 같은 revision 재관찰 | `unchanged` 1건, 중복 Task 생성 없음 |
| 승인 상태 | approval이 지속 상태로 저장됨 | `approved=true`, `approval_signal=queue_label:atlas:queued` |
| atomic claim | Task claim과 lease 발급 | `claim_id` 발급, lease 만료 시각 기록 |
| lease 배타성 | active lease 중 다른 worker의 claim | 거부(`NoClaimableTask`) |
| 승인 회수 | `atlas:queued` label 제거 후 polling | `revoked`, `revoke_reason=queue_label_absent` |
| claim 해제 | 회수 시 진행 중 claim 처리 | active claim이 `approval_revoked:queue_label_absent`로 해제 |
| 회수 후 claim | 승인 없는 Task의 claim | 거부 |
| 승인 복구 | label 재부착 후 polling과 claim | 재승인되어 claim 성공 |
| Issue 종료 | Issue를 닫은 뒤 polling | `revoked`, `revoke_reason=issue_not_open`, claim 해제 |

`state=all` 목록 조회는 이 검증에서 함께 확인됐습니다. 닫힌 Issue가 목록에 나타나야 승인 회수가 가능하며, Issue 종료 단계에서 그대로 동작했습니다.

append-only event log에 `task_registered`, `approval_granted`, `task_claimed`, `approval_revoked`, `claim_released`가 순서대로 기록됐습니다.

### 발견한 provider 특성

Issue를 닫은 **직후** polling pass는 변경을 관찰하지 못했습니다(`scanned=0`). 원인을 확인한 결과 cursor 로직 문제가 아니었습니다.

- 저장된 cursor가 Issue의 `updated_at`보다 이전이었고 조건상 포함되어야 했습니다.
- 같은 `since` 값으로 직접 목록을 조회하면 닫힌 Issue가 정상 반환됐습니다.
- 다음 polling pass에서 정상적으로 회수됐습니다.

GitHub Issue 목록 endpoint의 eventual consistency이며, 승인 회수 지연은 `polling interval + provider 인덱싱 지연`으로 보아야 합니다. [GitHub Event Ingestion](specs/github-event-ingestion.md)에 계약으로 기록했습니다.

### 확인하지 못한 항목

- 장시간 `--watch` 실행의 안정성과 실제 interval 준수
- GitHub rate limit에 실제로 도달했을 때의 backoff 동작
- 서로 다른 OS process 사이의 claim 경쟁 (같은 process 내 8개 thread 경쟁은 단위 테스트로 확인)
- 휴대전화에서 Atlas Task Form을 작성하는 사용성 (이 검증의 Issue는 API로 생성)
- `atlas:queued` label을 추가한 actor의 권한 재확인 (미구현)

## 검증 기록 작성 규칙

- 실제 외부 시스템을 사용한 검증은 이 문서에 남깁니다. 단위 테스트만으로 확인한 내용은 남기지 않습니다.
- 확인한 항목과 확인하지 못한 항목을 항상 함께 적습니다.
- 검증 중 발견한 외부 시스템의 동작 특성은 원인 분석과 함께 기록하고, 계약에 영향을 주면 해당 spec도 갱신합니다.
- server 주소, token, 개인 정보, private repository 세부사항은 기록하지 않습니다.

## 2026-09-06 — Run lifecycle, heartbeat, restart reconciliation

- 대상 구현: `src/atlas/store.py`(runs), `src/atlas/reconciliation.py`, `src/atlas/config.py`
- 검증 방법: 로컬 SQLite database + 별도 OS process
- 관련 계약: [Execution Runtime](specs/execution-runtime.md)의 Run Boundary, Run Lifecycle, Restart and Recovery

### schema migration (v2 → v3)

기존 v2 database에서 실제로 migration을 수행했습니다.

- v2 상태를 재현했습니다: `runs` 테이블 삭제, `events.run_id` 컬럼 제거, `schema_version`을 `2`로 되돌림
- store를 다시 열자 `schema_version=3`, `runs` 테이블 생성, `events.run_id` 추가가 이루어졌습니다
- 기존 Task 1건과 active claim이 그대로 보존됐습니다
- migration 직후 Run 생성이 정상 동작했습니다

### restart simulation (worker crash)

heartbeat를 남기고 프로세스가 사라진 상황을 재현했습니다.

- 새 프로세스가 store를 다시 열어 `Running` Run을 발견했습니다
- stale threshold(300초)를 넘긴 시점에 reconcile하자 `Orphaned`로 전이하고 `failure_category=worker_lost`를 기록했습니다
- 판단 근거가 event에 남았습니다: `heartbeat_age_seconds`, `stale_after_seconds`, `lease_expired`, `process_identity_checked: false`
- **자동 재실행은 하지 않았습니다.** active Run이 없어진 상태로 남았고, 이후 명시적 요청으로 만든 retry Run이 `previous_run_id`로 이전 Run을 참조했습니다

### 별도 OS process 동시성

이전 slice에서 "다중 process 경쟁 미검증"으로 남겨둔 항목을 해소했습니다.

- 서로 다른 OS process 6개가 동시에 같은 Task에 `start_run`을 시도했습니다
- 정확히 1개만 성공했고 나머지 5개는 `active_run_exists`로 거부됐습니다
- 최종 Run 수는 1건이었습니다

### CLI

`run-start`, `run-heartbeat`, `run-finish`, `runs`, `reconcile`을 실제로 실행해 확인했습니다. 중복 start 거부, 잘못된 worker heartbeat 거부, terminal Run heartbeat 거부, 구조화된 failure 보존, retry 연결이 모두 예상대로 동작했습니다.

검증 중 CLI 출력에서 Run의 `status`가 envelope의 `status`를 덮어쓰는 문제를 발견해 Run payload를 `run` 키 아래로 중첩하도록 고쳤습니다.

### 확인하지 못한 항목

- 승인 회수 또는 claim 해제 이후 실행 중인 executor를 실제로 멈추는 동작. 이 시점에는 executor가 없어 취소할 대상이 없었습니다. 2026-09-06 executor runtime slice에서 구현하고 검증했습니다.
- process identity(PID, start time) 기반 판정. executor process가 없어 수행할 수 없으며 판정 event에 `process_identity_checked: false`로 명시합니다
- 실제 worker가 장시간 heartbeat를 보내는 상황의 안정성
- orphan process 정리. executor process가 아직 없습니다
- Run 완료를 Task 상태 전이로 연결하는 흐름. Planner와 Validator가 없어 Task는 계속 `Draft`입니다

## 2026-09-06 — Run별 branch·worktree 격리

- 대상 구현: `src/atlas/gitcmd.py`, `src/atlas/workspace.py`, `src/atlas/workspace_service.py`, `src/atlas/store.py`(runs workspace 컬럼), `src/atlas/reconciliation.py`
- 검증 방법: 실제 임시 git repository. network를 쓰지 않았습니다.
- 관련 결정: [ADR-010](adr/0010-task-execution-isolation.md)의 Accepted 범위

### 확인된 항목

| 단계 | 확인 내용 | 결과 |
| --- | --- | --- |
| repo 준비 | `git init` + base commit | base revision 고정 |
| workspace 생성 | 두 Run에 각각 branch/worktree | `atlas/ATLAS-0042/...`, `atlas/ATLAS-0077/...` 서로 다름 |
| 파일 수정 | run1 worktree에서 README 수정과 파일 추가 | worktree 안에만 반영 |
| main 오염 | main worktree의 README, branch, HEAD, dirty 상태 | 모두 변화 없음. `dirty=False`, HEAD가 base와 동일 |
| Run 간 격리 | run1이 만든 파일이 run2 worktree에 보이는지 | 보이지 않음 |
| idempotency | 같은 Run에 create 재호출 | `created=False`, worktree 총 개수 3개 유지(main 포함) |
| restart | store를 닫고 다시 열어 create 호출 | 기존 workspace 재식별, 새로 만들지 않음 |
| dirty cleanup | 저장되지 않은 변경이 있는 worktree 정리 시도 | 거부(`worktree_dirty`), worktree 보존 |
| 정상 cleanup | 깨끗한 worktree 정리 | worktree 제거, branch 보존 |
| workspace reconciliation | worktree 디렉터리를 삭제한 뒤 판정 | `workspace_recovery_required` / `worktree_missing` 기록. 상태를 바꾸거나 branch를 지우지 않음 |

### 별도로 확인한 경계

단위 테스트로 확인한 거부 경로입니다.

- git repository가 아닌 경로, repository root가 아닌 하위 디렉터리
- `origin` remote가 Task repository와 다른 경우 (remote가 없으면 network 없이 통과)
- worker root 밖 경로, `..` traversal, symlink를 통한 escape
- Atlas namespace 밖 branch 삭제 시도
- branch 이름 충돌
- DB provenance가 없는 리소스 정리 시도
- 실행 중인 Run의 workspace 정리 시도

### schema migration (v3 → v4)

`runs`에서 workspace 컬럼 8개를 삭제하고 `schema_version`을 `3`으로 되돌린 뒤 store를 다시 열어 자동 migration을 확인했습니다. Run record 자체는 보존되고 workspace 상태는 `none`으로 시작합니다.

### 확인하지 못한 항목

- executor process 실행과 그로 인한 worktree 변경. 이번 범위가 아닙니다.
- worktree가 많아졌을 때의 disk 사용량과 retention 정책.
- 여러 Project를 동시에 다룰 때 worker root 분리.
- push, PR 생성 등 remote를 건드리는 동작. 전부 non-goal입니다.
- Windows 외 플랫폼에서의 symlink escape 동작. 이 검증은 Windows에서 수행했습니다.

### 2026-09-06 추가 — READY workspace 재검증과 remote identity

PR #9 리뷰에서 지적된 두 건을 수정하고 다시 검증했습니다.

- **READY 재검증**: `workspace_status`가 `ready`여도 실제 git 상태를 다시 확인합니다. restart 후 worktree 삭제, branch 변경, 다른 repository의 worktree로 경로 교체, git worktree가 아닌 빈 디렉터리 네 가지 상황에서 `create()`가 성공을 반환하지 않는 것을 확인했습니다. 불일치 시 상태를 바꾸거나 리소스를 지우지 않고 `workspace_recovery_required` event에 boolean 근거만 남깁니다. 작업이 진행돼 HEAD가 base에서 움직인 경우는 정상으로 통과합니다.
- **remote identity**: canonical `owner/repo` 정확 일치로 바꿨습니다. `https://github.com/evil/hongwon1031/atlas.git`처럼 suffix 비교였다면 통과했을 URL이 거부되는 것을 확인했습니다. HTTPS, SSH(scp 형식과 ssh:// 형식), credential 포함 URL, port 포함 URL을 모두 parsing합니다.

두 수정을 일시 제거하면 회귀 테스트 11건이 실패하고 복원하면 통과하는 것을 확인했습니다.

## 2026-09-06 — Executor process runtime (mock)

- 대상 구현: `src/atlas/{executor,local_process,mock_executor,process_identity,redaction,execution_service}.py`, `store.py`(executions), `reconciliation.py`
- 검증 방법: 실제 임시 git repository + 실제 OS subprocess. network와 provider 호출은 없습니다.
- 관련 결정: [ADR-010](adr/0010-task-execution-isolation.md)의 process isolation 범위

### 확인된 항목

| 항목 | 결과 |
| --- | --- |
| mock executor 성공 실행 | exit 0, worktree에 파일 생성, stdout 캡처 |
| non-zero exit | exit code 보존, `nonzero_exit` 분류, Run `Failed` |
| timeout | graceful → 강제 종료, `timeout` 분류, Run `Failed(timeout)` |
| 명시적 cancel | process 종료, cancellation state 기록, 재호출은 idempotent |
| 승인 회수 cancel | safety gate 실패를 감지해 실행 중 process 종료 |
| claim 상실 cancel | 동일 |
| process cwd | Run의 worktree에서 실행됨 |
| main worktree 오염 | 없음. README·HEAD·dirty 상태 모두 변화 없음 |
| 다른 Run worktree 오염 | 없음 |
| stale workspace | safety gate가 `workspace_valid` 실패로 거부 |
| duplicate start | `execution_already_active`로 거부, execution 1개 유지 |
| concurrent start (thread 6개) | 1개만 성공 |
| stdout/stderr 분리 | 각각 별도 파일 |
| 출력 크기 제한 | 상한에서 잘리고 `truncated` 표시 |
| invalid UTF-8 | 예외 없이 안전 디코딩 |
| secret redaction | token 형태, URL credential, Authorization 헤더, known 값 제거 |
| 환경 allowlist | allowlist 밖 변수가 child에 전달되지 않음 |
| child process 종료 | timeout과 cancel 양쪽에서 child가 남지 않음 |
| heartbeat | 실행 중 갱신되고 종료 후 중단, 실패 event 없음 |
| restart 후 재식별 | 살아 있는 process를 `execution_healthy`로 판정 |
| process 없음 | `process_missing`으로 recovery-required |
| PID identity mismatch | `pid_identity_mismatch`, `may_terminate=false`, 종료하지 않음 |
| attach 전 crash | `process_never_attached` |
| terminal Run + 생존 process | `execution_surviving_terminal_run`, 자동 종료하지 않음 |
| schema migration v4 → v5 | `executions` 테이블과 `events.execution_id` 자동 추가, Run·workspace 보존 |

### 검증 중 발견해 고친 것

1. **Windows에서 종료된 process를 살아 있다고 판정**했습니다. `OpenProcess`가 종료된 process handle에도 성공하기 때문입니다. `GetProcessTimes`의 exit time으로 판별하도록 고쳤습니다.
2. **child process가 살아남았습니다.** graceful 단계가 parent를 즉시 종료해 `taskkill /T`가 tree를 추적하지 못했습니다. graceful을 `CTRL_BREAK_EVENT`로 바꾸고 강제 단계를 parent 생존 중에 수행하도록 순서를 고쳤습니다.
3. **heartbeat가 한 번도 동작하지 않았습니다.** SQLite 연결을 스레드 간에 공유해 `ProgrammingError`로 죽었습니다. heartbeat 스레드가 자기 연결을 열도록 고치고 실패를 event로 남기게 했습니다.
4. **`Authorization: Bearer <token>`에서 토큰이 남았습니다.** 헤더 pattern이 `\S+`만 지워 "Bearer"만 사라졌습니다. 줄 끝까지 지우도록 고쳤습니다.

### 확인하지 못한 항목

- 실제 Claude Code나 Codex 호출. 이번 범위가 아닙니다.
- provider credential 주입과 회수. redaction boundary만 준비했습니다.
- POSIX에서의 process group 종료. 이 검증은 Windows에서 수행했습니다. POSIX 경로는 코드에 있으나 실측하지 않았습니다.
- 별도 OS process 사이의 동시 `executor-start` 경쟁. thread 6개 경쟁만 검증했습니다.
- 장시간 실행 executor의 안정성과 log 누적량.
- Job Object를 쓰지 않아 Windows에서 CTRL_BREAK를 무시하는 child가 있을 때의 동작.

### 2026-09-06 추가 — log redaction, 종료 확인, gate 경쟁

merge-blocking review 세 건을 고치고 다시 검증했습니다.

#### log artifact redaction

이전에는 event만 redaction했고 **파일에는 raw 출력을 그대로 썼습니다.** secret이 디스크에 평문으로 남는 문제라서, 파일에 쓰기 전에 redaction하도록 바꿨습니다.

| 확인 | 결과 |
| --- | --- |
| 주입한 known secret | stdout·stderr 파일 어디에도 남지 않음 |
| `Authorization: Bearer <token>` | 헤더가 줄 끝까지 제거됨 |
| GitHub token 형태, URL credential | 제거됨 |
| chunk 경계에 걸친 secret | 줄 단위로 모아 처리하므로 잘린 채 기록되지 않음 |
| 개행 없이 65536자 초과 | 보류 한도에서 redaction 후 기록, secret 남지 않음 |
| 유효하지 않은 UTF-8 | 예외 없이 대체 문자로 처리 |
| 크기 상한 | redaction을 마친 byte 기준으로 잘리고 `truncated` 표시 |
| pipe drain | 상한 도달 후에도 계속 비워 child가 블록되지 않음 |
| event·DB 전체 | secret 평문 없음 |

log는 텍스트로 취급하므로 **artifact는 원본과 byte 단위로 같지 않습니다.** binary를 그대로 남기려면 redaction을 적용할 수 없어, secret을 막는 쪽을 택했습니다.

#### 종료 확인과 termination outcome

이전에는 종료를 **요청**하기만 하면 `Finished`로 확정했습니다. identity를 확인할 수 없으면 실제로 종료하지 않으므로, 살아 있는 process가 terminal 처리되어 reconciliation에서 빠질 수 있었습니다.

| 확인 | 결과 |
| --- | --- |
| 정상 종료 | `not_required` → `Finished` |
| 종료 확인됨 | `confirmed` → `Finished` |
| 종료 미확인 | `unverified` → `Cancelling` 유지, terminal 아님 |
| timeout 후 process 잔존 | `Finished`가 아니라 `Cancelling`으로 남음 |
| cancel 미확인 | `cancelled=False` 반환, 근거 event 기록 |
| reconciliation 범위 | `Cancelling`은 active라 다시 검사됨 |
| 근거 | `execution_termination_unverified` event에 판정 evidence 기록 |

#### safety gate → reserve → spawn 경쟁

gate 통과와 spawn 사이에 승인 회수나 claim 해제가 들어오면 근거 없는 process가 뜰 수 있었습니다. 세 겹으로 막았습니다.

| 층 | 시점 | 확인 |
| --- | --- | --- |
| 첫 gate | 예약 전 | 8개 항목 |
| 예약 guard | 예약과 같은 transaction | run active, workspace ready, 승인, claim owner, lease |
| final gate | spawn 직전 | 8개 항목 |

| 확인 | 결과 |
| --- | --- |
| gate 직후 승인 회수 | spawn되지 않음 |
| gate 직후 claim 해제 | spawn되지 않음 |
| gate 직후 lease 만료 | spawn되지 않음 |
| 차단 후 상태 | active execution 0개. ghost reservation 없음 |
| 차단 근거 | `execution_safety_gate_failed` event에 단계와 실패 항목 기록 |
| transaction 경계 | subprocess spawn은 transaction 밖에서만 수행 |

예약 transaction은 rollback되므로 그 안에서 event를 남길 수 없습니다. 그래서 guard 실패 근거는 transaction 밖에서 기록합니다.

#### 회귀 테스트가 실제로 잡는지 확인

세 수정을 각각 되돌리고 다시 돌렸습니다. **11건이 실패**했고 복원하니 전부 통과했습니다. 테스트가 통과하기만 하는 것이 아니라 해당 결함을 실제로 잡습니다.

#### 재실행한 검증

- 전체 테스트 466건 통과
- `compileall` (src, tests) 통과
- Windows smoke 15단계 전부 통과 (timeout, cancel, child process tree, restart 재식별, `process_missing`, PID identity mismatch 포함)
- 이번 수정분 end-to-end smoke 통과
- secret scan, `git diff --check` 통과

#### 확인하지 못한 항목

앞 절의 항목이 그대로 남습니다. POSIX process group 종료 실측과 Windows Job Object 미사용 한계는 이번 범위에서 해소하지 않았습니다.

### 2026-09-06 추가 — 강제 flush 경계의 secret 분할

streaming redaction에 경계 문제가 하나 더 남아 있었습니다. 보류 한도에 도달해 **버퍼를 통째로 내보낼 때** secret이 그 경계에 걸치면, 앞 조각은 이미 기록된 뒤라 어느 쪽에도 전체 pattern이 없어 redaction이 걸리지 않았습니다.

overlap을 보존하고, 자를 지점이 완결된 secret 한가운데면 구간 시작점까지 물러서도록 고쳤습니다.

| 확인 | 결과 |
| --- | --- |
| known secret이 경계를 정확히 가로지름 | 전체·앞 조각 모두 남지 않음 |
| provider token이 경계를 가로지름 | 남지 않음 |
| Bearer token이 경계를 가로지름 | 남지 않음 |
| forced flush 4회 반복 | 매 회차 secret 없음 |
| 일반 출력 | 손실·중복 없이 입력과 정확히 일치 |
| 개행 기반 경로 | 기존 동작 유지 |
| `max_output_bytes`/truncation | 상한에서 정확히 잘리고 `truncated` 유지 |
| 보류 버퍼 크기 | 한도(`MAX_RETAINED_CHARS`) 안에 머무름 |
| overlap window | known secret 최대 길이 이상, 짧은 값은 window를 늘리지 않음 |

#### 검증 중 발견해 고친 것

**구간이 버퍼 끝까지 이어질 때 뒷부분이 raw로 남았습니다.** 버퍼 전체가 하나의 secret 후보(예: 아주 긴 `Authorization` 헤더)면 메모리 한도에서 강제로 내보내는데, 그 구간은 치환되지만 **이어서 들어오는 나머지 token 문자는 pattern 없이 그대로 기록**됐습니다. 구간이 버퍼 끝까지 이어진 경우 줄바꿈이 나올 때까지 이어지는 입력을 버리도록 고쳤습니다.

#### 회귀 테스트 확인

경계 보존을 되돌리고 다시 돌렸습니다. **3건이 실패**했고 복원하니 전부 통과했습니다.

## 2026-09-06 — 실제 Claude Code executor adapter

Windows 11, Python 3.12.x, git 설치 환경에서 **실제 Claude Code CLI**로 확인했습니다.

### Claude Code CLI 사전 조사

구현 전에 `claude --help`와 임시 git repository 실행으로 실측했습니다. 추측한 항목은 없습니다.

| 항목 | 확인한 동작 |
| --- | --- |
| version | `2.1.252 (Claude Code)`. `claude --version`이 약 320ms에 끝남 |
| 실행 파일 | Windows npm 설치는 `claude.CMD` wrapper로 resolve됨 |
| 비대화형 | `-p`/`--print`가 응답 후 종료. TTY를 요구하지 않음 |
| prompt 전달 | argv positional과 **stdin** 모두 가능. stdin으로 넘겨도 정상 동작 |
| 구조화 출력 | `--output-format json`이 단일 JSON 객체를 stdout에 출력 |
| exit code | 성공 0. 모델 오류 등 실패는 1 |
| **오류 신호** | 모델 오류에서도 `subtype`은 `success`였고 **`is_error: true`가 실제 신호**였음. `api_error_status: 404`, `terminal_reason: api_error`도 함께 옴 |
| stdout/stderr | 정상 실행에서 stderr는 비어 있음. 오류는 stderr에 한 줄 + stdout에 JSON |
| working directory | `cwd`에서 동작하고 파일을 그 안에 만듦 |
| **cwd 경계** | cwd 밖 파일 읽기를 CLI가 스스로 거부하고 `permission_denials`에 기록함 |
| 세션 재사용 | `--resume`/`--continue`가 있으나 `--no-session-persistence`로 비활성 가능 |
| 권한 옵션 | `--permission-mode {acceptEdits,auto,bypassPermissions,manual,dontAsk,plan}` |
| 도구 제한 | `--tools "Read,Edit,Write,Glob,Grep"`로 제한하면 Bash가 사라져 shell 명령을 실행할 수단이 없음 |
| model 옵션 | 필수가 아님. 지정하지 않으면 기본 모델 사용 |
| 잔여물 | `--no-session-persistence` 사용 시 worktree에 남는 파일 없음 |

### 인증에 필요한 최소 환경

기존 Windows allowlist만으로 인증이 됐습니다. 별도 credential 주입이나 auth 파일 복사가 필요하지 않았습니다.

| 환경 | 결과 |
| --- | --- |
| 기존 allowlist(`PATH`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `SYSTEMROOT` 등)만 | 성공 |
| allowlist + `HOME` | 성공 |
| 전체 상속(기준선) | 성공 |

Atlas는 credential 값을 읽지도 저장하지도 않습니다. 현재 로그인된 CLI 세션을 그대로 씁니다. API key를 argv에 넣지 않습니다.

### 실제 Claude Code end-to-end smoke

임시 git repository에 Run과 worktree를 만들고 실제 CLI로 한 번 실행했습니다.

Task objective는 "`docs/smoke.md` 파일을 새로 만들고 정확히 한 줄 `atlas claude smoke`만 써라"였습니다.

| 확인 | 결과 |
| --- | --- |
| executable resolution | `claude.CMD` 확인 |
| version probe | `2.1.252 (Claude Code)` |
| process 종료 | exit 0 |
| CLI 오류 | `is_error=false` |
| 파일 생성 | `docs/smoke.md` 생성됨 |
| 내용 | 정확히 `atlas claude smoke` 한 줄 |
| 변경 감지 | `changed_files=('docs/smoke.md',)` |
| 구현 판정 | `changes_applied` |
| main repository README | 변경 없음 |
| main repository HEAD | 변경 없음 |
| main repository dirty | 아님 |
| main에 결과 파일 | 없음 |
| branch | 예상 atlas branch 유지 |
| commit | 하지 않음. HEAD 그대로 |
| Run 상태 | `Succeeded`가 아니라 `Running`(validation 대기) |
| stdout/stderr log | 저장됨 |
| credential 흔적 | log·event·DB 전체에 없음 |
| scope 위반 | 없음 |

### 가짜 실행 파일 통합 테스트

network와 account에 의존하지 않는 경로입니다. `tests/fake_claude.py`가 실제 CLI 계약(stdin prompt, JSON 출력, `--version`)만 흉내 냅니다.

| 확인 | 결과 |
| --- | --- |
| prompt가 stdin으로 전달 | 확인. argv에는 없음 |
| argv에 사용자 텍스트 없음 | 확인 |
| cwd = Run worktree | 확인 |
| 파일 수정 | 확인 |
| nonzero exit | `claude_cli_failed` |
| timeout | `claude_timeout`, Run `Failed(timeout)` |
| stdout/stderr 수집 | 확인 |
| 변경 없음 | `claude_no_changes`. 성공으로 처리하지 않음 |
| allowed scope 밖 변경 | `out_of_scope_path_changed` 탐지. 되돌리지 않음 |
| forbidden path 변경 | `forbidden_path_changed` 탐지 |
| 예상치 못한 commit | `unexpected_commit` 탐지 |
| duplicate start | `execution_already_active`로 거부 |
| main worktree 오염 | 없음 |
| 다른 Run worktree 오염 | 없음 |
| secret 저장 | log·event·DB 어디에도 raw 값 없음 |

### 검증 중 발견해 고친 것

**Windows에서 prompt 인코딩이 깨졌습니다.** 테스트용 가짜 CLI가 `sys.stdin.read()`로 읽으면 Windows 기본 인코딩(cp949)으로 디코딩해 UTF-8 한국어 prompt가 손상됐습니다. 실제 CLI는 UTF-8을 읽으므로 가짜도 UTF-8로 고정했습니다. Atlas 쪽은 처음부터 `text.encode("utf-8")`로 쓰고 있어 제품 코드 변경은 없었습니다.

### 확인하지 못한 항목

- POSIX에서의 Claude Code 실행. 이 검증은 Windows에서 수행했습니다. `.CMD` wrapper resolution은 Windows 고유 동작입니다.
- 장시간 실행 Task에서의 안정성, 비용, usage 한도 도달 시 동작.
- `claude_auth_unavailable` 실제 발생 경로. 로그인 상태에서만 검증해 인증 실패는 가짜 출력으로만 확인했습니다.
- 여러 Run을 동시에 실제 CLI로 실행했을 때의 상호 간섭.
- Claude가 `--tools` 제한을 우회해 commit하는 경로. 도구 목록에 shell이 없어 수단이 없다고 판단했으나, 파일 도구로 `.git` 내부를 조작하는 경우는 탐지만 하고 차단하지는 않습니다.
- validation pipeline이 없어 "구현이 올바른가"는 확인하지 않았습니다. `changes_applied`는 "바뀌었다"는 뜻일 뿐입니다.

### 2026-09-06 추가 — 설정 하드닝, 파싱 경로 분리, AwaitingValidation

merge-blocking review 세 건을 고치고 다시 검증했습니다.

#### 설정으로 안전 경계를 우회할 수 있던 문제

`ATLAS_CLAUDE_PERMISSION_MODE`와 `ATLAS_CLAUDE_TOOLS` 값을 그대로 받고 있었습니다. `bypassPermissions`나 `Bash`를 넣으면 이 adapter의 핵심 경계가 무력화됩니다.

| 확인 | 결과 |
| --- | --- |
| `bypassPermissions`, `auto`, `dontAsk` | 거부 |
| 이름에 `dangerous`가 들어가는 mode | 거부 |
| 알 수 없는 mode(`plan`, `manual`, 빈 값) | 거부 |
| safe mode(`acceptEdits`) | 허용 |
| `Bash` 등 명령 실행 도구 12종 | 각각 거부 |
| allowlist 밖 도구 이름 | 거부(fail closed) |
| safe subset(`Read,Grep`) | 허용 |
| 기본 설정 | 기존 값 그대로 유지 |
| 검증 우회로 만든 설정 | preflight에서 거부 |
| event의 유효 정책 | 정규화 값만. 실행 파일 경로 없음 |

#### redaction이 JSON을 깨뜨리던 문제

persisted log는 redaction을 거치는데, adapter가 **그 redacted log를 다시 읽어 파싱**하고 있었습니다. `Authorization` 헤더 pattern은 줄 끝까지 지우므로 한 줄 JSON이 잘립니다. 실제로 확인했습니다.

```
{"is_error": false, "result": "Changed Authorization: Bearer abc123def456 safely", "subtype": "success"}
→ {"is_error": false, "result": "Changed Authorization: <redacted>
→ json.JSONDecodeError: Unterminated string starting at: line 1 column 31
```

파싱 대상과 저장 대상을 분리했습니다. 원문은 상한 있는 임시 메모리 버퍼로만 흐르고, 저장본은 그대로 redaction을 거칩니다.

| 확인 | 결과 |
| --- | --- |
| redaction 후 JSON이 깨지는 사실 | 재현됨(회귀 테스트로 고정) |
| result에 `Authorization: Bearer …` | 파싱 성공. 저장본에 token 없음 |
| result에 GitHub token | 파싱 성공. 저장본·요약에 없음 |
| result에 URL credential | 파싱 성공. 저장본에 없음 |
| 주입한 known secret | log·event·DB 전체에 없음 |
| malformed JSON | `claude_output_unparseable` |
| 상한 초과 JSON | `claude_output_too_large`(fail closed) |
| 임시 버퍼 | 한 번 읽으면 비워짐. `to_dict`에 내용 없음 |
| 파싱 상한 | `max_output_bytes`와 별개로 설정 |

core는 provider-neutral하게 유지했습니다. `StructuredCapture`는 "구조화된 출력을 잠깐 원문 그대로 보고 싶다"는 요구만 표현하고 Claude를 모릅니다.

#### 정상 결과가 Orphaned가 되던 문제

`changes_applied` 뒤 Run을 `Running`으로 남겼는데 executor heartbeat는 끝납니다. staleness reconciliation이 정상 구현 결과를 `Orphaned`로 만들 수 있었습니다. 문서로 덮을 문제가 아니라 상태 의미론 문제여서 `RunStatus.AWAITING_VALIDATION`을 추가했습니다.

`is_active`(Run 슬롯 점유)와 `expects_heartbeat`(살아 있어야 하는가)를 분리했습니다.

| 확인 | 결과 |
| --- | --- |
| `changes_applied` | `AwaitingValidation`으로 전이 |
| terminal | 아님 |
| active Run 슬롯 | 차지함. 같은 Task로 새 Run 시작 거부 |
| heartbeat 기대 | 아님 |
| stale threshold 초과 후 reconcile | `Orphaned`가 되지 않음 |
| `orphan_if_stale` 직접 호출 | 회수 거부 |
| active execution | 없음 |
| workspace | 유지. branch도 그대로 |
| 재전이 | idempotent |
| 이후 terminal 전이 | 가능(다음 slice가 이어받음) |
| 실패 경로 | 새 상태를 쓰지 않고 `Failed` |

schema v5 → v6에서 active Run partial unique index를 다시 만듭니다. 기존 index는 `AwaitingValidation`을 몰라 슬롯을 지키지 못합니다.

#### 회귀 테스트가 실제로 잡는지 확인

세 수정을 각각 되돌리고 다시 돌렸습니다. **34건이 실패**했고 복원하니 전부 통과했습니다.

#### 재실행한 검증

- 전체 테스트 통과
- `compileall` (src, tests) 통과
- **real Claude Code smoke 재실행 통과** — `docs/smoke.md` 정확 생성, main 무오염, commit 없음, branch 유지, Run이 `AwaitingValidation`, active execution 없음, workspace 유지, credential 흔적 없음
- secret scan, `git diff --check` 통과

#### 확인하지 못한 항목

POSIX 실측은 여전히 하지 않았습니다. 앞 절의 미확인 항목이 그대로 남습니다.

## 2026-09-06 — Validation pipeline

Windows 11, Python 3.12.x, git 설치 환경에서 확인했습니다.

### repository 검증 능력 사전 조사

Atlas repository 자체를 대상으로 실측했습니다. 추측한 항목은 없습니다.

| 항목 | 확인한 사실 |
| --- | --- |
| `pyproject.toml` | 존재. `[tool.ruff]`(line-length, src)만 있고 lint dependency 선언 없음 |
| pytest config | 없음 |
| `pytest` 실행 파일 | PATH에 있으나 worker interpreter에서 import 불가 |
| `tests/` | 존재. `tests/__init__.py`가 `src`를 sys.path에 넣어 설치 없이 실행 가능 |
| `ruff` | 설치 안 됨 |
| `mypy` | 설치돼 있으나 repository에 config 없음 |
| `pyright` | 설치 안 됨 |
| CI workflow | 없음. `.github`에는 Issue template과 PR template만 존재 |

이 사실에서 계획이 결정됩니다.

- pytest contract가 없고 `tests/`가 있으므로 **stdlib `unittest` discover**를 씁니다. dependency를 요구하지 않는 쪽을 먼저 고릅니다.
- `src`와 `tests`가 있으므로 **`compileall`이 required**입니다.
- ruff는 `[tool.ruff]` table만 있는 **weak contract**이고 실행 파일이 없어 `skipped`입니다.
- mypy는 config가 없어 `skipped`입니다.

#### contract 강도를 나눈 이유

`pyproject.toml`의 `[tool.X]` table만으로 "이 repository는 그 도구로 게이트한다"고 볼 수 없습니다. 편집기 설정만 담는 경우가 흔합니다. 전용 config 파일이나 dependency 선언은 강한 근거로, `[tool.X]` table만 있는 경우는 약한 근거로 나눴습니다.

이 구분이 없으면 Atlas 자신이 ruff 미설치만으로 실패하고, 구분을 아예 두지 않으면 진짜로 ruff를 요구하는 repository를 통과시킵니다. **이 정책은 명시적 선택이며 검토 대상입니다.**

### 실제 Atlas repository validation smoke

Atlas repository를 임시 위치로 clone하고 Run과 worktree를 만든 뒤, 구현이 끝난 상태를 만들어 실제 validation을 수행했습니다.

| 확인 | 결과 |
| --- | --- |
| 시작 상태 | `AwaitingValidation` |
| 계획 ecosystem | `python`, test capability 발견 |
| workspace integrity | passed |
| git policy | passed |
| **tests** | `python -m unittest discover -s tests -t .` **exit 0, 246초** |
| **compile** | `python -m compileall -q src tests` exit 0 |
| ruff | skipped (`command_missing_weak_contract`) |
| mypy | skipped (`no_contract`) |
| required step | 전부 통과 |
| Run 전이 | `AwaitingValidation` → `Validating` → **`Succeeded`** |
| main repository HEAD | 변경 없음 |
| main repository dirty | 변화 없음 |
| main에 결과 파일 | 없음 |
| branch | 예상 atlas branch 유지 |
| commit | 없음. HEAD가 base revision 그대로 |
| validation 기록 | 1건, outcome=passed |
| step 기록 | 6건 전부 저장 |
| log artifact | 2건 경로 저장, 파일 존재 |
| credential 흔적 | log·event·DB 전체에 없음 |

Atlas가 자기 자신의 전체 테스트를 격리된 worktree에서 실제로 돌려 통과시켰습니다.

### 임시 repository 통합 테스트

| 확인 | 결과 |
| --- | --- |
| `AwaitingValidation` → `Validating` → `Succeeded` | 확인 |
| 실패하는 테스트 | `validation_test_failed`, Run `Failed` |
| 이후 step 처리 | `earlier_required_step_failed`로 건너뜀 |
| timeout | `validation_timeout`, Run `Failed(timeout)` |
| 필수 명령 없음 | `command_missing`, Run `Failed` |
| 테스트 없는 repository | `Succeeded` + `no_tests_discovered` 경고 |
| pytest 강제 실행 | 없음 |
| allowed scope 밖 변경 | `out_of_scope_path_changed`, `policy_violation` |
| forbidden path 변경 | `forbidden_path_changed` |
| 예상치 못한 commit | `unexpected_commit` |
| 변경이 아예 없음 | `no_changes_to_validate` |
| 구현 이후 사람이 수정 | `workspace_changed_after_implementation` |
| branch 전환 | gate가 `workspace_valid`로 거부 |
| gate 통과 후 branch 이동 | workspace integrity step이 `failed` |
| workspace 삭제 | gate가 거부 |
| 승인 회수 | gate가 `task_approved`로 거부 |
| claim 해제 | gate가 `claim_active`로 거부 |
| 다른 worker | `claim_owner_matches`로 거부 |
| 거부 시 Run 상태 | `AwaitingValidation` 유지, active validation 없음 |
| 중복 validation start | database가 거부 |
| active execution 존재 | `execution_still_active`로 거부 |
| main worktree 오염 | 없음 |
| 다른 worktree 오염 | 없음 |
| validation log redaction | token·Authorization 헤더 모두 제거 |
| event·DB secret | 없음 |
| event 크기 | 전체 출력 미저장 |

### restart / reconciliation

| 상황 | 판정 | 자동 종료 |
| --- | --- | --- |
| `Running` + process 살아 있음 | `validation_healthy` | — |
| `Running` + process 사라짐 | `validation_process_missing` | 해당 없음 |
| `Running` + PID identity 불일치 | `validation_pid_identity_mismatch` | **하지 않음** (process 생존 확인) |
| step은 있으나 attach 실패 | `validation_process_never_attached` | 해당 없음 |
| `Starting`인데 step 없음 | `validation_never_started` | 해당 없음 |
| `Running`인데 실행 중 step 없음 | `validation_state_ambiguous` (high) | 해당 없음 |
| terminal Run + validation 생존 | `validation_surviving_terminal_run` (high) | **하지 않음** (process 생존 확인) |
| 자동 재검증 | **하지 않음.** 판정 후에도 status/outcome이 그대로 |
| `Validating` heartbeat 중단 | stale 판정으로 `Orphaned` |

마지막 항목이 `AwaitingValidation`과의 차이입니다. `AwaitingValidation`은 process가 없으므로 heartbeat 중단이 정상이고, `Validating`은 process가 돌고 있어야 하므로 중단이 이상입니다.

### 검증 중 발견해 고친 것

1. **git policy가 변경을 전혀 감지하지 못했습니다.** 기준선을 현재 상태로 잡아 자기 자신과 비교했기 때문입니다. workspace를 만든 시점(base revision, 깨끗한 트리)을 기준선으로 바꿨습니다. 이제 HEAD 비교가 commit 탐지도 겸합니다.
2. **설정 파일만 있는 repository를 Python으로 인식하지 못했습니다.** `pyproject.toml`이나 소스 디렉터리가 있어야만 Python으로 봐서, `ruff.toml`이나 `pyrightconfig.json`만 있는 repository는 어떤 step도 계획되지 않았습니다. Python marker 파일과 최상위 `*.py` 존재도 근거로 넣었습니다.
3. **heartbeat가 `Validating`을 `Running`으로 되돌렸습니다.** 기존 `heartbeat()`가 상태를 무조건 `Running`으로 썼습니다. `Pending`일 때만 올리도록 고치고, heartbeat 대상이 아닌 상태는 거부하도록 했습니다.

### 확인하지 못한 항목

- POSIX에서의 validation 실행. 이 검증은 Windows에서 수행했습니다.
- Node repository의 **실제** 검증 실행. detection은 단위 테스트로 확인했으나 실제 `npm run test` 실행은 검증하지 않았습니다.
- pytest를 실제로 실행하는 경로. Atlas는 pytest contract가 없어 unittest 경로만 실측했습니다.
- ruff·mypy·pyright를 실제로 실행하는 경로. 이 환경에 ruff와 pyright가 없고 Atlas에 mypy config가 없습니다.
- step 단위 resume. ambiguous 상태 식별까지만 구현했습니다.
- 매우 큰 repository에서의 소요 시간과 log 누적량.
- 여러 validation을 동시에 수행했을 때의 자원 경쟁.

### 2026-09-06 추가 — 코드 실행 신뢰 정책, 내용 지문, 종료 확인

merge-blocking review 네 건을 고치고 다시 검증했습니다.

#### validation이 임의 코드 실행 경로였던 문제

`shell=False`는 Atlas가 shell wrapper를 거치지 않는다는 뜻일 뿐, **실행된 repository 코드를 격리하지 않습니다.** 환경변수를 줄이는 것도 sandbox가 아닙니다. executor에게 shell 도구를 주지 않았어도, executor가 test나 `package.json`을 고친 뒤 validation이 그것을 실행하면 그 제한이 무의미해집니다.

이번 범위에서 실제 sandbox를 만들지 않았습니다. 대신 **명시적 신뢰 정책 뒤에** 두고, 문서가 실제 보장보다 강하게 주장하던 부분("network 사용 금지")을 정정했습니다.

실제 Atlas repository로 확인했습니다.

| 확인 | 결과 |
| --- | --- |
| 기본 정책 | `untrusted` (fail closed) |
| 코드 실행 step 식별 | `unittest`, `mypy` 2개 |
| 기본 정책에서 차단 | 전부 `active_validation_requires_trust` |
| argv 제거 | 확인. 계획에 명령이 남지 않음 |
| required 여부 | 전부 optional로 내려감 |
| 정적 step | `compileall` required 유지, `workspace-integrity`·`git-policy` 유지 |
| 정적 step의 코드 실행 | 없음 |
| sandbox 주장 | `sandboxed: false`, `network_denied: false` 명시 |
| 신뢰 부여 후 | 테스트가 required로 실행 대상이 됨 |
| dependency 설치 명령 | 양쪽 정책 모두 없음 |

임시 repository 테스트에서 확인한 것입니다.

| 확인 | 결과 |
| --- | --- |
| 악성 `package.json` test script (`powershell ... && curl ...`) | 기본 정책에서 실행되지 않음. argv에 `powershell`·`curl` 없음 |
| Python 테스트 분류 | `executes_repository_code=True` |
| 정적 검사만 통과한 결과 | `active_validation_skipped_untrusted`, `validation_passed_static_only` 경고 |
| 신뢰 목록에 있는 repository | 실행 허용 |
| 목록에 없는 repository | 기본 거부 |

**Node package script는 신뢰 없이 실행하지 않습니다.** script 본문이 임의 shell 문자열이라 이름 allowlist로는 통제되지 않습니다.

#### 같은 파일 내용만 바뀌면 drift를 놓치던 문제

이전 구현은 `implementation_completed` event의 변경 파일 **이름 집합**만 비교했습니다. Claude가 `src/a.py`를 고치고 사람이 같은 파일을 다시 고치면 양쪽 다 `{"src/a.py"}`라 drift가 없었습니다. PR 본문이 주장하던 "구현 이후 수정 시 실패" 보장과 달랐습니다.

내용 지문(SHA-256)으로 바꿨습니다. HEAD, branch, `git diff HEAD --binary`, untracked 파일의 경로와 내용을 모두 넣습니다.

| 확인 | 결과 |
| --- | --- |
| 같은 파일 내용만 변경 | 탐지 |
| untracked 파일 내용만 변경 | 탐지 |
| 파일 추가 | 탐지 |
| 파일 삭제 | 탐지 |
| 변경 없음 | 동일 지문 |
| 되돌리면 | 원래 지문과 일치 |
| 저장 내용 | digest와 개수만. raw source 없음 |
| 지문 없는 예전 Run | 이름 비교로 물러서되 `weaker_guarantee: true` 기록 |

#### 종료 미확인 process를 terminal로 숨기던 문제

executor runtime의 invariant가 validation에 적용되지 않고 있었습니다. `_classify()`가 termination 확인 없이 timeout을 `error`로 바꿨고, validation record가 `Finished`/`Failed`로 닫히면 `active_validations()`(당시 `Starting`/`Running`만 조회)에서 사라졌습니다.

| 확인 | 결과 |
| --- | --- |
| 종료 미확인 timeout | step `unconfirmed`. 평범한 error로 닫지 않음 |
| validation status | `RecoveryRequired`. terminal 아님 |
| outcome | `ambiguous`, `validation_termination_unverified` |
| Run 상태 | 확정하지 않음. `Validating` 유지 |
| reconciliation | 계속 보임 |
| 확인된 정상 timeout | 기존대로 `Finished` + Run `Failed` |
| identity 불일치 | 종료하지 않음. process 생존 확인 |
| spawn 후 attach 실패 | `validation_process_attach_failed` + `validation_interrupted` 기록, `RecoveryRequired` 유지 |
| terminal validation record + 생존 process | `validation_orphan_process`로 탐지. 종료하지 않음 |

**validation status만으로 reconciliation 범위를 정하지 않습니다.** process identity가 기록된 step 자체를 기준으로도 훑습니다.

#### unittest 0건 실행 문제

`unittest discover`는 기본 pattern(`test*.py`)에 맞는 파일이 없어도 "Ran 0 tests"로 exit 0을 냅니다. pytest 형식(`*_test.py`)만 있는 repository가 조용히 통과할 수 있었습니다.

| 확인 | 결과 |
| --- | --- |
| 계획 시점 pattern 불일치 | `test_runner_mismatch`로 required error |
| 실행 후 0건 | `no_tests_executed`로 되돌림 |
| Run 결과 | `Failed(validation_no_tests_executed)` |
| 파일이 아예 없는 경우 | 기존대로 `no_tests_discovered` skip |

#### 회귀 테스트가 실제로 잡는지 확인

네 수정을 각각 되돌리고 다시 돌렸습니다. **11건이 실패**했고 복원하니 전부 통과했습니다.

#### 재실행한 검증

- 전체 테스트 통과
- `compileall` (src, tests) 통과
- **real Atlas validation smoke 재실행 통과** — 신뢰를 부여한 상태에서 Atlas 자체 테스트 372초 exit 0, `Succeeded` 확정, main 무오염, commit 없음, credential 흔적 없음
- 실제 Atlas repository로 신뢰 정책·지문 smoke 통과
- secret scan, `git diff --check` 통과

#### 확인하지 못한 항목

- **실제 sandbox는 만들지 않았습니다.** filesystem 경계, process spawn 제한, network deny를 강제하지 않습니다. 신뢰 정책이 유일한 통제입니다.
- 신뢰를 부여한 repository에서 악성 코드가 실제로 host에 미치는 영향은 검증 대상이 아닙니다. 그 경우 통제 수단이 없습니다.
- 앞 절의 미확인 항목(POSIX, Node 실제 실행, pytest·ruff·mypy·pyright 실행 경로)이 그대로 남습니다.

## 2026-09-06 — Git publication과 draft PR 생성

Windows 11, Python 3.12.x, git 설치 환경에서 확인했습니다.

**실제 GitHub에는 side effect를 만들지 않았습니다.** push 대상은 로컬 bare remote이고 PR client는 fake입니다. 실제 GitHub PR 생성은 이번 검증 범위가 아닙니다.

### 실제 Atlas repository publication smoke

Atlas repository를 임시 위치로 clone하고 bare remote를 붙인 뒤, 구현·검증이 끝난 Run을 실제로 게시했습니다.

| 확인 | 결과 |
| --- | --- |
| Run 상태 | `Succeeded` |
| **기본 정책이 로컬 remote 거부** | `publication_remote_invalid` |
| 거부 시 side effect | 없음. remote branch 생성되지 않음 |
| 게시 결과 | `Published` |
| commit 생성 | 확인 |
| push 수행 | 확인 |
| draft 여부 | `draft=true` |
| branch | 예상 atlas branch 유지 |
| HEAD == commit | 확인 |
| commit 전진 | 정확히 1 |
| working tree | commit 후 clean |
| staged 경로 | `['docs/publication-smoke.md']` — 검증된 경로와 정확히 일치 |
| commit author | `Atlas <atlas@users.noreply.github.com>` |
| commit message | `atlas: implement ATLAS-9101`로 시작 |
| commit에 사용자 텍스트 | 없음 |
| remote branch | commit과 일치 |
| **remote에 main** | 없음 |
| main repository HEAD | 변경 없음 |
| main repository dirty | 아님 |
| main branch | 그대로 |
| main에 결과 파일 | 없음 |
| 재시도 | `PublicationGateFailed`로 거부 |
| PR 생성 횟수 | 1 |
| reconciliation 후 PR 생성 | 추가 없음 |
| durable linkage | commit SHA, PR 번호, validation id, status 모두 저장 |
| Run 상태 | `Succeeded` 유지 |
| credential 흔적 | event·DB·PR body 전체에 없음 |
| PR body 로컬 경로 | 없음 |
| Issue 연결 | `Refs #9101`. `Closes #` 없음 |
| 사람 검토 문구 | 포함 |

### bare remote 통합 테스트

| 확인 | 결과 |
| --- | --- |
| `Succeeded` → commit → push → draft PR | 확인 |
| 정확한 branch만 push | 확인. `main`은 remote에 없음 |
| 전역 git config | 변경 없음 |
| 같은 commit 재push | 건너뜀(`remote_already_matches`) |
| **remote가 다른 commit** | `publication_remote_conflict`. **덮어쓰지 않음.** remote 그대로 |
| 이미 열린 PR | 채택. 새로 만들지 않음 |
| 열린 PR 여러 개 | `publication_pr_conflict` + `RecoveryRequired` |
| draft 아닌 기존 PR | 채택하되 경고. 상태를 바꾸지 않음 |
| 닫힌 PR | 채택하지 않고 새로 생성 |
| 중복 publication 시작 | database가 거부 |
| PR 생성 실패 | commit·push 기록은 남고 `Failed` |
| 인증 실패 | `publication_authentication_failed`. Run은 `Succeeded` 유지 |

### 무결성 재확인

| 확인 | 결과 |
| --- | --- |
| 검증 이후 파일 내용 변경 | `publication_workspace_drift`. PR 생성 안 함 |
| 검증 이후 사람이 만든 commit | `publication_workspace_drift`. push 안 함 |
| allowed scope 밖 변경 | `publication_workspace_drift` |
| 게시할 변경 없음 | `publication_nothing_to_publish` |
| branch 전환 | 거부. remote 변화 없음 |
| 보호 branch(main, master, HEAD, trunk, develop) | 전부 거부. remote에 push 없음 |

### crash window 복구

| 창 | 결과 |
| --- | --- |
| commit 후 저장 전 | branch HEAD로 채택 |
| push 후 저장 전 | `ls-remote`로 채택 |
| PR 생성 후 저장 전 | head/base 검색으로 채택. **새로 만들지 않음** |
| commit 전 중단 | `publication_not_committed`. 재시작 가능 |
| remote branch 없음 | `publication_remote_branch_missing`. 재시도 가능 |
| remote 충돌 | `publication_remote_conflict` + `RecoveryRequired`. remote 그대로 |
| 기록과 local HEAD 불일치 | `publication_local_drift` + `RecoveryRequired` |
| reconciliation 자체 | **side effect를 만들지 않음.** PR 생성 0, push 0 |

### 보안

| 확인 | 결과 |
| --- | --- |
| `push` 실행 문장에 force 옵션 | 없음 |
| `+refs/heads` refspec | 없음 |
| publication 코드에 `shell=True` | 없음 |
| `git add -A` / `git add .` | 없음 |
| 악의적 objective(`--force +refs/heads/main:...; rm -rf /`) | refspec·argv 불변. commit message에 반영 안 됨. `main` push 없음 |
| 기본 정책의 임의 remote | 거부 |
| token이 DB·event에 | 없음 |
| token이 PR body에 | 없음 |

### 검증 중 발견해 고친 것

1. **재시도 시 이미 만든 commit을 무결성 검사가 거부했습니다.** HEAD가 base보다 앞서 있으면 "검증 이후 새 commit"으로 판정했습니다. Atlas가 만든 commit인지 네 조건(정확히 1 전진, clean tree, 결정적 subject 일치, 기대 branch)으로 판별해 채택하도록 고쳤습니다. 채택하더라도 범위 검사는 그대로 수행합니다.
2. **commit message에 objective가 들어갔습니다.** refspec이나 argv에 영향은 없었지만 사용자 텍스트를 git history에 영구히 남길 이유가 없습니다. 식별자만 남기고 사람이 읽을 요약은 PR에 두도록 바꿨습니다.
3. **publication이 공용 예약 guard의 `run_active`에 걸렸습니다.** publication은 terminal Run(`Succeeded`)에서 돌기 때문입니다. 승인·claim·lease·workspace는 그대로 확인하고 `run_active`만 제외하는 전용 guard를 만들었습니다.

### 회귀 테스트가 실제로 잡는지 확인

remote 충돌 시 덮어쓰기, 무결성 재확인 제거, staged 경로 검증 제거, 기존 PR 무시를 각각 되돌렸습니다. **5건이 실패**했고 복원하니 전부 통과했습니다.

### 확인하지 못한 항목

- **실제 GitHub PR 생성.** network와 credential이 필요하고, 실제 repository에 함부로 side effect를 만들지 않기 위해 fake client로 대체했습니다. GitHub REST 호출 경로(`github_pr.py`)는 단위 수준에서만 확인했습니다.
- **실제 GitHub remote로의 push.** 로컬 bare remote로만 확인했습니다.
- GitHub API의 rate limit, 2차 rate limit, 대규모 PR 본문 처리.
- PR이 merge되거나 닫힌 뒤의 재게시 정책.
- POSIX에서의 동작. 이 검증은 Windows에서 수행했습니다.
- 여러 Run이 동시에 같은 repository로 게시할 때의 경쟁.
- branch cleanup. 게시 후 remote branch를 정리하지 않습니다.

### 2026-09-06 추가 — 내용 기반 commit 채택, remote TOCTOU, 게시 중 권한 상실

merge-blocking review 세 건과 추가 점검 하나를 고치고 다시 검증했습니다.

#### crash recovery commit 채택이 metadata에만 의존하던 문제

기존 조건(base+1, clean, subject 일치, 기대 branch)은 **사람이 만든 commit도 만족할 수 있습니다.** subject를 `atlas: implement <task-id>`로 맞추고 허용 경로 안에서 다른 내용을 commit하면 채택돼, 검증하지 않은 내용이 게시됩니다.

commit 전후로 같은 값이 나오는 **내용 지문**을 도입했습니다. base revision 기준으로 각 경로의 blob 해시를 모아 SHA-256으로 요약합니다. `git hash-object`가 내는 값과 commit 안의 blob sha가 같다는 사실을 실측으로 확인한 뒤 설계했습니다.

| 확인 | 결과 |
| --- | --- |
| commit 전 지문 == commit 후 지문 | 일치 |
| rename·삭제·untracked 혼합 | 일치. entry 수도 동일 |
| 내용이 다른 commit | 지문 불일치 |
| subject를 맞춘 사람 commit | `publication_content_mismatch`. 채택하지 않음 |
| 허용 경로 안의 사람 commit | reconciliation이 `content_mismatch`로 판정, `RecoveryRequired` |
| 정확한 Atlas commit | 채택 |
| 지문이 아예 없는 경우 | 채택하지 않음(fail closed) |
| 같은 Run의 이전 attempt 지문 | 유효한 근거로 인정 |
| 정상 commit 직후 | 만든 commit의 내용을 다시 확인 |
| raw source | 지문·event·DB 어디에도 없음. digest 64자만 |
| service와 reconciler | 같은 verifier 사용 |

파일 mode는 지문에 넣지 않았습니다. Windows에서 실행 비트를 신뢰할 수 없기 때문이고, 알려진 한계로 문서에 적었습니다.

#### remote identity TOCTOU

예약 시점에 URL을 검증한 뒤 remote **이름**으로만 push하면, 그 사이 `git remote set-url`로 다른 repository를 가리키게 만들 수 있었습니다.

push 직전에 URL을 다시 읽어 저장된 값과 정확히 비교하고 identity를 재검증한 뒤, **확인한 URL을 그대로 push 대상으로** 씁니다. 이름을 한 번 더 거치지 않으므로 확인과 사용 사이의 간격이 사라집니다.

| 확인 | 결과 |
| --- | --- |
| 예약 후 다른 bare로 변경 | `publication_remote_changed`. **push 0** |
| lookalike GitHub URL로 변경 | 차단. push 0 |
| 다른 owner/repo로 변경 | 차단. push 0 |
| 변경 없음 | 정상 게시 |
| identity 재검증 호출 | push 직전에도 호출됨 |
| reconciliation | 저장된 URL과 다르면 `publication_remote_changed` + `RecoveryRequired` |
| 근거에 URL 자체 | 넣지 않음. credential이 박혀 있을 수 있음 |
| credential이 박힌 URL | argv에 넣지 않고 remote 이름 사용 |

#### 예약 이후 권한 상실

예약 guard 통과 뒤에도 승인 회수·claim 해제·owner 변경·lease 만료가 commit과 push와 PR 생성 사이에 일어날 수 있었습니다.

외부 side effect 직전마다 재확인하는 `authorization_checks`를 분리했습니다. 시작 gate와 달리 문맥 의존 항목(`not_already_published`, `run_succeeded` 등)을 넣지 않습니다.

| 확인 | 결과 |
| --- | --- |
| 예약 후 승인 회수 | `publication_authorization_lost`. **push 0, PR 0** |
| 예약 후 claim 해제 | 차단. push 0 |
| commit 후 lease 만료 | 차단. push 0 |
| commit 후 owner 변경 | 차단. push 0 |
| **push 성공 후 승인 회수** | **PR 0.** push된 branch는 되돌리지 않음. `side_effects_exist=true` 기록 |
| 근거 기록 | `publication_authorization_lost` event |
| 정상 경로 | 영향 없음 |
| 검사 집합 | 문맥 의존 항목 미포함 확인 |

이미 만든 side effect를 force push나 삭제로 정리하려 들지 않습니다. checkpoint를 남기고 사람이 판단합니다.

#### Published 외부 증거 확인 (추가 점검)

PR 본문이 "Published DB record but remote evidence missing"을 지원한다고 적었으므로 구현을 맞췄습니다. DB만 보고 게시됐다고 믿지 않고 remote branch와 PR을 실제로 확인합니다.

| 확인 | 결과 |
| --- | --- |
| 정상 Published | finding 없음 |
| remote branch 삭제됨 | `publication_remote_evidence_missing` |
| remote branch가 다른 commit | `publication_remote_evidence_changed` |
| PR이 더 이상 열려 있지 않음 | `publication_pr_no_longer_open`. **자동으로 고치지 않음** |
| PR 번호 없음 | `publication_published_without_pr` |

닫히거나 merge된 PR의 처리 정책이 정해지지 않았으므로 finding만 남깁니다.

#### 검증 중 발견해 고친 것

**한 번도 추적된 적 없는 파일이 삭제되면 staging이 통째로 실패했습니다.** `git add --all -- <paths>`에 매칭되는 것이 없는 경로가 섞이면 exit 128입니다. 실제 경로에서는 잘 생기지 않지만 방어가 필요합니다. stage할 것이 없는 경로를 조용히 빼고 나머지를 정상 처리하도록 고쳤습니다.

#### 회귀 테스트가 실제로 잡는지 확인

내용 검증 없이 metadata만으로 채택, push 직전 remote 재검증 제거, 예약 이후 authorization 재확인 제거, reconciler의 지문 비교 제거를 각각 되돌렸습니다. **12건이 실패**했고 복원하니 전부 통과했습니다.

#### 재실행한 검증

- 전체 테스트 통과
- `compileall` (src, tests) 통과
- bare remote smoke 재실행 통과 — 기본 정책의 로컬 remote 거부 포함
- secret scan, `git diff --check` 통과

#### 확인하지 못한 항목

앞 절의 항목이 그대로 남습니다. 실제 GitHub push와 PR 생성은 여전히 미검증이고, 파일 mode는 내용 지문에 포함하지 않습니다.

### 2026-09-06 추가 — SSH remote race, 늦은 권한 확인, 요청 상태, 지문 fail-closed

final review 네 건을 고치고 다시 검증했습니다.

#### SSH remote에서 TOCTOU가 되살아나던 문제

`_has_userinfo`가 `git@github.com:owner/repo.git`의 `git@`을 credential로 오인해 remote 이름으로 되돌아갔습니다. 그러면 검증한 URL이 아니라 이름으로 push하게 되고, 그 사이 `set-url`로 대상을 바꿀 수 있습니다. **정상 SSH remote에서 race가 그대로 남아 있었습니다.**

SSH username은 credential이 아닙니다. 인증은 SSH agent가 하고 URL에 secret이 없습니다. HTTP(S) URL의 실제 credential만 구분해 거부합니다.

| remote 형태 | 결과 |
| --- | --- |
| `git@github.com:owner/repo.git` | 검증한 exact URL을 대상으로 사용 |
| `ssh://git@github.com/owner/repo.git` | 검증한 exact URL |
| `https://github.com/owner/repo.git` | 검증한 exact URL |
| 로컬 bare 경로 | 검증한 exact 경로 |
| `https://token@github.com/...` | **거부**. push 0 |
| `https://user:pass@github.com/...` | **거부** |
| 이름으로 되돌아가는 경로 | 없음 |
| 검증 뒤 이름의 대상 변경 | push 목적지 불변. 원래 bare에만 올라감 |
| 거부 근거에 token | 없음 |

#### authorization 확인이 side effect에서 멀었던 문제

기존에는 단계 진입 시점에만 확인했습니다. 그런데 `ls-remote`와 PR 조회는 network 호출이라 그 사이에 승인이 회수돼도 실제 push나 POST가 실행됐습니다.

마지막 확인을 `git push` 명령과 `create_draft` POST **바로 앞**으로 옮겼습니다.

| 확인 | 결과 |
| --- | --- |
| `remote_head` 조회 도중 승인 회수 | push 0. stage=`push_command` |
| `remote_head` 조회 도중 lease 만료 | push 0 |
| `find_open` 도중 승인 회수 | `create_draft` 0. stage=`pr_create_call` |
| `find_open` 도중 claim 해제 | `create_draft` 0 |
| 기존 PR 채택 경로 | 외부 write가 아니므로 POST 직전 확인 없음. `Published` 확정 직전에는 확인 |
| 정상 경로 | 영향 없음 |

push가 필요 없는 경우(remote가 이미 같은 commit)는 외부 write가 없으므로 추가 확인을 하지 않습니다.

#### service가 요청 상태를 들고 있던 문제

`publish()`가 `worker_id`를 instance에 보관해, 같은 instance로 동시 게시하면 서로 덮어쓸 수 있었습니다. **다른 worker의 권한으로 확인**하게 됩니다.

`self._worker_id`를 제거하고 호출 인자로만 흘립니다.

| 확인 | 결과 |
| --- | --- |
| instance 상태 | `_worker_id` 속성 없음 |
| 서로 다른 worker의 게시 두 건 | 각 확인이 자기 worker를 사용 |
| 다른 worker의 게시 시도 | gate가 거부. push 0 |

#### 지문 계산 실패를 통과시키던 문제

`safe_content_digest`가 `computed=False`를 돌려줘도 무결성 확인이 계속 진행했고, commit 직후 확인도 지문이 없으면 건너뛰었습니다. "검증한 내용만 게시한다"는 보장을 증명하지 못한 채 게시되는 경로였습니다.

| 확인 | 결과 |
| --- | --- |
| 지문 계산 실패(`computed=false`) | `publication_content_digest_unavailable`. **commit 0, push 0, PR 0** |
| 빈 digest | 같은 분류로 차단 |
| commit 직후 지문 부재 | 건너뛰지 않고 실패 |
| 실패 기록 | raw source 없음 |
| 정상 지문 | 동작 변화 없음 |

#### 회귀 테스트가 실제로 잡는지 확인

네 수정(SSH exact URL, push 직전 확인, POST 직전 확인, 지문 fail-closed)을 각각 되돌렸습니다. **16건이 실패**했고 복원하니 전부 통과했습니다.

#### 재실행한 검증

- 전체 테스트 통과
- `compileall` (src, tests) 통과
- bare remote smoke 재실행 통과
- secret scan, `git diff --check` 통과

#### 확인하지 못한 항목

앞 절의 항목이 그대로 남습니다. 실제 GitHub push와 PR 생성, 실제 SSH remote로의 push는 여전히 미검증입니다.
