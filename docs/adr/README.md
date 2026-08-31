# Architecture Decision Records

> 출처: [09. ADR Register & Open Decisions](https://app.notion.com/p/3cd9f036b3078113b5dfcd6e5696608c) (2026-08-31 동기화)

중요한 기술·제품·운영 결정은 이 디렉터리에 개별 ADR로 기록합니다. 아래 항목은 아직 개별 ADR 파일로 확정되지 않은 초기 제안과 미해결 결정입니다.

## ADR Format

```text
Title
Status: Proposed | Accepted | Superseded | Rejected
Context
Decision
Alternatives Considered
Consequences
Security Impact
Follow-up Tasks
```

권장 파일명은 `NNNN-short-title.md`입니다. 결정 전에는 `Proposed`, 사람의 검토와 승인을 받은 뒤에만 `Accepted`로 변경합니다.

## Initial ADR Queue

### ADR-001: Notion-first vs GitHub-first Documentation

**Status:** Proposed

- 현재 설계는 Notion에서 작성 중입니다.
- 구현 시 AI 접근성과 version control을 위해 GitHub Markdown이 필요합니다.
- 후보: Notion 원본 + 수동 export / GitHub 원본 + Notion mirror

### ADR-002: Initial Mobile Task Channel

**Status:** Proposed

- GitHub Issue, Telegram, 전용 Web UI 비교
- 권고: GitHub Issue로 E2E 검증 후 전용 UI

### ADR-003: Initial Execution Environment

**Status:** Proposed

- Codex Cloud, Home PC Runner, VPS Runner 비교
- 권고: Codex Cloud PoC 우선, Adapter interface 유지

### ADR-004: Workflow Engine

**Status:** Proposed

- 직접 상태 머신, LangGraph, Temporal 비교
- 권고: MVP는 단순 persistence 상태 머신

### ADR-005: Project Memory Storage

**Status:** Proposed

- Git Markdown, relational DB, vector DB 비교
- 권고: Canonical은 Git Markdown, 운영 상태는 DB, vector DB는 연기

### ADR-006: Agent Availability & Usage

**Status:** Proposed

- 공식 API가 없을 때 수동 상태와 오류 기반 추정을 사용하는 방안

### ADR-007: Public Repository Security Policy

**Status:** Proposed

- Atlas 저장소가 Public이므로 개인 정보, credential, 회사 정보 금지

## Decision Checklist

- [ ] 결정이 MVP 성공에 꼭 필요한가?
- [ ] 되돌릴 수 있는가?
- [ ] 특정 공급자 종속을 만드는가?
- [ ] 보안 경계에 영향을 주는가?
- [ ] 운영 비용을 증가시키는가?
- [ ] 대안과 기각 이유가 기록됐는가?

## Open Decisions Requiring User Input

1. 첫 모바일 입력 채널
2. 첫 Executor
3. Atlas의 초기 개발 언어
4. Control Plane의 배포 위치
5. Notion/GitHub 문서 원본 정책
6. 개인 Claude 계정의 Atlas 사용 허용 기준
7. 공개 저장소에서 공개할 설계 범위
