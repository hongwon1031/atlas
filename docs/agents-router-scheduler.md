# Agent Roles, Router & Scheduler

> 출처: [04. Agent Roles, Router & Scheduler](https://app.notion.com/p/3cd9f036b30781818330ffa40507c20d) (2026-08-31 동기화)

## Role Model

역할은 모델 계정과 분리합니다. 동일한 Claude 또는 Codex 실행기가 상황에 따라 Researcher, Implementer, Reviewer 역할을 수행할 수 있습니다.

## Initial Roles

### PM / Planner

- 요구사항 정규화
- 작업 분해
- 완료 조건 확인

### Researcher

- 문서, 코드, 레퍼런스 조사
- 구현하지 않고 근거와 선택지를 제공

### Implementer

- 코드와 문서 수정
- 테스트 추가

### Reviewer

- diff, 아키텍처, 위험 요소 검토
- 구현자와 가능한 한 다른 Agent 사용

### QA / Validator

- 명령 기반 검증
- Acceptance Criteria 확인

### Reporter

- 모바일용 결과 요약

## Agent Capability Model

```yaml
capabilities:
  - code_write
  - repo_search
  - web_research
  - test_execution
  - pr_create
  - long_context
  - image_understanding
  - local_tool_access
```

## Routing Inputs

- Task 유형과 위험도
- 필요한 capability
- Executor 온라인 여부
- 사용자 입력 가용성
- 예상 비용과 잔여 사용량
- 최근 성공률
- 데이터 위치와 보안 등급

## MVP Routing Policy

1. 문서·설계 작업은 기본 PM/Research Executor에 배정합니다.
2. 코드 구현은 PR을 생성할 수 있는 Executor에 배정합니다.
3. 다른 Executor를 사용할 수 있으면 구현과 리뷰를 분리합니다.
4. 고위험 변경은 자동 실행을 금지하거나 사람 승인 후 실행합니다.
5. 사용량이 불명확하면 보수적인 수동 availability 상태를 사용합니다.

## Usage State

서비스가 공식 잔여량 API를 제공하지 않으면 다음 상태를 수동 입력이나 실패 신호로 관리합니다.

- `available`
- `limited`
- `exhausted`
- `unknown`
- `offline`

## Fallback Policy

- 1차 Executor 실패 → 동일 Executor 1회 재시도
- 인증·사용량 오류 → 다른 Adapter로 재라우팅
- 테스트 실패 → 동일 Task의 revision run
- 프로젝트 불명확 → 실행 중단 후 사용자 질문

## Anti-Patterns

- 여러 에이전트가 같은 브랜치를 동시에 수정
- Reviewer가 근거 없이 전체 구현을 다시 작성
- 사용량 절감을 위해 검증 단계를 생략
- Agent 개인 메모리가 프로젝트 정책보다 우선

## Subtasks

- [ ] Role 정의 파일 포맷 설계
- [ ] Capability taxonomy 확정
- [ ] Agent Registry 스키마 작성
- [ ] Availability 수동 입력 API 작성
- [ ] 규칙 기반 Router 구현
- [ ] 재시도·Fallback 정책 구현
- [ ] 동일 브랜치 동시 실행 Lock 구현
- [ ] Reviewer 분리 정책 구현
- [ ] 실행 결과 기반 성공률 통계 설계
- [ ] 비용·사용량 대시보드 후속 Issue 생성
