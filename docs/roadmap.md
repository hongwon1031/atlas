# Roadmap, Epics & Detailed Backlog

> 출처: [08. Roadmap, Epics & Detailed Backlog](https://app.notion.com/p/3cd9f036b307810583dce44763206949) (2026-08-31 동기화)

## Delivery Strategy

큰 플랫폼을 한 번에 만들지 않고 실제 사용 가능한 세로 단면을 반복해서 완성합니다.

## Milestone M0 — Foundation

**Outcome:** AI와 사람이 동일하게 이해할 수 있는 프로젝트 정의가 존재합니다.

- [ ] Vision & Constitution 리뷰
- [ ] PRD v0.1 리뷰
- [ ] Architecture v0.1 리뷰
- [ ] Research 결과와 Build/Adopt 결정
- [ ] GitHub `docs/` 동기화 계획

## Milestone M1 — Remote PR Proof

**Outcome:** 휴대전화에서 지시한 문서 변경이 PR로 생성됩니다.

### Epic 1. Task Intake

- [ ] GitHub Issue Template 생성
- [ ] Task 필수 필드 검증
- [ ] 실행 라벨과 취소 명령 정의

### Epic 2. Executor Connection

- [ ] Codex Cloud와 Atlas 저장소 연결
- [ ] 최소 권한 확인
- [ ] 문서 수정 Task 실행
- [ ] PR 자동 생성 검증

### Epic 3. Reporting

- [ ] PR 요약 템플릿
- [ ] 테스트/검증 결과 표시
- [ ] 모바일 알림 검증

## Milestone M2 — Atlas Control Plane

**Outcome:** Task 상태를 Atlas가 관리하고 Executor를 교체할 수 있습니다.

### Epic 4. Core Domain

- [ ] Workspace, Project, Task, Run 모델
- [ ] Run State Machine
- [ ] SQLite/PostgreSQL persistence
- [ ] Event log

### Epic 5. Executor Adapter

- [ ] Adapter interface
- [ ] Codex adapter
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
- [ ] MVP 입력 채널 결정
- [ ] MVP Executor 결정
- [ ] 공개 저장소에 노출하면 안 되는 개인 정보 제거
- [ ] Notion 문서를 Markdown으로 GitHub에 옮길 방법 결정
- [ ] Codex Cloud PR 실험 수행
- [ ] 첫 GitHub Issue 세트 생성

## Definition of Done for Every Task

- 목표와 범위가 명확합니다.
- Acceptance Criteria가 모두 확인됐습니다.
- 변경 사항과 검증 결과가 기록됐습니다.
- 관련 문서가 업데이트됐습니다.
- secret 또는 다른 Project 컨텍스트가 포함되지 않았습니다.
- 사람의 merge 승인이 필요하면 명시됐습니다.
