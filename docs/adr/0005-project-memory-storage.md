# ADR-005: Project Memory Storage

- Status: Proposed
- Date: 2026-08-31
- Decision owners: Project owner

## Context

Atlas는 session 대화가 사라져도 승인된 제품 정의, 정책, 결정, Task 결과를 다음 Agent가 사용할 수 있어야 합니다. Canonical memory, operational state, episodic memory는 변경 빈도와 신뢰 수준이 다릅니다.

후보는 Git Markdown, relational database, vector database입니다.

## Proposed Decision

- 사람이 승인한 Constitution, PRD, Architecture, spec, ADR은 Git Markdown에 canonical memory로 저장합니다.
- Task, Run, event, availability 같은 운영 상태는 향후 relational database에 저장합니다.
- Task 결과와 실패 요약은 source artifact를 참조하는 episodic record로 관리합니다.
- vector database와 의미 기반 장기 기억은 retrieval quality 요구가 입증될 때까지 연기합니다.
- 자동 생성 memory는 `proposed`이며 사람 승인 없이 canonical 문서를 덮어쓰지 않습니다.

## Alternatives Considered

### 모든 memory를 Git에 저장

- 장점: version control과 review가 단순합니다.
- 단점: 빈번한 Run event, lease, query에 부적합합니다.

### 모든 memory를 relational database에 저장

- 장점: query와 state update가 쉽습니다.
- 단점: 문서 review, diff, repository context와 분리됩니다.

### Vector database first

- 장점: 의미 검색과 대규모 recall에 유리할 수 있습니다.
- 단점: stale chunk, access boundary, 비용, 평가 복잡성이 MVP에 과도합니다.

## Consequences

- 승인된 설계와 code가 같은 PR workflow를 사용합니다.
- 운영 상태 storage 기술은 아직 열린 구현 결정입니다.
- retrieval MVP는 manifest, path, keyword, symbol 규칙을 우선합니다.
- Git 문서와 운영 record 사이의 안정적인 reference가 필요합니다.

## Security Impact

- Workspace와 Project마다 memory namespace와 credential을 분리합니다.
- raw log, secret, 개인정보를 장기 memory에 저장하지 않습니다.
- vector retrieval을 도입할 때 Project boundary와 삭제 정책을 먼저 검증합니다.
- canonical 승격은 audit 가능한 사람 승인을 요구합니다.

## Follow-up Tasks

- [ ] Project owner가 계층별 storage 제안을 승인
- [ ] canonical/proposed/episodic record 식별 규칙 정의
- [ ] Git revision과 Task/Run record 연결 방식 정의
- [ ] retention과 redaction 정책 작성
