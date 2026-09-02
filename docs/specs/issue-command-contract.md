# GitHub Issue Command Contract v0.1

이 문서는 GitHub Issue를 Atlas의 초기 모바일 Task channel로 사용할 때 comment와 label이 Task 상태를 변경하는 Target MVP 규칙을 정의합니다. [ADR-002](../adr/0002-initial-mobile-task-channel.md)는 GitHub Issues와 기존 Atlas Task Issue Form을 Accepted intake로 확정했고, [ADR-008](../adr/0008-initial-github-event-ingestion.md)의 polling-first 감지도 Accepted입니다.

comment command automation은 아직 구현되지 않았습니다. `/atlas` comment는 아무 효과가 없습니다. `atlas:queued` label은 poller의 approval signal로 동작해 Task 등록과 claim 대상 여부를 결정하지만, Run 실행이나 PR delivery를 시작하지는 않습니다. label을 추가한 actor의 권한을 Atlas가 직접 재확인하지는 않습니다.

## Current Manual Workflow

1. 사람이 Atlas Task Issue Form으로 Issue를 생성합니다.
2. 사람이 Task 필드와 권한을 확인합니다.
3. 사람이 Issue URL과 지시를 선택한 Executor에게 전달합니다.
4. Executor가 branch, 작업, 검증, PR을 수행합니다.
5. 사람과 Executor가 필요한 상태를 Issue/PR comment로 직접 기록합니다.

자동 처리로 오인되지 않도록 command를 현재 운영 절차의 필수 단계로 요구하지 않습니다.

## Goals

- Target MVP에서 Atlas worker가 처리할 command contract를 고정합니다.
- 휴대전화에서 짧고 예측 가능한 명령으로 Task를 제어합니다.
- GitHub permission과 Atlas policy를 모두 확인합니다.
- 중복 webhook과 comment 재전송에도 같은 결과를 반환합니다.
- 명령 승인, 거부, 상태 변경을 Issue timeline에서 감사할 수 있게 합니다.

## Parsing Rules

- 명령은 comment의 첫 번째 non-empty line에서 시작해야 합니다.
- 명령 prefix는 소문자 `/atlas`이며 대소문자를 구분합니다.
- 한 comment에는 하나의 명령만 허용합니다.
- 인용문, code fence, inline code 안의 문자열은 명령으로 처리하지 않습니다.
- 처리된 comment를 edit해도 새 명령으로 실행하지 않습니다. 변경하려면 새 comment를 작성합니다.
- 알 수 없는 command나 잘못된 argument는 side effect 없이 거부합니다.
- comment body와 argument는 신뢰되지 않은 입력으로 취급하고 로그 출력 전에 redaction합니다.

## Commands

다음 command는 Target MVP worker가 구현된 뒤에만 효력이 있습니다.

| Command | 허용 상태 | 결과 |
| --- | --- | --- |
| `/atlas status` | 모든 상태 | 현재 Task, Run, 필요한 사람 action 요약 |
| `/atlas plan` | `Draft` | schema 검증 후 `Planned` 또는 `NeedsClarification` |
| `/atlas queue` | `ContextReady` | 권한과 guard 통과 시 `Queued` |
| `/atlas cancel <reason>` | `Draft`~`PullRequestReady`, `Failed` | 안전한 정리 후 `Cancelled`; reason 필수 |
| `/atlas retry <reason>` | `Failed` | retry budget 확인 후 `Planned`; reason 필수 |
| `/atlas revise <instruction>` | `PullRequestReady` | revision을 기록하고 `RevisionRequested` |

여러 줄의 수정 지시는 첫 줄을 `/atlas revise`로 쓰고 다음 줄부터 본문으로 제공할 수 있습니다. 명령 이름 뒤와 후속 본문을 합친 값이 비어 있으면 거부합니다.

## Label Trigger

현재 `atlas:queued` label 추가는 poller가 Task를 등록하고 claim 대상으로 삼는 approval signal입니다. Executor 호출과 Run 실행은 아직 하지 않습니다. 아래 조건 전체 검사는 Target MVP에 적용됩니다.

`atlas:queued` label 추가는 `/atlas queue`와 같은 의도를 나타내며, 현재 poller가 이를 Task 등록의 approval signal로 사용합니다. 아래 조건 전체를 검사하는 상태 transition은 Run 실행이 구현될 때 적용됩니다.

