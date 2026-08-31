# Atlas — AI Workforce OS

Atlas는 사람이 휴대전화에서 업무를 지시하면 여러 AI 개발 에이전트가 올바른 프로젝트 컨텍스트를 불러오고, 격리된 환경에서 작업하고, 검증 가능한 결과와 Pull Request를 생성하도록 조율하는 AI Workforce Operating System입니다.

> **현재 작업 단계:** Sprint 1 — AI Development Foundation
>
> 이 저장소는 현재 제품 정의, 실행 계약, 기여 거버넌스만 포함합니다. 프로덕션 코드는 아직 구현하지 않습니다.

## 핵심 MVP

휴대전화에서 작업을 지시하면 원격 실행 환경의 에이전트가 올바른 프로젝트 컨텍스트를 사용하여 별도 브랜치에서 작업하고, 테스트 결과가 포함된 Pull Request를 생성합니다. `main` 반영은 항상 사람의 승인을 거칩니다.

## Getting Started

Atlas에 기여하는 사람과 AI Agent는 application stack을 먼저 만들지 않고 문서와 Task contract에서 시작합니다.

1. 저장소 전체 작업 규칙인 [AGENTS.md](AGENTS.md)를 읽습니다.
2. [Constitution](docs/constitution.md), [PRD](docs/prd.md), [Architecture](docs/architecture.md), [Security & Governance](docs/security-governance.md)를 순서대로 읽습니다.
3. [Task Schema](docs/specs/task-schema.md)에 맞춰 [Atlas Task Issue Form](.github/ISSUE_TEMPLATE/atlas-task.yml)으로 Objective, Constraints, Acceptance Criteria, Allowed Scope를 작성합니다.
4. 적용되는 [ADR](docs/adr/README.md)의 상태를 확인합니다. `Proposed`는 확정된 구현 근거가 아닙니다.
5. Task 전용 branch에서 작업하고 [PR template](.github/pull_request_template.md)에 변경 파일, 검증, 위험, open question을 기록합니다.
6. 사람의 검토와 승인 전에는 `main`에 merge하지 않습니다.

현재 단계에서는 별도 설치나 실행 명령이 없습니다. 언어, framework, database, deployment 환경은 Accepted ADR과 명시적인 구현 Task 없이 선택하지 않습니다.

## 왜 Atlas인가

- AI 코딩 세션이 끝나도 프로젝트의 중요한 맥락을 보존합니다.
- 여러 모델과 실행기의 중복 작업과 충돌을 조율합니다.
- PC를 열지 않고도 휴대전화에서 작업 지시와 결과 검토를 수행합니다.
- 모델별 가용성, 비용, 사용량과 역할 적합성을 라우팅에 반영합니다.
- 프로젝트와 Workspace 사이의 코드, 문서, 자격증명, 기억을 격리합니다.
- 계획, 실행, 로그, 테스트, 비용, 결과를 관찰 가능하게 만듭니다.

## MVP 사용자 흐름

1. 사용자가 휴대전화에서 Project와 작업 내용을 선택합니다.
2. Atlas가 Task를 정규화하고 위험도와 필요한 역량을 분류합니다.
3. Context Builder가 정책, 관련 코드, 최근 Issue/PR, 테스트 정보를 수집합니다.
4. Router가 사용 가능한 Executor를 선택합니다.
5. Runner가 격리된 브랜치 또는 worktree에서 작업합니다.
6. Validator가 테스트, lint, 변경 범위, 금지 파일, 비밀정보를 검사합니다.
7. Atlas가 PR과 모바일용 결과 요약을 생성합니다.
8. 사용자가 승인, 수정 요청, 폐기를 선택합니다.

## 핵심 문서

- [제품 비전](docs/vision.md)
- [프로젝트 헌법](docs/constitution.md)
- [PRD v0.1](docs/prd.md)
- [시스템 아키텍처 v0.1](docs/architecture.md)
- [컨텍스트와 메모리 설계](docs/context-memory.md)
- [에이전트 역할, 라우터와 스케줄러](docs/agents-router-scheduler.md)
- [모바일 워크플로와 UX](docs/mobile-workflow.md)
- [보안, 격리와 거버넌스](docs/security-governance.md)
- [기존 시스템 조사 계획](docs/research/existing-systems.md)
- [로드맵, Epic과 백로그](docs/roadmap.md)
- [ADR 등록부와 미해결 결정](docs/adr/README.md)

