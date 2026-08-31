# Roadmap, Epics & Detailed Backlog

> 출처: [08. Roadmap, Epics & Detailed Backlog](https://app.notion.com/p/3cd9f036b307810583dce44763206949) (2026-08-31 동기화)

## Delivery Strategy

큰 플랫폼을 한 번에 만들지 않고 실제 사용 가능한 세로 단면을 반복해서 완성합니다.

## Current Baseline — Manual Issue to PR

현재 가능한 운영 흐름은 **GitHub Issue 생성 → 사람이 Issue를 Executor에게 전달 → Executor가 branch에서 작업·검증 → PR 생성 → 사람 review/merge**입니다.

Atlas worker, webhook/polling, 자동 claim, self-hosted Claude Code invocation, validation automation, GitHub delivery automation은 아직 구현되지 않았습니다.

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

## Milestone M1 — Automated Issue-to-PR Vertical Slice

**Outcome:** GitHub Issue로 등록한 문서 전용 Task를 Atlas worker가 검증·claim하고, always-available server의 self-hosted Claude Code worker가 수행한 뒤 validation 결과가 포함된 PR을 자동 생성합니다.

### Epic 1. Worker Intake and Claim

- [x] GitHub Issue Template 생성
- [x] Task Schema와 Issue Command Contract 작성
- [ ] webhook과 polling 중 하나를 선택
- [ ] Issue payload parser와 Task 필수 필드 검증
- [ ] repository permission과 Project allowlist 확인
- [ ] idempotent claim, lease, duplicate event 방지
- [ ] 수동 workflow를 유지하는 safe rollout flag

### Epic 2. Self-hosted Claude Code Worker

- [ ] always-available server hosting과 service identity 결정
- [ ] provider-neutral Executor Adapter 최소 interface
- [ ] Claude Code Adapter와 invocation contract
- [ ] Task별 isolated workspace, branch, lock
- [ ] timeout, cancel, process cleanup, redacted log
- [ ] Codex Cloud를 manual/secondary로 유지하고 자동 fallback은 제외

### Epic 3. Validation and Delivery

- [x] PR Output Contract와 PR template 작성
- [ ] project validation command 실행
- [ ] diff scope, forbidden path, secret scan
- [ ] GitHub PR 생성과 Issue 상태 comment
- [ ] 실패 보고와 retry budget
- [ ] 문서 전용 Issue 한 건으로 mobile E2E 검증

### M1 Explicit Non-Goals

- multi-agent collaboration과 자동 Reviewer 분리
- Codex Cloud 자동 fallback
- 전용 Web UI 또는 Telegram Bot
- vector memory와 semantic retrieval
- production deployment와 회사 Project 연결

## Milestone M2 — Atlas Control Plane

**Outcome:** Task 상태를 Atlas가 관리하고 Executor를 교체할 수 있습니다.

### Epic 4. Core Domain

- [ ] Workspace, Project, Task, Run 모델
- [ ] Run State Machine
- [ ] SQLite/PostgreSQL persistence
- [ ] Event log

### Epic 5. Multi-Executor Adapter Hardening

- [ ] M1의 Executor Adapter contract 안정화
- [ ] Claude Code Adapter capability 확장
- [ ] Codex Cloud manual/secondary Adapter 연결
- [ ] Mock executor for tests
- [ ] timeout/cancel/retry

## Milestone M3 — Context Builder

**Outcome:** 저장소 전체가 아니라 작업 관련 컨텍스트만 구성합니다.

### Epic 6. Context Manifest

- [ ] Schema
- [ ] Required docs loader
- [ ] Forbidden path filter

### Epic 7. Retrieval

- [ ] filename/symbol search
- [ ] related tests
- [ ] recent PR/commit metadata
- [ ] token budget packing

### Epic 8. Context Evaluation

- [ ] golden task set
- [ ] recall/precision 측정
- [ ] project leakage test

## Milestone M4 — Multi-Agent Routing

**Outcome:** 복수 Executor를 역할과 가용성에 따라 배정합니다.

- [ ] Agent Registry
- [ ] Role/Capability schema
- [ ] 수동 availability 상태
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
- [ ] worker trigger, server hosting, network egress 결정
- [ ] 첫 Atlas Task Issue 세트 생성
- [ ] self-hosted Claude Code worker 문서 Task PR 실험 수행

## Definition of Done for Every Task

- 목표와 범위가 명확합니다.
- Acceptance Criteria가 모두 확인됐습니다.
- 변경 사항과 검증 결과가 기록됐습니다.
- 관련 문서가 업데이트됐습니다.
- secret 또는 다른 Project 컨텍스트가 포함되지 않았습니다.
- 사람의 merge 승인이 필요하면 명시됐습니다.
