# Existing Systems Research Plan

> 상태: 조사 계획
>
> 출처: [07. Existing Systems Research Plan](https://app.notion.com/p/3cd9f036b3078134a5b6fa7fbb928b3d) (2026-08-31 동기화)

> 현재 실행 전략: [ADR-003](../adr/0003-initial-execution-environment.md)에 따라 self-hosted Claude Code worker가 primary automated executor이고 Codex Cloud는 manual/secondary입니다. 아래 항목은 남은 Build/Adopt 검증 계획입니다.

## Research Objective

이미 해결된 실행·코딩 기능은 재구현하지 않고, Atlas의 차별 영역인 프로젝트 컨텍스트, 멀티 Executor 라우팅, 사용량 인식, 모바일 운영에 집중합니다.

## Evaluation Criteria

- GitHub Issue → PR 지원
- 로컬/클라우드 실행
- 멀티 모델 지원
- 샌드박스와 권한
- 컨텍스트 구성 방식
- 장기 기억
- API/SDK 확장성
- 모바일 원격 운영
- 라이선스와 self-host 가능성

## Systems to Evaluate

### OpenHands

Agent SDK, sandbox, remote execution, event model을 검토합니다. Atlas가 직접 Executor를 만드는 대신 Adapter로 활용할 수 있는지 확인합니다.

### SWE-agent

Issue 해결 루프, agent-computer interface, benchmark 방식을 검토합니다.

### Aider

repository map, git-native workflow, 편집 방식을 검토합니다.

### Codex Cloud

manual/secondary Task 실행, GitHub PR 전달, 사용량 제약, fallback 연동 가능 범위를 검토합니다. primary automated path로 평가하지 않습니다.

### Claude Code

always-available server의 self-hosted worker를 전제로 invocation, hooks, Git worktree, timeout/cancel, 인증, 구독 사용량, process supervision을 검토합니다.

### OpenCode / Goose / Roo Code

provider-neutral CLI와 자동화 API 가능성을 검토합니다.

### LangGraph / Temporal

durable workflow와 상태 복구를 직접 구현하지 않고 활용할 가치가 있는지 검토합니다.

### MCP

tool 연결 표준으로 사용하되 MVP에 반드시 필요한지 구분합니다.

## Build vs Adopt Hypothesis

| 영역 | 초기 판단 | 이유 |
| --- | --- | --- |
| 코딩 실행기 | Adopt | Codex, Claude Code, OpenHands 활용 |
| GitHub API | Adopt | 표준 API와 App 사용 |
| Workflow engine | Evaluate | MVP는 단순 상태 머신, 이후 Temporal 검토 |
| Context Builder | Build | Atlas 핵심 차별점 |
| Router/Scheduler | Build | 구독 가용성·역할 기반 배정 |
| Vector memory | Defer | MVP에 과도함 |
| Mobile UI | Adopt | GitHub Issues와 기존 Atlas Task Issue Form 사용 |

이 표는 조사 전 가설이며 최종 Build/Adopt 결정이 아닙니다.

## Research Deliverables

각 시스템별로 다음을 작성합니다.

- 해결하는 문제
- 핵심 아키텍처
- Atlas에서 재사용 가능한 부분
- 제약과 위험
- 라이선스
- 1시간 이내 최소 PoC 결과
- Adopt / Integrate / Reference / Reject 결정

## Research Subtasks

- [ ] OpenHands 기술 문서와 저장소 분석
- [ ] SWE-agent architecture 분석
- [ ] Aider repo map 방식 분석
- [ ] Codex Cloud manual/secondary PR 생성 실험
- [ ] self-hosted Claude Code worker Issue → PR 실험
- [ ] OpenCode/Goose 비교
- [ ] LangGraph와 Temporal 비교
- [ ] MCP 도입 시점 결정
- [ ] 결과 비교표 완성
- [x] ADR: Initial Execution Strategy Accepted
