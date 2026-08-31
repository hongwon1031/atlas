# Task State Machine v0.1

이 문서는 Atlas Task와 Run의 수명주기를 정의합니다. 상태는 UI 표시가 아니라 권한, 재시도, 감사 로그를 제어하는 계약입니다. persistence와 workflow engine 구현은 아직 선택하지 않습니다.

현재는 상태 저장과 자동 transition이 구현되지 않았습니다. Current manual workflow에서는 사람이 Issue를 Executor에게 전달하고 Issue/PR 기록으로 논리적 상태를 추적합니다. Target MVP에서는 Atlas worker가 transition, claim lease, event log를 관리합니다.

## States

| 상태 | 의미 | 주요 책임자 |
| --- | --- | --- |
| `Draft` | 입력을 받았지만 실행 가능한지 검증되지 않음 | Task Intake |
| `NeedsClarification` | 필수 정보나 사람 판단이 부족함 | 사람 / Planner |
| `Planned` | 목표, 범위, 위험, 완료 조건과 계획이 확인됨 | Planner |
| `ContextReady` | 정책과 Task 관련 컨텍스트 packet이 준비됨 | Context Builder |
| `Queued` | 승인된 실행 대기열에 들어감 | Router / Scheduler |
| `Running` | 격리된 Runner에서 작업 중 | Executor |
| `Validating` | 변경과 Acceptance Criteria를 검증 중 | Validator |
| `PullRequestReady` | 검증 결과가 포함된 PR이 사람 검토를 기다림 | Delivery Adapter |
| `RevisionRequested` | 사람이 변경을 요청해 재계획이 필요함 | 사람 / Planner |
| `Approved` | 사람이 PR 결과를 승인했지만 완료 처리가 남음 | 사람 |
| `Completed` | 승인된 결과가 전달되고 Task가 종료됨 | Control Plane |
| `Failed` | 단계가 실패했고 원인과 재시도 가능성이 기록됨 | 실패 단계 owner |
| `Cancelled` | 사람 요청 또는 정책에 따라 작업이 안전하게 종료됨 | 사람 / Control Plane |

`Completed`와 `Cancelled`는 terminal state입니다. `Failed`는 원인과 retry policy에 따라 다시 `Planned`로 이동할 수 있습니다.

## State Diagram

```mermaid
stateDiagram-v2
    [*] --> Draft
    Draft --> NeedsClarification
    Draft --> Planned
    Draft --> Cancelled
    NeedsClarification --> Draft
    NeedsClarification --> Cancelled
    Planned --> ContextReady
    Planned --> NeedsClarification
    Planned --> Cancelled
    ContextReady --> Queued
    ContextReady --> Failed
    ContextReady --> Cancelled
    Queued --> Running
    Queued --> Failed
    Queued --> Cancelled
    Running --> Validating
    Running --> Failed
    Running --> Cancelled
    Validating --> PullRequestReady
    Validating --> Failed
    PullRequestReady --> Approved
    PullRequestReady --> RevisionRequested
    PullRequestReady --> Cancelled
    RevisionRequested --> Planned
    RevisionRequested --> Cancelled
    Approved --> Completed
    Failed --> Planned
    Failed --> Cancelled
```

기존 Architecture v0.1 상태에 모바일 취소 요구사항을 반영해 `Cancelled` terminal state를 명시적으로 추가했습니다. 별도의 `Cancelling` 중간 상태가 필요한지는 Runner 취소 PoC 후 결정합니다.

## Transition Contract

| 현재 | 다음 | Trigger | Guard / 필수 근거 |
| --- | --- | --- | --- |
| `Draft` | `NeedsClarification` | Intake 검증 실패 | 누락·모순 질문 목록 |
| `Draft` | `Planned` | `/atlas plan` 또는 Planner | Task Schema의 Planned 필드 완성 |
| `NeedsClarification` | `Draft` | 사람 답변 | source와 답변 audit 기록 |
| `Planned` | `ContextReady` | Context Builder 완료 | 필수 policy, source, selection reason |
| `ContextReady` | `Queued` | 현재: 사람 전달 / 목표: worker queue trigger | 권한, 위험, availability, 승인 확인 |
| `Queued` | `Running` | 현재: Executor 시작 / 목표: worker claim lease | branch/workspace lock과 Run ID |
| `Running` | `Validating` | Executor 결과 제출 | diff와 artifact checksum |
| `Validating` | `PullRequestReady` | 필수 검증 통과 | [PR Output Contract](pr-output-contract.md) 충족 |
| `PullRequestReady` | `RevisionRequested` | PR change request 또는 `/atlas revise` | 수정 지시와 actor |
| `RevisionRequested` | `Planned` | 재계획 완료 | 기존 Task와 revision 연결 |
| `PullRequestReady` | `Approved` | 사람 PR approval | 승인 actor와 commit SHA |
| `Approved` | `Completed` | 결과 전달 확인 | PR merge 또는 합의된 delivery evidence |
| 비terminal | `Failed` | 단계 오류 | 실패 종류, redacted error, retryability |
| 허용 상태 | `Cancelled` | `/atlas cancel` | 권한 있는 actor, 사유, 정리 결과 |
| `Failed` | `Planned` | `/atlas retry` 또는 policy | retry budget과 변경된 계획 |

## Entry and Exit Rules

### `NeedsClarification`

- 질문은 한 번에 답할 수 있도록 구체적으로 작성합니다.
- 답변 전에는 Context Builder나 Executor를 시작하지 않습니다.
- 고위험 모호성은 기본값으로 보완하지 않습니다.

