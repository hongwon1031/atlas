# Usage and Availability Specification v0.1

이 문서는 Executor의 실행 가능성과 불확실한 usage limit을 안전하게 표현하는 계약입니다. [ADR-006](../adr/0006-agent-availability-and-usage.md)은 `Proposed`이며 자동 quota detection은 구현되지 않았습니다.

## Availability States

| 상태 | 의미 | 기본 routing 행동 |
| --- | --- | --- |
| `available` | 최근 근거상 실행 가능 | 다른 guard 통과 시 후보 |
| `limited` | 일부 window, budget, policy 제약 | 작은 Task 또는 사람 승인 후보 |
| `exhausted` | 알려진 limit 소진 | reset 전 자동 배정 금지 |
| `unknown` | 신뢰할 최신 근거 없음 | available로 간주하지 않고 사람 확인 |
| `offline` | worker 또는 service를 사용할 수 없음 | routing 후보 제외 |

## Usage Record

```yaml
agent_id: personal-claude-worker-01
state: unknown
usage_window_type: rolling_five_hour
resets_at: null
weekly_state: unknown
remaining_estimate: null
availability_source: manual
source_observed_at: 2026-08-31T00:00:00Z
last_usage_failure:
  category: null
  occurred_at: null
  retry_after: null
```

`remaining_estimate`는 정확한 provider quota로 오인되지 않도록 값, 단위, confidence, 관찰 시각을 함께 가져야 합니다. 근거가 없으면 `null`입니다.

## Evidence Sources

공식 usage API가 없으면 Atlas는 다음 근거를 사용할 수 있습니다.

- 사람의 수동 상태, rolling five-hour reset time, weekly limit 상태 입력
- 운영자가 설정한 `resets_at`
- 최근 execution success 또는 failure signal
- 분류된 usage-exhausted 오류와 provider가 명시한 retry time
- worker heartbeat 또는 offline signal

source precedence와 freshness는 policy로 정합니다. 오래된 `available` 상태는 자동으로 신뢰하지 않습니다.

## Accuracy Rules

- 공식적으로 제공되지 않는 remaining quota를 정확한 숫자로 주장하지 않습니다.
- page scraping, cookie 수집, 비공식 endpoint로 quota를 추정하지 않습니다.
- rolling five-hour와 weekly limit은 초기에는 사람이 입력할 수 있는 window 유형이지 자동 측정 완료 기능이 아닙니다.
- 인증 오류, network 오류, timeout을 usage exhaustion으로 자동 오분류하지 않습니다.
- execution failure 원문은 저장하지 않고 category, time, redacted detail만 기록합니다.

## State Updates

1. manual input은 actor, timestamp, optional expiry를 기록합니다.
2. configured reset time이 지나면 `exhausted`를 즉시 `available`로 단정하지 않고 `unknown` 또는 policy-defined 상태로 바꿉니다.
3. usage-exhausted signal은 `last_usage_failure`를 갱신하고 `exhausted` 또는 `limited` 후보로 만듭니다.
4. 정상 execution은 service reachability 근거지만 정확한 remaining quota 근거는 아닙니다.
5. worker heartbeat 실패는 usage와 별개로 `offline` 후보가 됩니다.

## Security and Account Separation

- 개인/회사 authentication profile은 별도 usage record를 가집니다.
- cookie, session token, billing detail을 usage 확인 목적으로 추가 수집하지 않습니다.
- public log, Issue, PR에는 account identity, 정확한 subscription detail, raw provider error를 노출하지 않습니다.
- routing은 Task Workspace와 authentication profile boundary를 교차하지 않습니다.

## Open Questions

- manual update를 허용할 actor와 UI
- freshness TTL과 state precedence
- `remaining_estimate` 단위와 confidence 표현
- reset 후 `unknown`에서 `available`로 승격하는 근거
- provider별 error taxonomy 유지 위치
