# Task Schema v0.1

이 문서는 Atlas가 입력 채널의 자연어 요청을 실행 가능한 Task로 정규화할 때 사용하는 정규 데이터 계약을 정의합니다. 현재 문서는 구현 명세이며 특정 언어, framework, database schema를 선택하지 않습니다.

[ADR-002](../adr/0002-initial-mobile-task-channel.md)에 따라 canonical intake는 GitHub Issues의 Atlas Task Issue Form입니다. Current manual workflow에서는 사람이 form을 확인하고 Executor에게 전달합니다. Target MVP에서는 Atlas worker가 같은 schema를 검증하고 claim합니다.

## 설계 목표

- 모든 Run이 하나의 Workspace와 Project에 명확히 속하도록 합니다.
- 사람의 요청과 Planner가 유도한 값을 구분합니다.
- Objective, Constraints, Acceptance Criteria, Allowed Scope를 실행 전에 고정합니다.
- 위험, 권한, 컨텍스트, 검증, 전달 조건을 라우팅 전에 확인합니다.
- Issue, API, 향후 모바일 UI가 같은 의미의 Task를 생성하도록 합니다.

## 수명주기별 완전성

### Intake에 필요한 필드

- `project_id`
- `objective`
- `constraints`
- `acceptance_criteria`
- `allowed_scope`
- `forbidden_scope`
- `risk_level`
- `priority`
- `validation_plan`

입력값이 부족하면 Task는 `NeedsClarification`으로 이동하며 Executor에 전달되지 않습니다.

### Planned 전에 필요한 필드

- `task_id`와 `workspace_id`
- 정규화된 source와 actor
- 확인된 Project와 repository
- 위험 분류와 필요한 capability
- 적용 정책과 컨텍스트 출처
- 전달 브랜치와 승인 요구사항
- current/target dispatch mode와 선택된 Executor policy

## Canonical Fields

| 필드 | 형식 | 필수 시점 | 설명 |
| --- | --- | --- | --- |
| `schema_version` | string | 생성 | 현재 값은 `0.1` |
| `task_id` | string | Planned 전 | 시스템이 발급하는 안정적인 식별자 |
| `workspace_id` | string | Planned 전 | credential과 policy의 최상위 격리 단위 |
| `project_id` | string | Intake | 하나의 허용된 Project 식별자 |
| `repository` | string | Planned 전 | `owner/name` 형식의 허용된 저장소 |
| `source` | object | 생성 | channel, URI, 요청 actor, 원본 시각 |
| `objective` | string | Intake | 하나의 검증 가능한 결과 |
| `constraints` | string[] | Intake | 비목표, 호환성, 일정, 보안 제약 |
| `acceptance_criteria` | object[] | Intake | ID, 설명, 검증 방법을 가진 완료 조건 |
| `allowed_scope` | object | Intake | 변경 가능한 path와 operation |
| `forbidden_scope` | object | Intake | 금지된 path, operation, external system |
| `priority` | enum | Intake | `low`, `normal`, `high`, `urgent` |
| `risk_level` | enum | Intake | 아래 Risk Level 참조 |
| `required_capabilities` | string[] | Planned 전 | 역할과 분리된 실행 capability |
| `preferred_role` | string 또는 null | Planned 전 | PM, Researcher, Implementer, Reviewer, QA, Reporter |
| `status` | enum | 생성 | [Task State Machine](task-state-machine.md)의 상태 |
| `clarification_questions` | object[] | 필요 시 | 질문, 답변, actor, 시각 |
| `context_refs` | object[] | ContextReady 전 | source, revision, selection reason, trust level |
| `validation_plan` | object[] | Intake | 검사 ID, 종류, 필수 여부, 성공 조건 |
| `delivery` | object | Planned 전 | base branch, PR 요구 여부, 승인 정책 |
| `execution` | object | Planned 전 | dispatch mode, primary/selected Adapter, claim 정보 |
| `audit` | object | 생성 | 생성·수정 actor와 timestamp, correlation ID |

