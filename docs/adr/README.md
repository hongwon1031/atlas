# Architecture Decision Records

> 출처: [09. ADR Register & Open Decisions](https://app.notion.com/p/3cd9f036b3078113b5dfcd6e5696608c) (2026-08-31 동기화)

중요한 기술·제품·운영 결정은 이 디렉터리에 개별 ADR로 기록합니다. 각 문서의 상태가 `Proposed`인 동안 권고안은 검토 대상이며, 사람의 승인 후에만 `Accepted`로 변경합니다.

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

## ADR Register

| ADR | Status | Summary |
| --- | --- | --- |
| [ADR-001: Documentation Source of Truth](0001-documentation-source-of-truth.md) | Accepted | GitHub Markdown canonical, Notion optional human-friendly mirror |
| [ADR-002: Initial Mobile Task Channel](0002-initial-mobile-task-channel.md) | Accepted | GitHub Issues와 기존 Atlas Task Issue Form |
| [ADR-003: Initial Execution Environment](0003-initial-execution-environment.md) | Accepted | self-hosted Claude Code worker primary, Codex Cloud manual/secondary |
| [ADR-004: Workflow Engine](0004-workflow-engine.md) | Proposed | 단순 persisted state machine 우선 제안 |
| [ADR-005: Project Memory Storage](0005-project-memory-storage.md) | Proposed | Git canonical 문서와 별도 운영 상태 저장 제안 |
| [ADR-006: Agent Availability and Usage](0006-agent-availability-and-usage.md) | Proposed | 수동 availability와 실패 signal 결합 제안 |
| [ADR-007: Public Repository Security Policy](0007-public-repository-security-policy.md) | Proposed | 공개 가능 설계와 민감 운영 정보 분리 제안 |

## Status Definitions

- `Proposed` — 검토 중이며 구현의 확정 근거로 사용할 수 없음
- `Accepted` — 사람이 승인했으며 후속 작업이 따라야 하는 결정
- `Superseded` — 더 최신 ADR로 대체됨; 대체 문서 링크 필요
- `Rejected` — 채택하지 않기로 결정; 이유와 대안 기록 유지

## Decision Checklist

- [ ] 결정이 MVP 성공에 꼭 필요한가?
- [ ] 되돌릴 수 있는가?
- [ ] 특정 공급자 종속을 만드는가?
- [ ] 보안 경계에 영향을 주는가?
- [ ] 운영 비용을 증가시키는가?
- [ ] 대안과 기각 이유가 기록됐는가?

## Open Decisions Requiring User Input

1. Atlas의 초기 개발 언어
2. Control Plane과 worker의 배포 위치 및 process model
3. GitHub Issue 감지를 webhook으로 할지 polling으로 할지
4. self-hosted Claude Code worker의 인증 방식과 계정 사용 기준
5. Codex Cloud secondary fallback을 자동화할지 사람 배정으로만 둘지
6. 공개 저장소에서 공개할 설계 범위
7. network egress, secret scanning, branch protection의 구체적인 정책
