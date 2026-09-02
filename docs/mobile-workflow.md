# Mobile Workflow & UX

> 출처: [05. Mobile Workflow & UX](https://app.notion.com/p/3cd9f036b30781d08a3bf1186276baba) (2026-08-31 동기화)

> Intake 결정: [ADR-002](adr/0002-initial-mobile-task-channel.md) Accepted

## UX Goal

사용자는 PC를 열지 않고도 Task 생성, 상태 확인, 수정 요청, 승인 판단을 수행할 수 있어야 합니다.

## MVP Input Channel Options

| 채널 | 장점 | 단점 | 판단 |
| --- | --- | --- | --- |
| GitHub Issue | 구현 간단, 감사 기록 | 입력 UX가 다소 개발자 중심 | **Accepted initial channel** |
| Telegram Bot | 모바일 UX 우수, 명령·알림 편리 | Bot 운영과 보안 필요 | 후보 |
| 전용 Web UI | 구조화 입력과 대시보드 | 초기 개발량 큼 | v0.2 이후 |
| Notion | 문서와 Task 연결 | 실시간 실행 인터페이스로 부적합 | 선택적 human-friendly mirror |

## Current Manual Workflow

1. 휴대전화에서 기존 Atlas Task Issue Form으로 GitHub Issue를 생성합니다.
2. 사람이 Project, Objective, Constraints, Acceptance Criteria, Scope를 검토합니다.
3. 사람이 Issue를 선택한 Executor에게 전달합니다.
4. Executor가 branch를 만들고 작업·검증한 뒤 PR을 생성합니다.
5. 사용자가 PR에서 승인, 수정 요청, 폐기를 선택합니다.

`atlas:queued` label을 붙이면 poller가 Task로 등록하고 claim 대상으로 삼습니다. `/atlas` command, 자동 상태 comment, worker notification은 동작하지 않습니다.

## Target MVP Workflow

1. 휴대전화에서 기존 Atlas Task Issue Form으로 GitHub Issue를 생성합니다.
2. polling worker가 `atlas:queued` label이 붙은 Issue를 찾고 구조화 정보를 parse·validate합니다. (구현됨)
3. Planner가 plan과 risk를 분류하고 Context Builder가 Project context를 구성합니다.
4. Router가 Executor를 선택하고 worker가 Task를 lease로 idempotent하게 claim합니다.
5. worker가 전용 worktree/clone과 branch를 만들고 Task별 새 executor process를 시작합니다.
6. self-hosted Claude Code Executor가 always-available server에서 실행합니다.
7. Validator가 project test, lint, scope, forbidden path, secret 검사를 수행합니다.
8. Delivery Adapter가 진행 상태, mobile result summary, PR을 생성합니다.
9. 사용자가 승인, 수정 요청, 취소 중 하나를 선택하고 merge를 결정합니다.

Codex Cloud는 사람이 직접 전달하는 manual executor 또는 secondary 경로이며 Target MVP primary path가 아닙니다.

[ADR-008](adr/0008-initial-github-event-ingestion.md)의 polling-first는 `Accepted`이며 polling과 claim은 동작합니다. `atlas:queued` label을 붙인 Issue만 후보가 됩니다. [ADR-009](adr/0009-worker-process-supervision.md)의 tmux PoC는 `Proposed`이고, Claude Code invocation, 자동 comment와 notification은 아직 동작하지 않습니다.

## Canonical Task Input

정규 입력 UI는 [Atlas Task Issue Form](../.github/ISSUE_TEMPLATE/atlas-task.yml)입니다. 아래 Markdown은 다른 channel adapter가 같은 의미를 제공할 때 참고하는 최소 형태입니다.

```markdown
## Objective

## Project

## Constraints

## Acceptance Criteria
- [ ]

## Allowed Scope

## Risk / Notes
```

## Mobile Result Card

- 상태: 성공 / 검토 필요 / 실패
- 수행 Agent와 실행 시간
- 변경 요약 3줄
- 테스트 결과
- 위험 또는 확인할 사항
- PR 링크
- 승인, 수정 요청, 폐기 액션

## Notification Rules

다음은 Target MVP 규칙입니다. Current manual workflow에서는 사람과 Executor가 Issue/PR comment로 필요한 상태를 직접 기록합니다.

- Task 접수
- 사용자 질문 필요
- 장시간 실행 또는 비용 한도 접근
- PR 생성
- 실패와 재시도 소진
- 승인 후 완료

## Long-running Project UX

Project는 product goal, context, roadmap, Epic, 여러 Task와 반복 PR을 포함할 수 있습니다. Planner는 roadmap과 Task batch를 제안할 수 있지만 사람은 실행 전에 roadmap 또는 batch를 승인합니다. MVP 모바일 흐름은 한 번에 한 Task와 한 PR만 전달하며 fully autonomous planning과 승인 없는 production deployment를 제공하지 않습니다.

AI Trading은 향후 onboarding UX를 검증할 예시 Project일 뿐 Atlas에 구현된 trading 기능이 아닙니다. 정규 lifecycle은 [Project Lifecycle Specification](specs/project-lifecycle.md)을 따릅니다.

## UX Subtasks

- [x] GitHub Issue Template 작성
- [x] Task parsing과 polling ingestion contract 작성
- [ ] 상태 라벨 정의
- [ ] Issue comment 진행 로그 포맷 작성
- [ ] 모바일용 PR 요약 템플릿 작성
- [x] 수정 요청, 취소, 상태 명령의 Target MVP 계약 작성
- [ ] command와 label automation 구현 여부 및 권한 확정
- [x] 초기 알림·검토 channel을 GitHub Issue/PR로 결정
- [ ] 휴대전화 기준 E2E 사용성 테스트
- [x] approved/queued Task polling과 idempotent claim 구현
- [ ] Run 결과를 redacted mobile summary로 전달

## Future UX

- 음성 Task 입력
- 여러 Project 사이의 빠른 전환
- 비용/사용량 확인
- 실행 계획 승인 화면
- Diff 핵심만 보여주는 모바일 리뷰
