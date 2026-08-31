# Context & Memory Design

> 출처: [03. Context & Memory Design](https://app.notion.com/p/3cd9f036b30781f9a62ff57fd1cf340e) (2026-08-31 동기화)

## Purpose

세션의 대화 기록에 의존하지 않고, 매 작업에 필요한 최소한의 정확한 프로젝트 컨텍스트를 재구성합니다.

## Context Layers

1. **Global Policy** — 보안, 승인, 금지 행동
2. **Workspace Policy** — 개인/회사 격리, 자격증명 범위
3. **Project Identity** — 목표, 기술 스택, 저장소, 코딩 규칙
4. **Task Context** — 목표, 제약, 완료 조건
5. **Retrieved Evidence** — 관련 코드, ADR, Issue, PR, 테스트
6. **Runtime Feedback** — 명령 출력, 테스트 실패, diff

## Project Context Manifest

```yaml
project_id: atlas
repository: hongwon1031/atlas
default_branch: main
required_docs:
  - README.md
  - docs/vision.md
  - docs/prd.md
  - docs/architecture.md
optional_sources:
  - docs/adr/**
  - docs/research/**
forbidden_paths:
  - .env
  - secrets/**
validation_commands:
  - pytest
  - ruff check .
context_budget:
  max_tokens: 60000
```

이 Manifest는 설계 예시입니다. 실제 구현 전 명령과 schema를 프로젝트 상태에 맞게 확정해야 합니다.

## Retrieval Strategy for MVP

- 의미 검색보다 명시적 Manifest와 경로 규칙을 먼저 사용합니다.
- Task 키워드와 파일명·심볼 검색으로 후보를 생성합니다.
- 최근 변경 파일과 관련 테스트를 우선합니다.
- 문서는 전체가 아니라 관련 heading 또는 범위를 포함합니다.
- 포함된 모든 항목에 출처와 선택 이유를 붙입니다.

## Context Packet

```text
Task Summary
Constraints
Acceptance Criteria
Applicable Policies
Project Overview
Relevant Decisions
Relevant Files
Relevant Tests
Recent Related Changes
Known Risks
Expected Output Contract
```

## Memory Types

### Canonical Memory

merge된 GitHub Markdown의 Constitution, PRD, Architecture, spec, Accepted ADR, 정책입니다. 가장 높은 신뢰도를 가집니다. Notion은 선택적인 human-friendly mirror이며 GitHub와 다를 때 canonical memory를 덮어쓸 수 없습니다.

### Episodic Memory

Task 실행 결과, 실패, 해결 과정, 비용, 소요 시간입니다.

### Working Memory

현재 Run에서만 사용하는 로그와 중간 산출물입니다.

Working Memory는 Run별 log와 artifact scope 안에 격리합니다. 여러 Task나 Project가 executor conversation, shell state, mutable worktree를 공유하지 않으며 Run 종료 후 [Execution Runtime](specs/execution-runtime.md)의 retention과 cleanup 규칙을 적용합니다.

### Agent-Specific Notes

특정 역할의 경험 요약입니다. Canonical Memory를 덮어쓸 수 없습니다.

## Memory Write Policy

- 자동 생성된 메모리는 `proposed` 상태로 저장합니다.
- 반복적으로 유용하거나 사람이 승인한 내용만 canonical 문서로 승격합니다.
- 비밀정보, 원문 로그 전체, 개인 데이터는 장기 기억에 저장하지 않습니다.

## Context Quality Metrics

- Relevant file recall
- 불필요한 토큰 비율
- 잘못된 Project 혼입 건수
- 작업 후 추가 컨텍스트 요청 횟수
- 오래된 결정 사용 건수

## Subtasks

- [ ] Context Manifest JSON Schema 작성
- [ ] 필수 문서 로더 구현
- [ ] 파일·심볼 검색 후보 생성기 구현
- [ ] 토큰 예산 계산기 구현
- [ ] 우선순위 기반 Context Packager 구현
- [ ] 출처·선택 이유 메타데이터 추가
- [ ] Project boundary 테스트 작성
- [ ] Secret path 필터 작성
- [ ] Task 종료 요약 생성 규칙 작성
- [ ] Memory 승격 승인 흐름 설계

## Deferred

- Vector DB
- 의미 기반 장기 기억
- 자동 지식 그래프
- 다중 저장소 cross-project retrieval

장기 Project의 goal, roadmap, Epic, Task, 반복 PR 관계는 [Project Lifecycle Specification](specs/project-lifecycle.md)을 따릅니다. AI Trading은 예시 onboarding target일 뿐 Atlas memory에 구현된 기능이 아닙니다.
