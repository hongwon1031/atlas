# Mobile Workflow & UX

> 출처: [05. Mobile Workflow & UX](https://app.notion.com/p/3cd9f036b30781d08a3bf1186276baba) (2026-08-31 동기화)

## UX Goal

사용자는 PC를 열지 않고도 Task 생성, 상태 확인, 수정 요청, 승인 판단을 수행할 수 있어야 합니다.

## MVP Input Channel Options

| 채널 | 장점 | 단점 | 판단 |
| --- | --- | --- | --- |
| GitHub Issue | 구현 간단, 감사 기록 | 입력 UX가 다소 개발자 중심 | MVP 유력 |
| Telegram Bot | 모바일 UX 우수, 명령·알림 편리 | Bot 운영과 보안 필요 | 후보 |
| 전용 Web UI | 구조화 입력과 대시보드 | 초기 개발량 큼 | v0.2 이후 |
| Notion | 문서와 Task 연결 | 실시간 실행 인터페이스로 부적합 | 설계·상태용 |

## Recommended MVP Flow

1. 휴대전화에서 GitHub Issue Template으로 Task를 생성합니다.
2. Atlas가 구조화된 정보를 검증합니다.
3. `atlas:queued` 라벨 또는 명령으로 실행을 승인합니다.
4. 진행 상태를 Issue comment로 기록합니다.
5. PR 생성 후 모바일 알림을 보냅니다.
6. 사용자가 PR에서 승인하거나 수정을 요청합니다.

이 흐름은 권고안이며 첫 입력 채널은 아직 미결정입니다(ADR-002).

## Task Template

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

- Task 접수
- 사용자 질문 필요
- 장시간 실행 또는 비용 한도 접근
- PR 생성
- 실패와 재시도 소진
- 승인 후 완료

## UX Subtasks

- [ ] GitHub Issue Template 작성
- [ ] Task parsing 규칙 작성
- [ ] 상태 라벨 정의
- [ ] Issue comment 진행 로그 포맷 작성
- [ ] 모바일용 PR 요약 템플릿 작성
- [ ] 수정 요청 명령 설계
- [ ] 취소와 중단 명령 설계
- [ ] 알림 채널 결정
- [ ] 휴대전화 기준 E2E 사용성 테스트

## Future UX

- 음성 Task 입력
- 여러 Project 사이의 빠른 전환
- 비용/사용량 확인
- 실행 계획 승인 화면
- Diff 핵심만 보여주는 모바일 리뷰
