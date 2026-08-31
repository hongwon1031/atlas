# ADR-006: Agent Availability and Usage

- Status: Proposed
- Date: 2026-08-31
- Decision owners: Project owner

## Context

Atlas는 여러 Executor의 capability, online 상태, 비용, 구독 잔여량을 고려해 Task를 배정해야 합니다. 일부 서비스는 공식 잔여량 API를 제공하지 않거나 사용자 구독 session만 노출합니다. 부정확한 추정은 반복 실패와 무단 credential 사용으로 이어질 수 있습니다.

## Proposed Decision

- availability의 정규 상태는 `available`, `limited`, `exhausted`, `unknown`, `offline`입니다.
- 공식 API가 없으면 사람이 rolling five-hour window, weekly state, reset time, remaining estimate와 유효 기간을 입력할 수 있습니다.
- record는 `usage_window_type`, `resets_at`, `weekly_state`, `remaining_estimate`, `availability_source`, `last_usage_failure`를 구분합니다.
- configured reset time, execution success/failure, usage-exhausted 오류를 상태 변경 후보 signal로 사용하되 원문 credential 정보를 저장하지 않습니다.
- `unknown`은 `available`과 같게 취급하지 않고 보수적인 routing 또는 사람 확인을 요구합니다.
- 자동 retry는 기본 1회이며 인증·quota 오류는 다른 Adapter로 재라우팅하는 후보가 됩니다.
- 공식 API가 제공하지 않는 remaining quota를 정확한 자동 측정값으로 주장하지 않습니다.

## Alternatives Considered

### 항상 available로 가정

- 장점: routing이 단순합니다.
- 단점: 실패 반복, 비용 낭비, 사용자 경험 저하가 발생합니다.

### 오류 signal만 사용

- 장점: 별도 입력 UI가 필요 없습니다.
- 단점: 실제 작업을 실패시켜야 상태를 알 수 있습니다.

### 비공식 사용량 scraping

- 장점: 자동 잔여량 추정이 가능할 수 있습니다.
- 단점: 불안정하고 보안·약관 위험이 있어 채택하지 않습니다.

## Consequences

- 공식 API가 없는 Executor도 명시적인 불확실성으로 관리할 수 있습니다.
- 사용자가 수동 상태를 갱신해야 할 수 있습니다.
- Router는 capability 외에 freshness와 confidence를 고려해야 합니다.
- 정확한 비용 최적화는 후속 단계로 남습니다.
- 정상 execution은 reachability 근거일 뿐 precise remaining quota의 근거가 아닙니다.

## Security Impact

- 개인 구독 credential을 availability 조회만을 위해 추가 수집하지 않습니다.
- 오류 message는 분류 후 redact하고 원문 token이나 cookie를 저장하지 않습니다.
- 회사 Workspace의 Executor 상태를 개인 Workspace와 공유하지 않습니다.

## Follow-up Tasks

- [ ] Project owner가 수동 상태 허용 기준을 승인
- [x] availability와 usage record 초안 작성: [Usage and Availability Specification](../specs/usage-availability.md)
- [ ] availability record TTL과 source precedence 확정
- [ ] 오류 → 상태 signal mapping 작성
- [ ] routing fallback acceptance test 정의
