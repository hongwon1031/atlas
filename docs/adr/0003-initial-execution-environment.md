# ADR-003: Initial Execution Environment

- Status: Accepted
- Date: 2026-08-31
- Accepted: 2026-08-31
- Decision owners: Project owner

## Context

Atlas는 개인 PC가 꺼져 있어도 GitHub Issue를 처리할 수 있어야 합니다. 초기 후보는 Codex Cloud, 개인 PC의 Claude Code, 항상 가동되는 서버의 self-hosted Claude Code worker, 기타 VPS Runner였습니다. 장기적으로 core domain은 특정 Executor에 종속되지 않아야 하지만, MVP의 primary automated path는 하나로 명확해야 합니다.

## Decision

- primary automated executor는 운영자가 관리하는 always-available server에서 실행되는 self-hosted Claude Code worker입니다.
- Atlas worker는 GitHub Issue를 검증·claim하고 Claude Code Executor Adapter를 통해 작업을 실행합니다.
- Claude Code worker는 Task별 독립 workspace와 branch를 사용하고 validation 후 PR을 생성합니다. `main` write와 merge 권한은 갖지 않습니다.
- Codex Cloud는 사람이 직접 전달해 사용하는 manual executor 또는 primary worker가 사용할 수 없을 때 선택하는 secondary executor로 유지합니다. MVP의 자동 primary path로 간주하지 않습니다.
- Atlas core는 provider-neutral Executor Adapter 계약에 의존하며 Claude Code invocation 세부사항은 Adapter와 worker boundary 안에 격리합니다.
- server의 hosting provider와 위치, webhook 또는 polling trigger, worker process model은 후속 구현 결정으로 남깁니다.
- credential, filesystem, network, timeout, update 경계가 구현·검증되기 전에는 worker가 고위험 Task를 실행하지 않습니다.

## Alternatives Considered

### Personal PC Claude Code Runner

- 장점: 기존 구독형 도구를 활용하고 환경 제어권이 높습니다.
- 단점: 상시 가동과 network reachability를 보장하기 어렵고 개인 데이터와 실행 workspace 경계가 약해질 수 있습니다.

### Always-available server + Claude Code worker

- 장점: 상시 가동, Claude Code 활용, runtime과 repository boundary 제어가 가능합니다.
- 단점: server 비용, patching, process supervision, secret, network 보안 운영이 추가됩니다.

### Codex Cloud as primary

- 장점: 별도 worker server 구현과 운영 부담이 적습니다.
- 단점: automated claim·routing·runtime control이 제한되고 Atlas의 primary worker lifecycle과 결합하기 어렵습니다.

### 자체 Executor 구현

- 장점: 완전한 lifecycle 제어가 가능합니다.
- 단점: 이미 해결된 coding agent 기능을 재구현하며 MVP 범위를 크게 늘립니다.

## Consequences

- Atlas의 target MVP 실행 경로와 운영 책임이 명확해집니다.
- worker server의 가용성, patching, monitoring, backup, credential rotation을 운영자가 책임집니다.
- Claude Code의 provider-specific 기능과 인증 방식은 Adapter 내부에 격리해야 합니다.
- Codex Cloud는 수동 실행과 secondary fallback에 계속 사용할 수 있습니다.
- 다른 Executor를 추가해도 Task Schema, state machine, PR contract는 유지합니다.

## Security Impact

- worker는 전용 service identity와 대상 repository에 필요한 최소 GitHub 권한만 사용합니다.
- `main` write와 merge 권한을 worker와 Executor에 제공하지 않습니다.
- Task별 filesystem workspace, branch lock, command policy, timeout을 강제합니다.
- server에는 필요한 credential만 주입하고 prompt, repository, audit log에 평문으로 저장하지 않습니다.
- network egress는 기본 deny 또는 allowlist 정책을 후속 보안 결정으로 확정합니다.
- Claude Code와 secondary Executor로 전달되는 context를 Project allowlist와 forbidden path로 제한합니다.
- 실행 로그와 PR에서 credential·개인정보·provider 오류 원문을 redact합니다.

## Follow-up Tasks

- [x] Project owner가 self-hosted Claude Code worker primary / Codex Cloud manual-secondary 결정을 승인
- [ ] worker trigger로 webhook과 polling 중 하나를 결정
- [ ] always-available server의 hosting 위치와 운영 책임 정의
- [ ] provider-neutral Executor Adapter interface 작성
- [ ] Claude Code invocation, timeout, cancel, redaction contract 작성
- [ ] repository allowlist, branch lock, isolated workspace 검증
- [ ] 문서 전용 Issue → claim → Claude Code → validation → PR E2E PoC