## Risk Level

낮은 값으로 분류해 승인을 우회하지 않습니다. 여러 유형이 적용되면 가장 높은 위험을 사용합니다.

1. `read_only` — repository나 외부 시스템을 변경하지 않는 조사
2. `documentation` — Markdown, template, governance 변경
3. `code` — application code와 test 변경
4. `dependency` — package, lockfile, 외부 action 변경
5. `ci_infrastructure` — CI, runner, network, deployment 설정 변경
6. `secrets_deployment` — credential, secret, production 변경; MVP 자동 실행 금지

## Scope Model

`allowed_scope`와 `forbidden_scope`는 자연어 설명만이 아니라 path와 operation 목록을 가집니다.

```yaml
allowed_scope:
  paths:
    - AGENTS.md
    - .github/**
    - docs/**
  operations:
    - create
    - update
forbidden_scope:
  paths:
    - .env
    - secrets/**
    - src/**
  operations:
    - delete
    - deploy
    - merge
  external_systems:
    - production
```

명시적 금지 범위가 허용 범위보다 우선합니다. 허용되지 않은 path나 operation은 기본적으로 금지합니다.

## Acceptance Criterion

각 항목은 다음 구조를 가집니다.

```yaml
- id: AC-01
  description: 요청된 문서와 template이 생성됐다.
  verification:
    type: file_presence
    evidence: required
```

설명은 pass/fail을 판단할 수 있어야 하며 “잘 동작한다”처럼 검증 방법이 없는 표현만 사용할 수 없습니다.

## Validation Plan

검증은 구현 방법이 아니라 성공 조건을 정의할 수 있습니다.

```yaml
validation_plan:
  - id: VAL-01
    type: format
    required: true
    success: Markdown과 YAML에 구조 오류가 없다.
  - id: VAL-02
    type: scope
    required: true
    success: application code 변경이 없다.
```

실행 환경이 선택된 뒤 실제 command가 추가될 수 있습니다. command를 실행하지 못하면 결과를 성공으로 기록하지 않고 이유와 대체 근거를 남깁니다.

## Source and Context Reference

```yaml
source:
  channel: github_issue
  uri: https://github.com/hongwon1031/atlas/issues/123
  actor: github:example-user
  created_at: 2026-08-31T00:00:00Z

context_refs:
  - source: docs/constitution.md
    revision: git:<commit-sha>
    reason: repository-wide safety policy
    trust: canonical
```

Context에는 secret 원문, 전체 인증 로그, 다른 Project의 자료를 넣지 않습니다.

## Execution Policy

```yaml
execution:
  dispatch_mode: manual
  primary_adapter: claude_code_self_hosted
  selected_adapter: codex_cloud
  claim_id: null
  claimed_by: human:project-owner
  lease_owner: null
  lease_expires_at: null
  active_run_id: null
```

- Current manual workflow의 `dispatch_mode`는 `manual`이며 사람이 `selected_adapter`와 전달 시점을 기록합니다.
- Target MVP의 `dispatch_mode`는 `worker`이며 기본 `primary_adapter`는 `claude_code_self_hosted`입니다.
- Codex Cloud는 `manual` 또는 명시적인 secondary 선택일 때만 사용합니다. 자동 fallback은 아직 결정되지 않았습니다.
- claim을 구현하면 `claim_id`, `claimed_by`, `lease_owner`, `lease_expires_at`, idempotency evidence를 함께 기록합니다.
- Run은 [Execution Runtime](execution-runtime.md)에 따라 unique Run ID, branch, worktree/clone, process, log scope, timeout, cancellation state를 별도 record로 가집니다.
- retry Run은 새 Run ID를 사용하고 이전 Run과 failure reason을 참조합니다. Task의 Acceptance Criteria와 scope는 명시적인 revision 없이 바꾸지 않습니다.

## Complete Example

