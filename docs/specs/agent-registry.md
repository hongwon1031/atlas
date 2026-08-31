# Agent Registry Specification v0.1

이 문서는 Atlas가 역할, Executor, account, runtime availability를 혼동하지 않고 routing하기 위한 future Agent Registry 계약을 정의합니다. Registry와 Router는 아직 구현되지 않았습니다.

## Concept Separation

- **Atlas:** Task를 수집하고 계획·상태·dispatch·delivery를 조율하는 orchestrator입니다.
- **Role:** Planner, Researcher, Implementer, Reviewer, Validator, Reporter처럼 한 Run에서 수행할 책임입니다.
- **Executor:** Claude Code, Codex Cloud, future API model, local model처럼 작업을 수행하는 교체 가능한 adapter 대상입니다.
- **Authentication profile:** 개인 또는 회사 account와 credential boundary를 나타냅니다.
- **Worker registration:** 특정 execution host에서 특정 authentication profile로 Executor를 실행할 수 있는 등록입니다.

같은 Executor가 policy와 capability에 따라 여러 Role을 수행할 수 있습니다. Role 이름을 provider account나 credential identity로 사용하지 않습니다.

## Registry Record

| 필드 | 형식 | 설명 |
| --- | --- | --- |
| `agent_id` | string | registration의 stable unique ID |
| `provider` | string | `anthropic`, `openai`, `local` 같은 provider identity |
| `adapter_type` | string | 호출 경계를 구현하는 adapter 종류 |
| `execution_host` | string | redacted host registration ID; public address가 아님 |
| `authentication_profile` | string | credential을 직접 포함하지 않는 profile reference |
| `capabilities` | string[] | 실제 검증된 작업 capability |
| `supported_roles` | string[] | policy상 수행 가능한 Role |
| `availability` | enum | `available`, `limited`, `exhausted`, `unknown`, `offline` |
| `usage_state` | object | [Usage and Availability](usage-availability.md) record reference |
| `security_scope` | object | Workspace, Project, repository, network, permission 경계 |
| `current_run` | string/null | active Run ID; concurrency policy에 따라 null 또는 하나 |

## Example

```yaml
agent_id: personal-claude-worker-01
provider: anthropic
adapter_type: claude_code_self_hosted
execution_host: host-registration-01
authentication_profile: personal-claude-profile
capabilities: [repo_search, code_write, test_execution, pr_create]
supported_roles: [Planner, Researcher, Implementer, Validator, Reporter]
availability: unknown
usage_state:
  ref: usage-personal-claude-worker-01
security_scope:
  workspace_ids: [personal]
  project_ids: [atlas]
  repositories: [hongwon1031/atlas]
  permission_level: open_pr
current_run: null
```

예시는 contract 설명용이며 registration, credential, worker가 현재 존재한다는 뜻이 아닙니다.

## Authentication Boundaries

- 개인 account와 회사 account는 별도 authentication profile과 별도 worker registration으로 표현합니다.
- profile 간 credential, usage state, repository allowlist, logs, memory를 공유하지 않습니다.
- Registry에는 token, cookie, key, credential file path 원문을 저장하지 않습니다.
- `authentication_profile`은 secret manager 또는 host-local credential configuration의 opaque reference입니다.
- routing은 Task의 Workspace와 `security_scope`가 정확히 일치하는 registration만 후보로 사용합니다.

## Support Status

| Executor / Adapter | 상태 | 허용된 설명 |
| --- | --- | --- |
| Codex Cloud manual | Proven Manually | 사람이 prompt 전달 → branch 변경 → PR 생성 → 사람 merge |
| Atlas-to-Codex Cloud automation | Feasibility Unverified | automated adapter로 등록하기 전 integration validation 필요 |
| self-hosted Claude Code | Planned | Target MVP primary automated executor; invocation 미구현 |
| Claude API, OpenAI API, Gemini | Not Implemented | future adapter 후보 |
| local models | Not Implemented | future adapter 후보 |

Codex Cloud를 자동 candidate로 표시하려면 먼저 supported invocation, identity, branch/PR delivery, cancellation, status reporting을 검증해야 합니다.

## Routing Guards

1. Task의 Workspace와 Project가 `security_scope` 안에 있어야 합니다.
2. required capability와 requested Role이 registration과 일치해야 합니다.
3. availability와 usage state가 policy상 실행 가능해야 합니다.
4. `current_run`과 concurrency policy가 새 Run을 허용해야 합니다.
5. credential profile과 execution host가 운영 상태여야 합니다.
6. 불명확하거나 stale한 정보는 `unknown`으로 취급하고 자동 실행을 보수적으로 중단합니다.

## Open Questions

- Registry의 canonical operational storage
- `agent_id`와 worker registration ID를 분리할지
- capability 검증과 만료 정책
- 한 registration의 동시 Run 수를 항상 1로 제한할지
- Codex Cloud automated invocation의 기술적·정책적 feasibility
