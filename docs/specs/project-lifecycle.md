# Project Lifecycle Specification v0.1

이 문서는 Atlas가 단발성 prompt가 아니라 장기 Project를 계획하고 여러 Task와 PR로 전달할 때의 승인 경계를 정의합니다. Project planning automation은 구현되지 않았습니다.

## Hierarchy

```text
Workspace
└── Project
    ├── Product goals
    ├── Project context and canonical documents
    ├── Roadmap
    ├── Epics
    └── Tasks
        └── Runs
            └── Pull Request or failure delivery
```

- Project는 하나의 repository, context boundary, credential scope를 가집니다.
- Roadmap은 목표 순서와 milestone을 설명합니다.
- Epic은 여러 Task를 묶는 결과 단위입니다.
- Task는 승인 가능한 objective, scope, Acceptance Criteria를 가진 실행 단위입니다.
- Run은 한 Task의 실행 시도이며 retry마다 새 ID를 가집니다.
- Pull Request는 한 Task의 검토 가능한 delivery를 기본으로 합니다.

## Planning and Approval

1. 사람이 Product goal과 Project boundary를 등록합니다.
2. Planner는 context를 바탕으로 roadmap, Epic, Task breakdown을 제안할 수 있습니다.
3. 사람은 roadmap 또는 명시적인 Task batch를 승인·수정·거부합니다.
4. 승인된 batch 안에서도 각 Task는 실행 전 schema, risk, scope, dependency를 검증합니다.
5. MVP는 한 번에 한 Task를 실행하고 한 PR을 전달합니다.
6. PR review 결과는 다음 Task 계획에 episodic evidence로 연결할 수 있지만 canonical context 변경은 별도 PR 승인을 거칩니다.

Planner의 제안은 자동 승인이나 실행 권한이 아닙니다. 기존 Task를 분해하거나 순서를 바꿀 때 Acceptance Criteria와 scope를 조용히 변경하지 않습니다.

## MVP Boundaries

### In Scope

- 사람이 승인한 roadmap 또는 Task batch
- Project별 context와 credential isolation
- 한 Task → 한 active Run → 한 PR 순차 delivery
- 실패, retry, revision을 이전 Run과 연결
- 사람이 merge, revision, cancel을 결정

### Out of Scope

- fully autonomous product planning
- 승인 없는 unlimited self-directed iteration
- 여러 Task의 동시 mutable execution
- production deployment without approval
- AI가 스스로 roadmap을 Accepted로 승격하는 기능

## Project Onboarding

Project onboarding은 다음 최소 정보를 요구합니다.

- Product goal과 explicit non-goals
- repository와 default branch
- Workspace, credential, execution host boundary
- canonical documentation와 required reading order
- allowed/forbidden paths와 operations
- validation commands와 delivery policy
- initial roadmap 또는 사람이 승인한 Task batch

AI Trading은 Atlas가 향후 이 lifecycle로 onboarding할 수 있는 별도 Project 예시입니다. Trading 기능, broker integration, strategy execution이 Atlas에 구현됐다는 뜻이 아닙니다.

## Progress and Memory

- canonical goal, roadmap, architecture 변경은 GitHub Markdown PR로 승인합니다.
- Task와 Run state는 향후 operational store에 보존합니다.
- 완료 PR, failure reason, validation evidence는 다음 계획의 episodic reference가 될 수 있습니다.
- 다른 Project의 context, credential, usage record를 재사용하지 않습니다.

## Open Questions

- roadmap과 Task batch approval을 GitHub에서 표현하는 방식
- Task dependency와 blocked state의 schema
- 한 Project 안에서 parallel Task를 허용할 시점과 조건
- Project archival, retention, credential revocation 절차
- Planner 제안 품질과 roadmap drift 평가 방법
