# GitHub Event Ingestion Specification v0.1

이 문서는 GitHub Issue를 Atlas Task 후보로 발견하고 exactly-one active Run으로 연결하기 위한 Target MVP 계약입니다. [ADR-008](../adr/0008-initial-github-event-ingestion.md)의 polling-first는 `Accepted`이며 poller, persistent idempotency, atomic claim, lease가 구현됐습니다. approval signal은 `atlas:queued` label로 확정됐습니다. webhook과 Run 생성은 아직 구현되지 않았습니다.

## Scope

- initial transport: polling (구현됨)
- source: 허용된 repository의 GitHub Issues와 comment/label metadata
- output: parsed Task candidate, validation result, idempotent claim request
- 제외: executor invocation, validation execution, PR delivery 구현

## Candidate Polling Flow

1. repository allowlist 안에서 open candidate Issues를 나열합니다.
2. Atlas Task Issue Form marker와 approved 또는 queued signal을 확인합니다.
3. Issue body를 [Task Schema](task-schema.md)로 parse합니다.
4. actor permission, Project, repository, scope, risk, required field를 검증합니다.
5. source revision과 signal identity로 idempotency key를 계산합니다.
6. active/delivered Run을 확인하고 lease를 atomic하게 획득합니다.
7. 정확히 한 개의 Run record를 만들고 runtime queue에 전달합니다.
8. 같은 candidate를 다시 보면 기존 validation, claim, Run 결과를 반환합니다.

현재 구현은 6단계까지입니다. valid Task를 등록하고 lease 기반 claim을 획득하지만 Run record는 만들지 않습니다(`active_run_id`는 null). 7단계는 후속 slice입니다.

## Candidate and Approval Rules

- Issue가 존재한다는 사실만으로 실행하지 않습니다.
- Issue Form marker, supported repository, explicit approval/queue signal을 모두 확인합니다.
- `atlas:queued` label은 approval signal로 동작합니다. `/atlas` comment command는 아직 자동 효과가 없습니다.
- approval signal은 `atlas:queued` label로 확정했고 기본으로 필수입니다. GitHub가 label 추가를 triage 이상 권한자로 제한하므로 label이 authorization gate 역할을 합니다.
- `require_queue_label`을 끄면 label 없는 후보도 **등록**되지만 승인되지 않으므로 claim 대상이 되지 않습니다. 설정으로 approval 정책을 우회할 수 없습니다.
- label을 추가한 actor의 repository permission을 Atlas가 직접 재확인하지는 않습니다.
- Issue edit는 기존 승인을 자동으로 재사용하지 않습니다. 실행에 영향을 주는 field가 바뀌면 validation revision과 재승인이 필요합니다.

## Idempotency Keys

최소한 다음 identity를 보존합니다.

```yaml
repository_id: github-repository-id
issue_id: github-issue-id
issue_revision: normalized-content-hash
signal_type: queue-command-or-label   # 현재 구현: queue_label:atlas:queued
signal_id: github-event-or-comment-id
task_id: ATLAS-0001
```

- 같은 source identity는 같은 Task candidate를 반환합니다.
- active lease 또는 delivered PR이 있으면 새 Run을 만들지 않습니다.
- source content가 바뀌면 기존 Task와의 revision relationship을 기록합니다.
- failed Run retry는 ingestion duplicate가 아니라 명시적인 retry decision으로 생성합니다.

## Ingestion Claim Lease

claim은 ingestion 단계의 lease이며 [Task State Machine](task-state-machine.md)의 실행 claim과 구분됩니다.

- Task 상태를 `Queued`나 `Running`으로 옮기지 않습니다. 현재 구현은 `Draft`를 유지합니다.
- Run record를 만들지 않으며 `active_run_id`는 null입니다.
- executor를 실행하지 않습니다.
- 목적은 "한 Task를 한 worker만 처리한다"를 보장하는 것입니다.

### Approval은 지속 상태입니다

approval을 polling 시점의 필터로만 쓰면 label이 제거된 뒤에도 과거 승인으로 claim할 수 있습니다. 따라서 다음을 요구합니다.

- approval 여부와 signal 식별자를 Task와 함께 저장합니다.
- claim은 저장된 approval을 다시 확인합니다. 승인되지 않은 Task는 claim 대상이 아닙니다.
- poller는 후보 조건을 잃은 Issue(label 제거, Issue 종료, marker 변경, 내용이 invalid로 변경)를 관찰하면 승인을 회수하고 active claim을 해제합니다.
- 승인 근거 없이 저장된 과거 Task는 claim 대상이 아닙니다.

회수는 polling pass에서 일어나므로 label 제거와 회수 사이에 interval만큼의 창이 있습니다. claim 직전에 GitHub 최신 상태를 재조회하지는 않습니다.

GitHub Issue 목록 endpoint는 eventual consistency를 가지므로 변경 직후 pass가 이를 관찰하지 못할 수 있습니다. 따라서 회수 지연은 polling interval과 provider 인덱싱 지연의 합으로 보아야 합니다.

## Polling Operations

- cursor, last-observed timestamp, conditional request metadata를 persistence에 저장합니다.
- pagination 중 worker가 재시작해도 page 재처리가 안전해야 합니다.
- GitHub rate-limit signal을 존중하고 backoff하되 Task를 실패로 오표시하지 않습니다.
- polling interval, jitter, concurrency, repository별 budget은 open question입니다.
- 한 worker iteration이 여러 candidate를 발견해도 MVP scheduler는 한 Task와 한 PR을 순차 처리할 수 있습니다.

## Webhook Migration

webhook은 transport adapter로 추가하며 다음 core 동작을 재사용합니다.

- source normalization
- authorization과 schema validation
- idempotency key
- claim/lease
- Task/Run creation

webhook 도입 전 signature validation, replay window, delivery retry, endpoint availability, event ordering을 정의합니다. Polling은 webhook delivery gap reconciliation 경로로 남길 수 있습니다.

## Security

- GitHub token은 repository에 저장하지 않고 최소 read/write 범위로 worker에 주입합니다.
- Issue, comment, label text는 신뢰되지 않은 content입니다.
- command argument를 shell, branch, filesystem path로 직접 변환하지 않습니다.
- raw payload를 장기 저장하기 전에 secret, 개인정보, private repository detail을 redact합니다.
- public inbound endpoint가 없는 것이 polling-first의 초기 보안·운영 이점입니다.

## Open Questions

- polling interval, jitter, backoff, rate-limit budget
- label을 추가한 actor의 permission을 Atlas가 직접 재확인할지
- Issue edit 후 재승인 규칙
- multi-repository polling fairness
- webhook migration 시점과 reconciliation 기간