### 실행 계약

- [Task Schema](docs/specs/task-schema.md)
- [Task State Machine](docs/specs/task-state-machine.md)
- [GitHub Issue Command Contract](docs/specs/issue-command-contract.md)
- [Pull Request Output Contract](docs/specs/pr-output-contract.md)

## Repository Structure

```text
atlas/
├── AGENTS.md                         # AI Agent 작업·리뷰·보안 규칙
├── README.md                         # 프로젝트 진입점과 문서 지도
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   └── atlas-task.yml            # 구조화된 Atlas Task 입력
│   └── pull_request_template.md      # PR 결과와 검증 보고 형식
└── docs/
    ├── constitution.md               # 저장소의 최상위 운영 원칙
    ├── prd.md                        # 목표, 범위, 요구사항, 성공 조건
    ├── architecture.md               # 컴포넌트, 데이터, 상태 개요
    ├── context-memory.md             # 컨텍스트 packet과 memory 정책
    ├── agents-router-scheduler.md    # 역할, capability, routing 정책
    ├── mobile-workflow.md            # 모바일 Task와 review 흐름
    ├── security-governance.md        # 격리, 권한, 승인, 위협 모델
    ├── specs/                         # 구현과 channel이 따라야 할 정규 계약
    ├── adr/                           # 결정, 대안, 영향, 후속 작업
    └── research/                      # 외부 시스템 조사와 Build/Adopt 근거
```

`docs/specs/`를 추가해 실행 가능한 계약을 제품 방향 문서와 분리했고, 기존 문서 경로는 이동하지 않아 링크 호환성을 유지합니다. ADR placeholder는 인덱스를 유지한 채 `docs/adr/0001-*.md`부터 `0007-*.md`까지 독립 문서로 확장했습니다.

## 설계 원칙

- **Context First:** 모델보다 올바른 컨텍스트 선별이 우선입니다.
- **Project Memory:** 기억은 대화 세션이 아니라 프로젝트 저장소에 남습니다.
- **Human Approval:** 위험한 변경과 최종 반영은 사람이 승인합니다.
- **Vendor Neutrality:** Codex, Claude Code, OpenHands 등 실행기를 교체할 수 있어야 합니다.
- **Observable Work:** 계획, 실행, 로그, 테스트, 비용, 결과를 추적합니다.
- **Least Privilege:** 역할과 작업에 필요한 최소 권한만 부여합니다.

## Sprint 0 완료 조건

- [ ] Vision & Constitution 승인
- [ ] PRD v0.1 승인
- [ ] Architecture v0.1 승인
- [ ] 레퍼런스 조사와 Build/Adopt 결정
- [ ] MVP Epic과 세부 Task 확정
- [ ] Codex Cloud 또는 원격 Runner를 통한 첫 PR 경로 검증

| 영역 | 상태 | 완료 기준 |
| --- | --- | --- |
| 제품 정의 | 작성 중 | 목표·비목표·사용자 흐름 승인 |
| 기술 조사 | 작성 중 | 재사용 대상과 직접 구현 범위 결정 |
| 아키텍처 | 초안 | MVP 컴포넌트와 데이터 흐름 확정 |
| 실행 환경 | 미검증 | 폰 지시 → PR 생성 E2E 성공 |

## 문서 운영

- 중요한 기술 결정은 [ADR](docs/adr/README.md)로 남깁니다.
- 모든 변경은 독립 브랜치와 Pull Request로 제출합니다.
- 완료 주장은 자동 테스트 또는 명시적인 검증 근거를 포함해야 합니다.
- Notion/GitHub 중 어느 쪽을 문서 원본으로 삼을지는 아직 미결정입니다(ADR-001).

이 초기 문서 세트는 [Atlas Notion 문서](https://app.notion.com/p/3cd9f036b307814f888fe2fb827a230a?pvs=204)를 2026-08-31 기준으로 옮긴 것입니다.
