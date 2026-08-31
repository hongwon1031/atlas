# ADR-004: Workflow Engine

- Status: Proposed
- Date: 2026-08-31
- Decision owners: Project owner

## Context

Atlas Task는 clarification, planning, context, queue, execution, validation, PR review, retry 상태를 거칩니다. 재시작 가능성과 audit가 필요하지만 MVP에서 workflow platform을 먼저 도입하면 운영 복잡성과 dependency가 늘어납니다.

후보는 직접 구현한 persisted state machine, LangGraph, Temporal입니다.

## Proposed Decision

- MVP는 [Task State Machine](../specs/task-state-machine.md)을 따르는 단순한 persisted state machine으로 시작합니다.
- 모든 transition은 명시적 guard, idempotency key, append-only event를 가집니다.
- domain state와 workflow engine API를 분리해 이후 Temporal 또는 다른 engine으로 이동할 수 있게 합니다.
- durability 요구가 단순 구현의 한계를 실제로 초과하기 전에는 LangGraph나 Temporal을 필수 dependency로 채택하지 않습니다.

## Alternatives Considered

### LangGraph

- 장점: Agent-oriented graph와 checkpoint 모델을 활용할 수 있습니다.
- 단점: Atlas domain state와 framework graph가 강하게 결합될 수 있습니다.

### Temporal

- 장점: durable execution, retry, timeout, signal 지원이 강력합니다.
- 단점: server 운영과 worker model이 초기 MVP보다 무겁습니다.

### In-memory state only

- 장점: 구현이 가장 단순합니다.
- 단점: 재시작, audit, idempotency 요구를 충족하지 못합니다.

## Consequences

- 초기 domain과 transition semantics를 빠르게 검증할 수 있습니다.
- retry, lease, crash recovery를 직접 명확히 정의해야 합니다.
- engine 도입 전까지 복잡한 장기 workflow 기능은 제한됩니다.
- event와 state contract를 framework-neutral하게 유지해야 합니다.

## Security Impact

- 승인과 권한 guard가 workflow callback이 아니라 domain transition에서 강제돼야 합니다.
- event log는 secret과 개인정보를 저장하지 않습니다.
- 중복 event가 같은 side effect를 반복하지 않도록 idempotency를 검증합니다.

## Follow-up Tasks

- [ ] Project owner가 단순 state machine 우선 결정을 승인
- [ ] transition event schema 작성
- [ ] crash/retry/idempotency acceptance test 정의
- [ ] Temporal 도입 판단 기준 수립
