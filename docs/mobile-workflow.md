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

현재 `/atlas` command, `atlas:queued` label trigger, 자동 상태 comment, worker notification은 동작하지 않습니다.

## Target MVP Workflow

1. 휴대전화에서 기존 Atlas Task Issue Form으로 GitHub Issue를 생성합니다.
2. Atlas worker가 구조화 정보를 검증하고 Task를 idempotent하게 claim합니다.
3. self-hosted Claude Code worker가 always-available server의 격리 workspace와 branch에서 실행합니다.
4. Validator가 project test, lint, scope, forbidden path, secret 검사를 수행합니다.
5. Delivery Adapter가 진행 상태를 Issue에 기록하고 PR을 생성합니다.
6. 사용자가 PR에서 승인, 수정 요청, 폐기를 선택합니다.

Codex Cloud는 사람이 직접 전달하는 manual executor 또는 secondary 경로이며 Target MVP primary path가 아닙니다.

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

## UX Subtasks

- [x] GitHub Issue Template 작성
- [ ] Task parsing 규칙 작성
- [ ] 상태 라벨 정의
- [ ] Issue comment 진행 로그 포맷 작성
- [ ] 모바일용 PR 요약 템플릿 작성
- [x] 수정 요청, 취소, 상태 명령의 Target MVP 계약 작성
- [ ] command와 label automation 구현 여부 및 권한 확정
- [x] 초기 알림·검토 channel을 GitHub Issue/PR로 결정
- [ ] 휴대전화 기준 E2E 사용성 테스트

## Future UX

- 음성 Task 입력
- 여러 Project 사이의 빠른 전환
- 비용/사용량 확인
- 실행 계획 승인 화면
- Diff 핵심만 보여주는 모바일 리뷰
