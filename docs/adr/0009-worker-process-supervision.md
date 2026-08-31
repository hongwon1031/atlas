# ADR-009: Worker Process Supervision

- Status: Proposed
- Date: 2026-08-31
- Decision owners: Project owner

## Context

Target MVP의 Atlas worker는 개인 PC가 꺼져 있어도 동작하도록 always-available server에서 실행됩니다. PoC에서는 빠르게 worker를 유지할 방법이 필요하지만, 장기 운영은 boot startup, restart policy, structured logging, health reporting, resource limit, lifecycle 관리가 필요합니다.

현재 worker, server provisioning, tmux session, systemd unit, Docker configuration은 구현되지 않았습니다.

## Proposed Decision

- worker PoC는 운영자가 관리하는 server의 `tmux` session 안에서 실행할 수 있습니다.
- `tmux`는 PoC process persistence와 관찰 편의를 위한 도구일 뿐 service manager 또는 Task isolation boundary가 아닙니다.
- Task마다 새로운 executor process를 시작하며, 여러 Task나 Project가 하나의 Claude Code conversation, shell session, tmux pane을 공유하지 않습니다.
- stable operation 단계에서는 `systemd` 또는 Docker 중 하나를 별도 ADR과 구현 Task로 선택해 startup, restart, logging, resource limit, health, shutdown을 관리합니다.
- 개인 PC는 server 기반 worker의 실행 의존성이 아니며 꺼져 있어도 됩니다.

## Alternatives Considered

### tmux for PoC

- 장점: 설정이 작고 worker output을 직접 관찰하기 쉽습니다.
- 단점: 자동 restart, health check, resource 정책, 표준화된 log lifecycle이 부족합니다.

### systemd first

- 장점: Linux host의 startup, restart, identity, logging을 직접 관리할 수 있습니다.
- 단점: host-specific configuration과 운영 결정을 PoC 전에 확정해야 합니다.

### Docker first

- 장점: packaging과 resource boundary를 표준화할 수 있습니다.
- 단점: image, volume, credential injection, update 정책이 초기 범위를 늘립니다.

## Consequences

- PoC 운영은 빠르게 시작할 수 있지만 사람이 session과 worker health를 확인해야 합니다.
- tmux pane 생존 여부를 Task 성공이나 Run ownership의 근거로 사용하지 않습니다.
- stable supervisor로 전환할 때 worker domain contract와 Run recovery contract는 유지합니다.
- server hosting provider와 stable supervisor 선택은 계속 열려 있습니다.

## Security Impact

- worker는 dedicated OS user와 최소 권한 credential을 사용해야 합니다.
- tmux socket과 session access는 해당 service identity와 승인된 operator로 제한해야 합니다.
- command output과 scrollback에 secret이 남지 않도록 redaction하고 raw provider 오류를 노출하지 않습니다.
- stable supervisor configuration은 public repository에 실제 server 주소, account, token을 포함하지 않아야 합니다.

## Follow-up Tasks

- [ ] Project owner가 tmux PoC 허용과 stable supervisor 후속 전환을 승인
- [ ] worker shutdown, heartbeat, restart recovery acceptance test 정의
- [ ] stable 단계의 systemd와 Docker 평가 기준 작성
- [ ] log rotation, health reporting, resource limit policy 결정