### `Queued`

- 정확한 Project, repository, base branch가 고정되어야 합니다.
- Executor capability와 availability가 확인되어야 합니다.
- 고위험 변경의 사전 승인이 기록되어야 합니다.
- 동일 branch를 점유한 다른 Run이 없어야 합니다.
- Current manual workflow에서는 사람의 명시적 전달이 queue 승인 증거입니다.
- Target MVP에서는 Atlas worker의 idempotent claim과 유효한 lease가 필요합니다.

### `Running`

- Run은 고유 ID, dedicated branch, worktree/clone, executor process, log scope, actor, 시작 시각, timeout, cancellation state를 가집니다.
- 모든 side effect는 허용된 scope와 command policy 안에 있어야 합니다.
- cancel 요청을 받으면 새 side effect를 중단하고 정리 결과를 기록합니다.
- 여러 Task가 mutable worktree를 공유하거나 여러 Run이 같은 branch를 동시에 수정할 수 없습니다.

### `Validating`

- Validator는 Executor의 완료 주장만 신뢰하지 않고 계획된 검증을 실행합니다.
- 실행하지 못한 검증은 pass가 아니라 `not_run`과 이유로 기록합니다.
- secret scan, forbidden path, 변경 범위 검사는 생략할 수 없습니다.

### `PullRequestReady`

- PR head는 Task 전용 branch이고 base는 승인된 default branch입니다.
- PR body는 변경 파일, 검증, 위험, open question을 포함합니다.
- application code PR이면 필요한 테스트 결과가 없을 때 Ready로 이동할 수 없습니다.

## Failure Taxonomy

| 종류 | 기본 처리 |
| --- | --- |
| `clarification_required` | `NeedsClarification`으로 이동 |
| `transient_executor` | 동일 Executor 1회 retry 후보 |
| `authentication` | credential 노출 없이 중단하고 다른 Adapter 또는 사람에게 반환 |
| `usage_exhausted` | availability를 갱신하고 재라우팅 후보 |
| `validation_failed` | `Failed` 후 revision plan 필요 |
| `policy_violation` | 즉시 중단; 자동 retry 금지 |
| `project_boundary` | 즉시 중단; 사람 검토 필수 |
| `timeout` | side effect 정리 후 retryability 평가 |

## Retry and Idempotency

- 동일 실패에 대한 자동 retry는 기본 1회이며 Task별 policy가 더 엄격하면 그 값을 따릅니다.
- command comment ID, event ID, Run ID를 idempotency key로 사용합니다.
- 동일 transition 요청을 다시 받으면 새 Run을 만들지 않고 기존 결과를 반환합니다.
- retry Run은 이전 Run, 실패 원인, 변경된 plan을 참조합니다.
- retry는 Acceptance Criteria나 scope를 몰래 변경할 수 없습니다.
- lease expiry만으로 새 Run을 만들지 않고 heartbeat, worker ownership, process identity를 reconcile합니다.
- worker restart는 [Execution Runtime](execution-runtime.md)의 recovery 절차로 stale lease, orphan process, stale worktree를 확인합니다.

## Current and Target State Ownership

| 항목 | Current manual workflow | Target MVP workflow |
| --- | --- | --- |
| Intake validation | 사람 | Atlas worker |
| Claim / queue | 사람이 Executor에게 전달 | worker lease와 idempotency key |
| Primary execution | 사람이 선택한 Executor | self-hosted Claude Code worker |
| Secondary execution | Codex Cloud를 포함한 수동 선택 | Codex Cloud manual/secondary; 자동 fallback 미결정 |
| State record | Issue/PR comment와 사람 보고 | persisted Task state와 append-only event |
| Validation trigger | Executor와 사람이 실행 | Validator가 policy에 따라 실행 |
| Merge approval | 사람 | 사람 |

## Transition Event

모든 상태 변경은 최소한 다음 정보를 기록합니다.

```yaml
event_id: evt-unique
task_id: ATLAS-0001
run_id: run-0001
from: Queued
to: Running
trigger: scheduler_lease
actor: agent:executor-id
occurred_at: 2026-08-31T00:00:00Z
reason: self-hosted Claude Code worker claimed the task
evidence:
  branch: docs/example
  commit: null
```

로그에 secret이나 원문 credential 오류를 포함하지 않습니다.

## Authorization

- 사람만 `Queued`, `Approved`, `Cancelled`로 가는 고위험 transition을 승인할 수 있습니다.
- Current manual workflow에서는 사람이 Issue 전달로 queue를 승인합니다.
- Target MVP에서는 Atlas worker만 claim/lease transition을 기록하고 self-hosted Claude Code Executor는 자신의 Run을 `Running`과 `Validating` 사이에서만 이동하도록 제한합니다.
- Validator와 Delivery Adapter는 검증 근거 없이 `PullRequestReady`를 기록할 수 없습니다.
- AI는 `Approved` 또는 `Completed`를 사람 승인 없이 생성하지 않습니다.

## Open Questions

- Runner가 즉시 멈추지 못할 때 `Cancelling` 상태를 추가할지
- PR approval과 merge를 각각 상태로 분리할지
- timeout과 retry budget의 Task별 기본값
- Issue label을 상태의 source of truth로 사용할지 projection으로만 사용할지
- Proposed polling-first trigger의 interval, backoff, production scaling policy
- claim lease duration, heartbeat, abandoned Run recovery 정책
- polling interval, source revision과 approval signal idempotency key
