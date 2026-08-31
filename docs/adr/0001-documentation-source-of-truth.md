# ADR-001: Documentation Source of Truth

- Status: Accepted
- Date: 2026-08-31
- Accepted: 2026-08-31
- Decision owners: Project owner

## Context

Atlas의 초기 제품 설계는 Notion에서 작성됐고 GitHub의 `docs/`로 동기화됐습니다. AI Agent가 안정적으로 작업하려면 문서가 source revision, branch, PR review와 연결돼야 하지만, 사람의 초기 기획과 탐색에는 Notion이 편리합니다.

두 시스템을 동시에 canonical source로 취급하면 내용 충돌, 오래된 정책 사용, 승인 상태 불명확 문제가 생깁니다.

## Decision

- 구현과 실행에 영향을 주는 Constitution, PRD, Architecture, spec, ADR은 GitHub Markdown을 canonical source로 사용합니다.
- Notion은 선택적인 human-friendly mirror, 아이디어 탐색, meeting note, 초안 작성에 사용할 수 있습니다.
- Notion의 내용은 canonical policy나 Accepted 결정이 아닙니다. 실행에 영향을 주는 변경은 GitHub Pull Request로 반영하고 merge된 GitHub Markdown을 기준으로 판단합니다.
- 각 동기화 문서는 가능한 경우 원문 링크와 마지막 동기화 기준을 남깁니다.
- 자동 양방향 동기화는 MVP 범위에서 제외합니다.
- 기존 문서의 Notion 링크는 역사적 출처와 선택적 mirror를 나타낼 뿐 source of truth를 뜻하지 않습니다.

## Alternatives Considered

### Notion canonical + 수동 GitHub export

- 장점: 기존 작성 경험을 유지합니다.
- 단점: AI가 revision과 승인 상태를 정확히 판단하기 어렵고 export drift가 생깁니다.

### GitHub canonical + Notion mirror

- 장점: code, spec, decision이 같은 review와 version control을 사용합니다.
- 단점: 비개발자 편집 경험과 rich document 기능이 제한됩니다.

### 양방향 실시간 동기화

- 장점: 두 인터페이스에서 최신 내용을 볼 수 있습니다.
- 단점: 충돌 해결과 block 변환이 복잡하며 MVP 가치보다 구현 비용이 큽니다.

## Consequences

- Agent의 문서 읽기 순서와 commit SHA 기반 재현성이 명확해집니다.
- 중요한 문서 변경은 PR review를 거칩니다.
- Notion 초안이 GitHub에 반영되지 않으면 실행 정책으로 사용되지 않습니다.
- mirror 운영 방법과 drift 확인은 별도 절차가 필요합니다.

## Security Impact

- Public repository에 반영하기 전 개인정보, credential, 회사 정보를 제거해야 합니다.
- Notion의 access level이 GitHub보다 넓거나 좁을 수 있으므로 자동 복사를 기본 허용하지 않습니다.
- Agent는 Notion 초안에서 비밀정보를 발견해도 GitHub 문서로 복사하지 않습니다.

## Follow-up Tasks

- [x] Project owner가 GitHub Markdown canonical / Notion optional mirror 결정을 승인
- [ ] canonical/mirror 상태를 문서 header에 표시하는 규칙 확정
- [ ] 수동 동기화 checklist 작성
- [ ] drift 점검 주기 결정
