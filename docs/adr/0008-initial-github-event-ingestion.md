# ADR-008: Initial GitHub Event Ingestion

- Status: Accepted
- Date: 2026-08-31
- Accepted: 2026-09-02
- Decision owners: Project owner

## Context

Atlas의 Target MVP는 GitHub Issue에서 승인되거나 queue된 Task를 감지해야 합니다. GitHub webhook은 낮은 지연 시간을 제공하지만 public inbound endpoint, signature 검증, delivery 재처리, network 운영이 필요합니다. Polling은 지연이 더 크지만 always-available server에서 outbound GitHub access만으로 시작할 수 있습니다.

polling은 이 ADR 승인 후 구현됐고 webhook은 아직 구현되지 않았습니다.

## Accepted Scope (2026-09-02)

Project owner가 PR #6 리뷰 과정에서 polling-first 방식의 장단점을 확인한 뒤 명시적으로 승인했습니다. 구현 지시 자체를 승인으로 해석한 것이 아니며, 승인 질의와 답변을 근거로 상태를 변경했습니다. 확정한 값과 계속 열려 있는 값을 구분합니다.

**확정**

- transport는 polling이며 webhook은 후속 transport로 남깁니다.
- candidate 판정은 open Issue + Pull Request 제외 + `[Atlas Task]` 제목 marker입니다.
- interval, backoff, per-page, max-page는 코드에 고정하지 않고 `PollingConfig`로 노출합니다. 초기 기본값은 interval 60초, backoff 5초에서 최대 300초까지 2배 증가입니다.
- polling cursor는 repository별 최신 `updated_at`을 store에 저장하고 다음 pass의 `since`로 사용합니다.
- source 오류(rate limit, 인증, network)는 Task를 실패로 표시하지 않고 backoff 후 재시도합니다.

**계속 열림**

- approval/queue signal로 `atlas:queued` label을 **필수로 확정**했습니다. GitHub가 label 추가를 triage 이상 권한자로 제한하므로 label 자체가 authorization gate 역할을 합니다. 다만 label을 추가한 actor의 권한을 Atlas가 직접 재확인하지는 않으며, 그 검증은 후속 과제입니다.
- production scaling policy와 multi-repository fairness.
- webhook migration 시점.

## Decision

- Initial MVP는 GitHub polling으로 candidate Issue를 조회합니다.
- worker는 candidate Issue만 parse·validate하고 valid Task를 idempotent하게 등록한 뒤 atomic claim으로 하나의 lease만 허용합니다. Run 생성은 후속 slice입니다.
- Issue number, source revision, command 또는 label event identity를 idempotency key에 포함합니다.
- 같은 Issue나 command를 반복해서 관찰해도 중복 Run 또는 PR을 만들지 않습니다.
- polling interval, backoff, pagination은 설정으로 노출합니다. rate-limit budget과 production scaling policy는 계속 open question입니다.
- webhook은 초기 vertical slice가 검증된 뒤 동일한 ingestion contract를 호출하는 후속 transport로 추가합니다.

## Conceptual Flow

1. 허용된 repository에서 candidate Issues를 나열합니다.
2. 승인 또는 queue 의도가 있는 Task를 식별합니다.
3. Issue Form을 Task Schema로 parse하고 권한·Project·scope를 검증합니다.
4. Task를 lease 기반으로 idempotent하게 claim합니다.
5. 기존 active 또는 delivered Run이 없을 때 한 개의 Run을 시작합니다.

## Alternatives Considered

### Webhook first

- 장점: 낮은 지연 시간과 event-driven 처리가 가능합니다.
- 단점: public inbound endpoint, signature와 replay 방어, delivery 운영이 초기 PoC 범위를 늘립니다.

### Polling first

- 장점: 배포가 단순하고 public inbound endpoint가 필요 없으며 운영 부담이 낮습니다.
- 단점: 지연, GitHub API rate limit, cursor와 duplicate 관리를 설계해야 합니다.

### Manual dispatch only

- 장점: 자동 ingestion 구현이 필요 없습니다.
- 단점: 현재 수동 흐름을 넘어서는 Target MVP를 검증할 수 없습니다.

## Consequences

- 초기 worker는 outbound-only GitHub 연결로 Issue-to-PR vertical slice를 검증할 수 있습니다.
- polling 자체가 exactly-once delivery를 보장하지 않으므로 claim, lease, Run uniqueness가 필수입니다.
- webhook으로 이동해도 Task parser, authorization, idempotency, claim contract는 재사용합니다.
- polling 지연과 API 사용량은 실제 운영 측정 후 조정해야 합니다.

## Security Impact

- GitHub credential은 repository에 저장하지 않고 최소 repository 권한으로 주입합니다.
- Issue와 comment는 신뢰되지 않은 입력으로 처리하며 shell, path, branch에 직접 보간하지 않습니다.
- private repository 이름, server 주소, token, raw event payload의 민감한 값은 public log나 문서에 남기지 않습니다.
- webhook을 추가할 때 signature validation, replay protection, endpoint exposure를 별도로 위협 모델링합니다.

## Follow-up Tasks

- [x] Project owner가 polling-first 제안을 승인
- [x] candidate Issue filter 확정 (open + non-PR + `[Atlas Task]` marker)
- [x] polling interval, backoff, pagination을 설정으로 노출
- [x] duplicate Task registration acceptance test 작성
- [x] approval/queue signal을 `atlas:queued` label 필수로 확정
- [ ] label을 추가한 actor의 repository permission을 Atlas가 직접 재확인
- [ ] duplicate Run/PR acceptance test 작성 (Run 생성은 아직 미구현)
- [ ] rate-limit budget과 production scaling policy 결정
- [ ] webhook migration trigger와 보안 요구사항 정의
