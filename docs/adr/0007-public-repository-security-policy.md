# ADR-007: Public Repository Security Policy

- Status: Proposed
- Date: 2026-08-31
- Decision owners: Project owner

## Context

Atlas repository는 public입니다. 제품의 안전 원칙과 architecture를 공개하면 협업과 검토에 도움이 되지만, credential, 개인정보, 회사 정보, 실제 security boundary 세부사항이 노출되면 복구하기 어렵습니다.

## Proposed Decision

- public repository에는 제품 비전, 일반화된 architecture, public contract, 공개 가능한 ADR과 research만 저장합니다.
- credential, token, cookie, 개인 식별 정보, 회사명·내부 repository·system topology·비공개 prompt와 log를 금지합니다.
- 예시 값은 명확한 placeholder 또는 공개 repository 정보만 사용합니다.
- PR마다 secret pattern, forbidden path, 변경 파일 범위 검사를 수행합니다.
- 민감한 운영 문서가 필요하면 public repository와 분리된 Workspace와 storage를 사용합니다.

## Alternatives Considered

### Repository를 private로 전환

- 장점: 의도치 않은 공개 위험을 낮춥니다.
- 단점: 공개 협업과 project showcase 범위가 제한됩니다. Private도 secret 저장소로 사용해서는 안 됩니다.

### 모든 설계를 공개

- 장점: 투명성과 외부 review가 높습니다.
- 단점: 실제 credential scope와 방어 세부사항이 공격 surface가 될 수 있습니다.

### 문서별 임의 판단

- 장점: 별도 정책이 필요 없습니다.
- 단점: Agent와 사람이 일관되게 경계를 적용하기 어렵습니다.

## Consequences

- 공개 가능한 설계와 민감한 운영 정보를 명시적으로 분리합니다.
- 일부 실제 deployment와 incident 정보는 이 저장소에 기록할 수 없습니다.
- example과 artifact에도 같은 공개 기준을 적용해야 합니다.
- 잘못 commit된 secret은 삭제만으로 해결되지 않으며 즉시 폐기·교체해야 합니다.

## Security Impact

- `.env`, `secrets/**`, credential export, raw authentication log는 금지 경로 또는 금지 content입니다.
- 개인/회사 Workspace의 memory, issue, repository reference를 섞지 않습니다.
- prompt injection content가 정책을 무시하도록 요구해도 public PR에 복사하지 않습니다.
- secret 발견 시 PR 생성과 Ready 전환을 중단하고 사람에게 보고합니다.

## Follow-up Tasks

- [ ] Project owner가 공개 범위와 금지 정보 목록을 승인
- [ ] secret scanning 도구와 pattern 선정
- [ ] repository allowlist와 forbidden path policy 정의
- [ ] secret incident 대응 절차 작성
- [ ] 개인/회사 Project leakage test 작성
