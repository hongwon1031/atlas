# ADR-002: Initial Mobile Task Channel

- Status: Proposed
- Date: 2026-08-31
- Decision owners: Project owner

## Context

Atlas의 MVP는 휴대전화에서 Task를 생성하고 상태를 확인하며 결과 PR을 검토할 수 있어야 합니다. 후보는 GitHub Issue, Telegram Bot, 전용 Web UI, Notion입니다.

Sprint 1은 GitHub Issue Form과 comment command 계약을 문서화하지만 실제 실행 automation은 구현하지 않습니다.

## Proposed Decision

- 첫 모바일 Task channel은 GitHub Issue로 시작합니다.
- `.github/ISSUE_TEMPLATE/atlas-task.yml`을 구조화된 입력 인터페이스로 사용합니다.
- 권한 있는 사용자는 [Issue Command Contract](../specs/issue-command-contract.md)에 정의된 comment와 `atlas:queued` label로 Task를 제어합니다.
- 실행 상태는 Atlas의 Task state가 canonical이며 GitHub label은 projection으로 취급합니다.
- 전용 Web UI는 Task → PR 경로가 검증된 뒤 검토합니다.

## Alternatives Considered

### Telegram Bot

- 장점: 모바일 입력과 notification 경험이 좋습니다.
- 단점: bot 운영, identity mapping, secret, command authorization이 필요합니다.

### 전용 Web UI

- 장점: schema 기반 form, timeline, approval UX를 최적화할 수 있습니다.
- 단점: 초기 application code와 인증 구현량이 큽니다.

### Notion

- 장점: 설계 문서와 Task를 연결하기 쉽습니다.
- 단점: 실시간 command와 execution audit interface로는 적합하지 않습니다.

## Consequences

- 별도 모바일 application 없이 GitHub의 인증, Issue, PR, audit timeline을 활용합니다.
- 초기 사용자는 GitHub 중심 UX를 이해해야 합니다.
- Issue Form heading과 command grammar가 public contract가 됩니다.
- 향후 channel adapter는 같은 Task Schema를 생성해야 합니다.

## Security Impact

- Public Issue의 모든 입력은 신뢰되지 않은 content로 처리합니다.
- Issue author만으로 mutation 권한을 부여하지 않고 repository permission을 다시 확인합니다.
- comment의 링크, code, prompt를 자동 실행하지 않습니다.
- secret, 개인정보, 회사 자료를 Issue에 입력하지 않는 경고를 제공합니다.

## Follow-up Tasks

- [ ] Project owner가 GitHub Issue 우선 결정을 승인
- [ ] 필요한 `atlas:*` label 목록과 provisioning 방식 확정
- [ ] Issue parser와 schema validation acceptance test 정의
- [ ] 휴대전화 기준 Issue → PR 사용성 시험
