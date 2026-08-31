# ADR-003: Initial Execution Environment

- Status: Proposed
- Date: 2026-08-31
- Decision owners: Project owner

## Context

Atlas는 집 PC에 지속적으로 접근하지 않고도 원격 Task를 실행해야 합니다. 초기 후보는 Codex Cloud, Home PC Runner, VPS Runner입니다. 장기적으로 특정 Executor에 종속되지 않아야 합니다.

## Proposed Decision

- 첫 Remote PR Proof는 Codex Cloud 경로를 우선 검증합니다.
- Atlas core는 provider-neutral Executor Adapter 계약에만 의존합니다.
- Home PC와 VPS Runner는 초기 E2E가 검증된 뒤 별도 Adapter로 평가합니다.
- credential, filesystem, network, timeout 경계가 문서화되기 전에는 고위험 Task를 실행하지 않습니다.

## Alternatives Considered

### Home PC Runner first

- 장점: 기존 구독형 도구를 활용하고 환경 제어권이 높습니다.
- 단점: 전원, network reachability, local data isolation 운영이 필요합니다.

### VPS Runner first

- 장점: 상시 가동과 runtime 제어가 쉽습니다.
- 단점: server/API 비용, patching, secret, network 보안 운영이 추가됩니다.

### 자체 Executor 구현

- 장점: 완전한 lifecycle 제어가 가능합니다.
- 단점: 이미 해결된 coding agent 기능을 재구현하며 MVP 범위를 크게 늘립니다.

## Consequences

- 원격 문서 변경 PR을 가장 짧은 경로로 검증할 수 있습니다.
- 초기 capability는 Codex Cloud가 노출하는 인터페이스에 제한됩니다.
- provider-specific 기능은 Adapter 내부에 격리해야 합니다.
- 다른 Runner의 비용과 제어 이점은 후속 PoC로 비교합니다.

## Security Impact

- GitHub App 또는 token은 대상 repository에 필요한 최소 권한만 가집니다.
- `main` write와 merge 권한을 Executor에 제공하지 않습니다.
- 외부 실행 환경으로 전달되는 context를 Project allowlist와 forbidden path로 제한합니다.
- 실행 로그와 PR에서 credential 오류 원문을 redact합니다.

## Follow-up Tasks

- [ ] Project owner가 Codex Cloud 우선 결정을 승인
- [ ] Executor Adapter interface 작성
- [ ] Codex Cloud permission과 repository boundary 확인
- [ ] 문서 전용 Task → 검증 → PR E2E PoC
- [ ] Home PC/VPS 비교 기준과 중단 조건 작성
