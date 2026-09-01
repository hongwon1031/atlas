# Roadmap, Epics & Detailed Backlog

> 출처: [08. Roadmap, Epics & Detailed Backlog](https://app.notion.com/p/3cd9f036b307810583dce44763206949) (2026-08-31 동기화)

## Delivery Strategy

큰 플랫폼을 한 번에 만들지 않고 실제 사용 가능한 세로 단면을 반복해서 완성합니다.

## Current Baseline — Manual Issue to PR

현재 가능한 운영 흐름은 **GitHub Issue 생성 → 사람이 Issue를 Executor에게 전달 → Executor가 branch에서 작업·검증 → PR 생성 → 사람 review/merge**입니다.

Atlas worker, webhook/polling, 자동 claim, self-hosted Claude Code invocation, validation automation, GitHub delivery automation은 아직 구현되지 않았습니다.

Codex Cloud의 **사람 prompt → branch 변경 → PR 생성 → 사람 merge** 흐름은 `Proven Manually`입니다. Atlas-to-Codex automated invocation은 `Feasibility Unverified`이며 adapter backlog로 이동하기 전에 별도 integration validation이 필요합니다.

## Milestone M0 — Foundation

**Outcome:** AI와 사람이 동일하게 이해할 수 있는 프로젝트 정의가 존재합니다.

- [ ] Vision & Constitution 리뷰
- [ ] PRD v0.1 리뷰
- [ ] Architecture v0.2 리뷰
- [ ] Research 결과와 Build/Adopt 결정
- [x] GitHub Markdown canonical / Notion optional mirror 결정
- [x] GitHub Issues와 Atlas Task Form intake 결정
- [x] self-hosted Claude Code primary / Codex Cloud manual-secondary 결정
- [x] AI Agent guide, Task contract, PR contract, ADR 문서 foundation
- [x] execution runtime, Agent Registry, usage, polling ingestion, Project lifecycle specification 초안
- [x] 초기 구현 언어를 Python으로 확정 (ADR-011)
- [ ] Proposed ADR-004~010 검토; ADR-004~007은 계속 Proposed

## Milestone M1 — Automated Issue-to-PR Vertical Slice

**Outcome:** GitHub Issue로 등록한 문서 전용 Task를 Atlas worker가 polling, 검증, claim하고, 격리된 mock executor Run을 수행한 뒤 validation 결과가 포함된 draft PR을 자동 생성합니다. self-hosted Claude Code invocation은 control path 검증 후 별도 PR로 추가합니다.

### Epic 1. Worker Intake and Claim

- [x] GitHub Issue Template 생성
- [x] Task Schema와 Issue Command Contract 작성
- [ ] ADR-008 polling-first 제안 승인
- [x] Issue payload parser와 Task 필수 필드 검증
- [x] Project allowlist 확인 (actor repository permission 확인은 claim 단계로 남음)
- [ ] idempotent claim, lease, duplicate event 방지
- [ ] polling cursor, backoff, pagination, rate-limit handling
- [ ] 수동 workflow를 유지하는 safe rollout flag

### Epic 2. Isolated Worker Runtime

- [ ] provider-neutral Executor Adapter 최소 interface
- [ ] 최소 Task, Run, claim lease, heartbeat persistence
- [ ] Task별 worktree/clone, branch, process, Run ID, log scope
- [ ] mock executor invocation
- [ ] timeout, cancel, retry, process/worktree cleanup
- [ ] worker restart, stale lease, orphan resource recovery
- [ ] Proposed tmux PoC로 process persistence 검증

### Epic 3. Validation and Delivery

- [x] PR Output Contract와 PR template 작성
- [ ] project validation command 실행
- [ ] diff scope, forbidden path, secret scan
- [ ] GitHub PR 생성과 Issue 상태 comment
- [ ] 실패 보고와 retry budget
- [ ] 문서 전용 Issue 한 건으로 mobile E2E 검증

### Epic 4. Self-hosted Claude Code Integration

- [ ] always-available server hosting과 dedicated service identity 결정
- [ ] Claude Code Adapter와 invocation contract
- [ ] per-Task Claude Code process, redacted stdout/stderr 수집
- [ ] usage-exhausted, authentication, timeout failure mapping
- [ ] mock executor vertical slice와 같은 isolation·validation·delivery contract 검증
- [ ] Codex Cloud는 manual/secondary로 유지하고 자동 fallback은 제외

### M1 Explicit Non-Goals

- multi-agent collaboration과 자동 Reviewer 분리
- Codex Cloud 자동 fallback
- 전용 Web UI 또는 Telegram Bot
- vector memory와 semantic retrieval
- production deployment와 회사 Project 연결
- systemd 또는 Docker stable supervisor configuration

## Milestone M2 — Atlas Control Plane

**Outcome:** Task 상태를 Atlas가 관리하고 Executor를 교체할 수 있습니다.

### Epic 5. Core Domain

- [ ] Workspace, Project, Task, Run 모델
- [ ] Run State Machine
- [ ] SQLite/PostgreSQL persistence
- [ ] Event log

### Epic 6. Multi-Executor Adapter Hardening

- [ ] M1의 Executor Adapter contract 안정화
- [ ] Claude Code Adapter capability 확장
- [ ] Codex Cloud automated adapter feasibility validation
- [ ] feasibility가 확인된 경우에만 Codex Cloud automated adapter 설계
- [ ] timeout/cancel/retry

## Milestone M3 — Context Builder

**Outcome:** 저장소 전체가 아니라 작업 관련 컨텍스트만 구성합니다.

### Epic 7. Context Manifest

- [ ] Schema
- [ ] Required docs loader
- [ ] Forbidden path filter

### Epic 8. Retrieval

- [ ] filename/symbol search
- [ ] related tests
- [ ] recent PR/commit metadata
- [ ] token budget packing

### Epic 9. Context Evaluation

- [ ] golden task set
- [ ] recall/precision 측정
- [ ] project leakage test

## Milestone M4 — Multi-Agent Routing

**Outcome:** 복수 Executor를 역할과 가용성에 따라 배정합니다.

- [ ] Agent Registry
- [ ] Role/Capability schema
- [ ] 개인/회사 authentication profile 분리
- [ ] usage window, reset, weekly state를 포함한 수동 availability
- [ ] Rule-based Router
- [ ] Implementer/Reviewer 분리
- [ ] fallback routing

## Milestone M5 — Mobile Product

**Outcome:** GitHub UI에 의존하지 않는 모바일 운영 경험을 제공합니다.

- [ ] 인증 포함 경량 Web UI 또는 Telegram Bot
- [ ] Task form
- [ ] progress timeline
- [ ] approval/revision controls
- [ ] usage dashboard

## Milestone M6 — AI Trading Project Onboarding

**Outcome:** Atlas가 별도 Project인 AI Trading을 안전하게 개발합니다.

AI Trading은 long-running [Project lifecycle](specs/project-lifecycle.md)을 검증하는 example onboarding target입니다. Atlas 자체에 trading, broker, strategy 기능을 구현하는 milestone이 아닙니다.

- [ ] AI Trading Project Manifest
- [ ] 별도 repository와 credential
- [ ] initial PRD/architecture Task
- [ ] 첫 자동 구현 PR

## Sprint 0 — Immediate Checklist

- [ ] 이 문서 전체 사용자 검토
- [x] MVP 입력 채널을 GitHub Issues로 결정
- [x] MVP primary automated Executor를 self-hosted Claude Code worker로 결정
- [ ] 공개 저장소에 노출하면 안 되는 개인 정보 제거
- [x] GitHub Markdown canonical / Notion optional mirror 결정
- [ ] ADR-008 polling-first, ADR-009 tmux PoC, ADR-010 isolation 제안 검토
- [ ] polling interval, server hosting, stable supervisor, network egress 결정
- [ ] 첫 Atlas Task Issue 세트 생성
- [ ] mock executor 문서 Task PR 실험 수행
- [ ] 별도 후속 PR에서 self-hosted Claude Code worker 문서 Task 실험 수행

## Recommended Next Implementation Sprint

1. ~~Atlas GitHub Issue를 parse하고 validate합니다.~~ (완료 — `src/atlas/`)
2. approved 또는 queued Task를 polling합니다.
3. 한 Task를 idempotent하게 claim합니다.
4. 최소 Task와 Run 상태를 persist합니다.
5. 격리된 worktree와 branch를 생성합니다.
6. Task별 새 process에서 mock executor를 호출합니다.
7. 결과를 validate하고 draft PR을 생성합니다.
8. self-hosted Claude Code invocation은 별도 후속 PR에서 추가합니다.

이 Sprint는 한 Task와 한 PR을 순차 처리합니다. Agent Registry, usage-aware routing, dedicated mobile UI, automated Codex adapter는 후속 milestone입니다.

## Definition of Done for Every Task

- 목표와 범위가 명확합니다.
- Acceptance Criteria가 모두 확인됐습니다.
- 변경 사항과 검증 결과가 기록됐습니다.
- 관련 문서가 업데이트됐습니다.
- secret 또는 다른 Project 컨텍스트가 포함되지 않았습니다.
- 사람의 merge 승인이 필요하면 명시됐습니다.