- label actor가 repository에서 triage 이상의 권한을 가집니다.
- Task가 `ContextReady`입니다.
- risk와 사전 승인 guard를 통과했습니다.
- branch lock과 Executor availability를 확인했습니다.

조건을 통과하지 못하면 label을 상태의 증거로 사용하지 않고 Issue comment로 거부 이유를 남깁니다. 향후 구현은 state label을 source of truth가 아니라 Task state의 projection으로 취급해야 합니다.

## Authorization

| 동작 | 최소 권한 |
| --- | --- |
| `status` | repository 읽기 권한 |
| `plan` | triage 또는 그 이상 |
| `queue` / `atlas:queued` | triage 또는 그 이상 |
| `cancel` | triage 또는 그 이상; 실행 중이면 추가 policy 적용 |
| `retry` | write 또는 그 이상 |
| `revise` | triage 또는 PR change request 권한 |

Public repository의 Issue 작성자라는 사실만으로 mutation command 권한을 부여하지 않습니다. GitHub actor identity와 현재 repository permission을 명령 처리 시점에 확인합니다.

## State Guards

- `project_id`가 repository allowlist와 일치해야 합니다.
- Task Schema의 현재 상태 필수 필드가 완성돼야 합니다.
- `secrets_deployment` Task는 queue할 수 없습니다.
- CI·인프라와 dependency 변경은 필요한 사전 승인을 참조해야 합니다.
- 이미 terminal state인 Task의 mutation은 거부합니다.
- 같은 comment ID를 다시 전달받으면 이전 응답을 반환합니다.
- 같은 Issue revision과 queue signal을 polling에서 반복 관찰해도 active Run이나 PR을 추가 생성하지 않습니다.
- Issue의 실행 필드가 edit되면 기존 approval을 자동 재사용하지 않고 새 revision validation이 필요합니다.

## Response Contract

Atlas는 모든 명령 comment에 하나의 구조화된 응답을 남깁니다.

```markdown
### Atlas Command Result

- Command: `/atlas queue`
- Result: accepted
- Task: `ATLAS-0001`
- Previous state: `ContextReady`
- Current state: `Queued`
- Run: pending
- Required action: none
- Correlation ID: `issue-comment-123`
```

거부 응답은 `Result: rejected`와 사람이 해결할 수 있는 reason을 포함합니다. 내부 stack trace, credential 원문, 민감한 policy 세부사항은 포함하지 않습니다.

## Status Projection Labels

향후 자동화는 다음 label 중 하나만 현재 상태 projection으로 유지하는 것을 권고합니다.

- `atlas:draft`
- `atlas:needs-clarification`
- `atlas:planned`
- `atlas:context-ready`
- `atlas:queued`
- `atlas:running`
- `atlas:validating`
- `atlas:review`
- `atlas:failed`
- `atlas:cancelled`
- `atlas:completed`

현재 repository는 label automation을 구현하지 않았습니다.

## Notification Rules

Target MVP에서 다음 사건에는 Issue comment 또는 연결된 모바일 notification이 필요합니다. Current manual workflow에서는 사람이 필요한 comment와 PR link를 기록합니다.

- Task 접수와 정규화 결과
- 사용자 질문 필요
- queue 승인 또는 거부
- 실행 시작과 장시간 실행
- 비용·사용량 한도 접근
- PR 생성
- 실패와 retry 소진
- 취소 완료
- 승인 후 완료

## Security Rules

- comment의 URL, shell snippet, 외부 문서를 자동으로 신뢰하거나 실행하지 않습니다.
- command argument를 shell command, branch name, file path로 직접 보간하지 않습니다.
- 권한 확인 실패는 deny로 처리합니다.
- bot 자신이 생성한 comment는 command source로 처리하지 않습니다.
- audit record에는 actor, permission snapshot, comment ID, 이전·다음 상태, 결과를 포함합니다.

## Open Questions

- `triage`를 mutation command의 공통 최소 권한으로 유지할지
- Issue author에게 제한적인 `cancel` 권한을 허용할지
- label set을 repository bootstrap 과정에서 자동 생성할지
- 장문의 revision instruction에 별도 form 또는 attachment가 필요한지
- polling interval, backoff, production scaling policy
- Issue edit 후 재승인에 필요한 field와 permission
