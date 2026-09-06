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

- 승인 회수 또는 claim 해제 이후 실행 중인 executor를 실제로 멈추는 동작. executor가 없어 취소할 대상이 없습니다. [Execution Runtime](specs/execution-runtime.md)에 executor slice 요구사항으로 기록했습니다.
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
