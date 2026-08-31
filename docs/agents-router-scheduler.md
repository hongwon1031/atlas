# Agent Roles, Router & Scheduler

> 출처: [04. Agent Roles, Router & Scheduler](https://app.notion.com/p/3cd9f036b30781818330ffa40507c20d) (2026-08-31 동기화)

> 실행 환경 결정: [ADR-003](adr/0003-initial-execution-environment.md) Accepted

## Role Model

Atlas는 orchestrator, dispatcher, state manager, delivery coordinator입니다. 역할은 모델 계정 및 Executor와 분리합니다. 동일한 Claude 또는 Codex 실행기가 policy에 따라 Planner, Researcher, Implementer, Reviewer, Validator, Reporter 역할을 수행할 수 있습니다.

## Initial Roles

### PM / Planner

- 요구사항 정규화
- 작업 분해
- 완료 조건 확인

### Researcher

- 문서, 코드, 레퍼런스 조사
- 구현하지 않고 근거와 선택지를 제공

### Implementer

- 코드와 문서 수정
- 테스트 추가

### Reviewer

- diff, 아키텍처, 위험 요소 검토
- 구현자와 가능한 한 다른 Agent 사용

### QA / Validator

- 명령 기반 검증
- Acceptance Criteria 확인

### Reporter

- 모바일용 결과 요약

## Agent Capability Model

```yaml
capabilities:
  - code_write
  - repo_search
  - web_research
  - test_execution
  - pr_create
  - long_context
  - image_understanding
  - local_tool_access
```

Future [Agent Registry](specs/agent-registry.md)는 `agent_id`, `provider`, `adapter_type`, `execution_host`, `authentication_profile`, `capabilities`, `supported_roles`, `availability`, `usage_state`, `security_scope`, `current_run`을 구분합니다. 개인과 회사 account는 별도 authentication profile 또는 worker registration으로 등록합니다.

## Executor Support Status

| Executor path | 상태 | 설명 |
| --- | --- | --- |
| Codex Cloud manual | Proven Manually | 사람 prompt → Codex branch 변경 → Codex PR → 사람 merge |
| self-hosted Claude Code | Planned | primary automated executor; worker와 invocation 미구현 |
| Atlas-to-Codex Cloud | Feasibility Unverified | automated adapter로 routing하기 전 integration validation 필요 |
| Claude API, OpenAI API, Gemini | Not Implemented | future API adapter 후보 |
| local model | Not Implemented | future adapter 후보 |

## Routing Inputs

- Task 유형과 위험도
- 필요한 capability
- Executor 온라인 여부
- 사용자 입력 가용성
- 예상 비용과 잔여 사용량
- 최근 성공률
- 데이터 위치와 보안 등급

## Current Manual Routing

현재 Atlas Router와 Scheduler는 구현되지 않았습니다.

1. 사람이 GitHub Issue의 Project, Objective, Scope, Acceptance Criteria를 확인합니다.
2. 사람이 Issue를 Claude Code, Codex Cloud 또는 다른 선택된 Executor에게 전달합니다.
3. Executor가 Task branch에서 작업하고 PR을 생성합니다.
4. 사람의 재배정이 fallback routing을 대신합니다.

Issue 생성, `/atlas` command, label 변경만으로 실행이 자동 시작되지는 않습니다.

## Target MVP Routing

1. Atlas worker가 GitHub Issue를 검증하고 lease 기반 idempotent claim을 획득합니다.
2. Router는 security scope, capability, role, availability를 확인하고 primary `claude_code_self_hosted` Adapter를 선택합니다.
3. self-hosted Claude Code worker가 always-available server의 전용 worktree/clone과 새 executor process에서 실행합니다.
4. primary worker를 사용할 수 없으면 Task를 실패 또는 사람 확인 상태로 반환합니다.
5. Codex Cloud는 사람이 선택하는 manual/secondary executor로 유지합니다. 자동 fallback은 별도 결정 전까지 수행하지 않습니다.

## MVP Routing Policy

1. Target MVP의 primary automated executor는 self-hosted Claude Code worker입니다.
2. 역할은 Executor와 분리하므로 같은 worker가 PM, Researcher, Implementer 역할을 수행할 수 있습니다.
3. Codex Cloud는 manual/secondary executor이며 자동 primary routing 대상이 아닙니다.
4. 다른 Executor를 사용할 수 있으면 구현과 리뷰를 분리합니다.
5. 고위험 변경은 자동 실행을 금지하거나 사람 승인 후 실행합니다.
6. 사용량이나 worker 상태가 불명확하면 보수적으로 실행하지 않고 사람에게 반환합니다.
7. 여러 Project가 conversation을 공유하거나 여러 Task가 mutable worktree를 공유하거나 여러 Run이 같은 branch를 동시에 수정하지 않습니다.

## Usage State

서비스가 공식 잔여량 API를 제공하지 않으면 다음 상태를 수동 입력, configured reset, execution signal로 관리하도록 [Usage and Availability](specs/usage-availability.md)에서 제안합니다. 자동 상태 관리 기능은 아직 구현되지 않았습니다.

- `available`
- `limited`
- `exhausted`
- `unknown`
- `offline`

초기 record는 다음 정보를 구분합니다.

- `usage_window_type`: rolling five-hour, weekly, provider-defined, unknown
- `resets_at`: 사람이 입력하거나 provider가 명시한 reset 시각
- `weekly_state`: available, limited, exhausted, unknown
- `remaining_estimate`: optional estimate와 단위·confidence; 근거가 없으면 null
- `availability_source`: manual, configured reset, execution signal, official API
- `last_usage_failure`: redacted category, 시각, optional retry time

정상 실행은 service reachability signal일 뿐 정확한 remaining quota의 증거가 아닙니다. Atlas는 지원되지 않는 정밀 quota detection을 주장하지 않으며 rolling five-hour와 weekly limit을 초기에는 수동으로 입력할 수 있습니다.

## Fallback Policy

- Current manual workflow: primary Executor 실패 시 사람이 재시도 또는 Codex Cloud를 포함한 다른 Executor로 재배정합니다.
- Target MVP: transient Claude Code worker 실패는 동일 Executor 1회 재시도 후보입니다.
- 인증·사용량·worker offline 오류는 자동 Codex fallback을 수행하지 않고 redacted 원인과 함께 사람에게 반환합니다.
- 테스트 실패는 동일 Task의 revision run 후보입니다.
- 프로젝트가 불명확하거나 claim guard가 실패하면 실행을 중단하고 사용자에게 질문합니다.

## Anti-Patterns

- 여러 에이전트가 같은 브랜치를 동시에 수정
- Reviewer가 근거 없이 전체 구현을 다시 작성
- 사용량 절감을 위해 검증 단계를 생략
- Agent 개인 메모리가 프로젝트 정책보다 우선

## Subtasks

- [ ] Role 정의 파일 포맷 설계
- [ ] Capability taxonomy 확정
- [ ] Agent Registry에 `claude_code_self_hosted` primary와 `codex_cloud` manual/secondary 등록
- [ ] 개인/회사 authentication profile을 분리한 Agent Registry schema 구현
- [ ] Availability와 rolling/weekly reset 수동 입력 구현
- [ ] primary-only 규칙 기반 Router 최소 구현
- [ ] worker claim lease와 heartbeat 정책 구현
- [ ] 재시도·Fallback 정책 구현
- [ ] 동일 브랜치 동시 실행 Lock 구현
- [ ] Reviewer 분리 정책 구현
- [ ] 실행 결과 기반 성공률 통계 설계
- [ ] 비용·사용량 대시보드 후속 Issue 생성
- [ ] Atlas-to-Codex Cloud invocation, cancellation, delivery feasibility 검증