```yaml
schema_version: "0.1"
task_id: ATLAS-0001
workspace_id: personal
project_id: atlas
repository: hongwon1031/atlas
source:
  channel: github_issue
  uri: https://github.com/hongwon1031/atlas/issues/1
  actor: github:hongwon1031
  created_at: 2026-08-31T00:00:00Z
objective: AI 에이전트가 안전하게 기여할 수 있는 문서 기반 개발 foundation을 만든다.
constraints:
  - Documentation only
  - No application code or infrastructure
acceptance_criteria:
  - id: AC-01
    description: AGENTS.md와 GitHub template이 존재한다.
    verification:
      type: file_presence
      evidence: required
allowed_scope:
  paths: [AGENTS.md, .github/**, docs/**, README.md]
  operations: [create, update]
forbidden_scope:
  paths: [.env, secrets/**, src/**]
  operations: [delete, deploy, merge]
  external_systems: [production]
priority: normal
risk_level: documentation
required_capabilities: [repo_search, code_write, pr_create]
preferred_role: Implementer
status: Planned
clarification_questions: []
context_refs:
  - source: docs/constitution.md
    revision: git:<commit-sha>
    reason: repository-wide safety policy
    trust: canonical
validation_plan:
  - id: VAL-01
    type: format
    required: true
    success: Markdown과 YAML 구조 검사가 통과한다.
delivery:
  base_branch: main
  pull_request_required: true
  human_merge_approval_required: true
execution:
  dispatch_mode: manual
  primary_adapter: claude_code_self_hosted
  selected_adapter: codex_cloud
  claim_id: null
  claimed_by: human:hongwon1031
  lease_owner: null
  lease_expires_at: null
  active_run_id: null
audit:
  created_by: github:hongwon1031
  created_at: 2026-08-31T00:00:00Z
  updated_at: 2026-08-31T00:00:00Z
  correlation_id: issue-1
```

## GitHub Issue Mapping

| Issue Form ID | Task 필드 |
| --- | --- |
| `project` | `project_id` |
| `objective` | `objective` |
| `constraints` | `constraints` |
| `acceptance_criteria` | `acceptance_criteria` |
| `allowed_scope` | `allowed_scope` |
| `forbidden_scope` | `forbidden_scope` |
| `risk_level` | `risk_level` |
| `priority` | `priority` |
| `validation` | `validation_plan` |
| `context` | 초기 `context_refs` 후보 |
| `notes` | 분류 전 risk와 open question 후보 |

Issue body는 신뢰되지 않은 사용자 입력입니다. Parser는 heading label에 의존해 값을 추출하고, Project allowlist와 path policy를 별도로 검증해야 합니다.

## Invariants

- 하나의 Task는 정확히 하나의 `workspace_id`, `project_id`, `repository`에 속합니다.
- 초기 source channel은 `github_issue`이며 Issue body는 신뢰되지 않은 입력입니다.
- `main`은 delivery base일 수 있지만 AI의 직접 write target일 수 없습니다.
- 모든 Acceptance Criterion은 실행 결과에서 evidence 또는 명시적인 미충족 상태를 가집니다.
- `secrets_deployment`는 MVP에서 자동으로 `Queued` 또는 `Running`으로 전이할 수 없습니다.
- `forbidden_scope` 위반을 발견하면 Run을 시작하지 않거나 안전하게 중단합니다.
- source 원문과 정규화 결과의 변경 이력을 audit event로 연결합니다.
- 한 Task에는 동시에 하나의 active Run과 하나의 유효 claim lease만 존재합니다.
- source Issue나 queue signal을 반복 관찰해도 같은 Task revision에 중복 Run 또는 PR을 만들지 않습니다.

## Open Questions

- Task ID를 GitHub Issue 번호에서 유도할지 별도 sequence로 발급할지
- `workspace_id`와 `project_id` registry의 정규 저장 위치
- schema version의 호환성과 migration 정책
- path glob 해석과 대소문자 정규화 방식
- worker claim lease duration과 heartbeat interval
- Proposed polling-first ingestion의 interval, backoff, approval/queue signal
