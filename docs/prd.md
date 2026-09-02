# Atlas PRD v0.1

> 상태: 초안
>
> 출처: [01. PRD v0.1](https://app.notion.com/p/3cd9f036b30781cea343fecbf2c434df) (2026-08-31 동기화)

## Executive Summary

Atlas는 모바일에서 받은 개발 지시를 프로젝트 단위의 실행 가능한 Task로 변환하고, 필요한 컨텍스트를 구성하여 적합한 AI 코딩 에이전트에 배정한 뒤, 격리 환경에서 작업, 테스트, PR 생성을 수행하는 오케스트레이션 제품입니다.

## Target User

### Primary Persona — Solo Developer Operator

- 낮에는 본업 때문에 개인 PC를 사용하기 어렵습니다.
- 휴대전화로 작업을 지시하고 퇴근길에 결과를 검토하고 싶습니다.
- 여러 AI 구독 또는 실행기를 보유하지만 사용량이 제한적입니다.
- 개인 프로젝트와 회사 프로젝트가 섞이는 것을 원하지 않습니다.

## Jobs To Be Done

- “폰에서 한 문장으로 개발 작업을 맡기고, 검토 가능한 PR을 받고 싶다.”
- “세션이 초기화되어도 다음 에이전트가 이전 작업을 이어받게 하고 싶다.”
- “Claude 사용량이 남으면 조사·리뷰에 활용하고, 부족하면 다른 에이전트로 넘기고 싶다.”
- “어떤 AI가 무엇을 했고 왜 그렇게 결정했는지 확인하고 싶다.”

## Goals

- 모바일 Task 생성부터 PR 생성까지 E2E 자동화
- 프로젝트별 컨텍스트와 실행 환경 격리
- 복수 Agent Adapter 지원
- 작업, 비용, 사용량, 결과 추적
- 사람 승인 기반의 안전한 Git 워크플로

## MVP Non-Goals

- 완전 자율적인 제품 기획과 무제한 반복 개선
- 사람 승인 없는 프로덕션 배포
- 모든 Git 제공자와 모든 AI 모델 지원
- 회사 내부 시스템에 대한 즉시 연결
- 벡터 DB 기반 대규모 장기 기억
- AI Trading 구현 자체

## MVP Scope

### 포함

- Workspace 1개, Project 1개 등록
- GitHub 저장소 연결
- GitHub Issues의 Atlas Task Form을 사용하는 모바일 친화적 Task 생성 인터페이스
- primary automated Executor: always-available server의 self-hosted Claude Code worker
- manual/secondary Executor: Codex Cloud
- 정적 규칙 기반 Context Builder
- 별도 브랜치 생성, 파일 수정, 테스트 실행, PR 생성
- 작업 상태와 결과 요약
- 수동 승인

### 제외

- 자동 merge
- 복잡한 멀티 에이전트 협업
- 사용량 API가 없는 서비스의 완전 자동 잔여량 탐지
- Notion ↔ GitHub 양방향 실시간 동기화

## Current Manual Workflow

1. 사용자가 GitHub Issue의 Atlas Task Form으로 Task를 등록합니다.
2. 사람이 Task를 검토하고 선택한 Executor에게 전달합니다.
3. Executor가 별도 branch에서 작업·검증하고 PR을 생성합니다.
4. 사람이 PR을 검토하고 merge하거나 수정을 요청합니다.

사람이 번호를 지정한 GitHub Issue 한 건을 fetch·parse·schema validation하는 intake core는 구현됐습니다. 자동 worker, polling, claim, persistence, Run validation, GitHub delivery는 아직 구현되지 않았습니다. Codex Cloud의 사람 prompt → branch 변경 → PR 생성 → 사람 merge 흐름은 수동으로 입증됐습니다. Atlas-to-Codex automated invocation은 feasibility가 검증되지 않았고, self-hosted Claude Code primary automated path는 계획됐지만 구현되지 않았습니다.

## Target MVP User Flow

1. 사용자가 휴대전화에서 GitHub Issue의 Atlas Task Form으로 작업을 생성합니다.
2. Atlas worker가 Task를 정규화하고 위험도와 필요 역량을 분류한 뒤 claim합니다.
3. Context Builder가 정책 문서, 관련 코드, 최근 Issue/PR, 테스트 정보를 수집합니다.
4. Router가 primary self-hosted Claude Code Executor를 선택합니다.
5. always-available server의 Runner가 dedicated worktree/clone, branch, per-Task executor process에서 작업합니다.
6. Validator가 테스트, lint, diff 제한, 비밀정보 검사를 수행합니다.
7. Atlas가 PR과 모바일용 결과 요약을 생성합니다.
8. 사용자가 휴대전화에서 승인, 수정 요청, 폐기를 선택합니다.

## Functional Requirements

### FR-01 Workspace & Project

- Workspace는 자격증명과 정책의 최상위 격리 단위입니다.
- Project는 하나의 저장소와 프로젝트 메모리 묶음입니다.

### FR-02 Task Intake

- 자연어 작업 설명을 수신합니다.
- 목표, 제약, 완료 조건, 우선순위 필드를 구조화합니다.
- 불명확한 고위험 요청은 실행 전에 질문합니다.

현재 구현은 manual CLI로 지정한 단건 GitHub Issue를 `Draft` 또는 `NeedsClarification`으로 정규화하는 단계까지입니다. candidate discovery, permission 확인, approval/queue signal, claim은 후속 범위입니다.

### FR-03 Context Builder

- 필수 문서와 관련 파일을 토큰 예산 안에서 선택합니다.
- 컨텍스트 출처와 선택 이유를 기록합니다.

### FR-04 Agent Router

- 역할 적합성, 가용성, 비용, 위험도에 따라 Executor를 선택합니다.
- 실행 실패 시 정책에 따라 재시도하거나 사람에게 반환합니다.

### FR-05 Execution

- 별도 브랜치 또는 worktree에서 실행합니다.
- 허용된 명령과 경로만 사용합니다.

### FR-06 Validation

- 프로젝트가 정의한 테스트를 실행합니다.
- 테스트를 실행하지 못하면 이유를 보고합니다.

### FR-07 GitHub Delivery

- PR 제목, 요약, 변경 파일, 테스트 결과, 위험 요소를 포함합니다.

### FR-08 Mobile Reporting

- 진행 상태와 승인 필요 여부를 짧게 제공합니다.

## Non-Functional Requirements

- **Security:** 비밀정보 격리, 최소 권한, 감사 로그
- **Reliability:** 재시작 가능한 작업 상태, idempotent 실행
- **Portability:** Executor Adapter 인터페이스
- **Observability:** 단계별 이벤트, 비용, 오류 기록
- **Latency:** 소규모 문서 작업은 15분 이내 결과 목표
- **Maintainability:** 정책, 프롬프트, 실행 코드를 분리

## Success Metrics

- Task → PR E2E 성공률 80% 이상
- 잘못된 프로젝트 컨텍스트 혼입 0건
- 사용자 개입 없이 PR 초안까지 도달하는 비율 70% 이상
- 실패 Task의 원인 식별 가능률 95% 이상
- 모바일 승인까지 필요한 사용자 조작 5단계 이하

## MVP Acceptance Criteria

- [ ] 휴대전화에서 Task를 등록할 수 있습니다.
- [ ] Atlas 저장소에 문서 변경 PR을 자동 생성합니다.
- [ ] PR에 diff 요약과 실행된 검증 결과가 포함됩니다.
- [ ] 사용자가 승인하기 전 `main`은 변경되지 않습니다.
- [ ] 다른 Project의 파일을 컨텍스트로 사용하지 않습니다.
- [ ] 실패 시 단계와 원인이 기록됩니다.

## Open Questions

- Accepted polling-first ingestion의 rate-limit budget과 production scaling policy
- always-available server의 hosting 위치와 stable supervisor로 systemd/Docker 중 무엇을 선택할지
- self-hosted Claude Code worker의 인증 방식과 availability 확인 정책
- Codex Cloud secondary fallback을 자동화할지 사람 배정으로만 둘지
- Atlas의 초기 개발 언어와 Control Plane 배포 위치
- network egress, secret scanning, branch protection의 구체적인 policy

문서 원본, 초기 intake channel, primary automated Executor 결정은 각각 ADR-001, ADR-002, ADR-003에서 Accepted됐습니다. Polling-first ingestion(ADR-008), 구현 언어(ADR-011), operational state store(ADR-012)도 Accepted입니다. tmux PoC supervision과 Task execution isolation은 ADR-009~010의 Proposed 방향이며 구현 근거로 승인되지 않았습니다.
