# Security, Isolation & Governance

> 출처: [06. Security, Isolation & Governance](https://app.notion.com/p/3cd9f036b30781329676cc21d9ffce80) (2026-08-31 동기화)

## Threat Model

- 잘못된 Project 또는 Workspace의 컨텍스트 혼입
- 프롬프트 인젝션으로 금지 명령 수행
- 비밀키의 로그·PR 노출
- AI의 과도한 파일 변경 또는 삭제
- 외부 코드 실행에 따른 공급망 위험
- 승인 없는 merge·배포
- 회사 데이터가 개인 시스템으로 유출

## Security Boundaries

### Workspace Boundary

개인과 회사 Workspace는 별도 인증정보, 저장소 허용 목록, Runner, Memory Store를 사용합니다.

### Project Boundary

Runner는 지정 저장소와 작업 디렉터리만 mount합니다.

### Credential Boundary

각 Adapter에 필요한 최소 token만 제공하고 secrets manager를 사용합니다.

### Network Boundary

MVP에서는 기본 deny 또는 허용 도메인 목록을 고려합니다. 구체적인 egress 정책은 아직 결정되지 않았습니다.

## Permission Levels

1. `read_only`
2. `write_branch`
3. `open_pr`
4. `deploy_staging`
5. `production` — MVP 제외

## Mandatory Controls

- branch protection
- `main` 직접 push 금지
- secret scan
- 변경 파일 수와 diff 크기 제한
- 금지 경로 보호
- shell command allow/deny policy
- audit event log
- 실행 timeout

## Human Approval Matrix

| 변경 유형 | 자동 작업 | 사전 승인 | 최종 승인 |
| --- | --- | --- | --- |
| 문서 | 가능 | 불필요 | PR merge |
| 일반 코드 | 가능 | 불필요 | PR merge |
| 의존성 추가 | 가능 | 정책에 따라 | 필수 |
| CI·인프라 | 제한 | 필수 | 필수 |
| 비밀정보·배포 | MVP 금지 | 필수 | 필수 |

## Governance

- 모든 정책 변경은 ADR이 필요합니다.
- Agent별 권한은 역할과 분리해 설정합니다.
- 감사 로그는 Task ID, Agent, 명령, 결과, artifact checksum을 포함합니다.
- 실패 로그에 개인정보와 secret redaction을 적용합니다.

## Security Subtasks

- [ ] Workspace credential model 정의
- [ ] Repository allowlist 구현
- [ ] Forbidden path 정책 구현
- [ ] Secret scanning 도구 선정
- [ ] Command policy 설계
- [ ] Runner filesystem isolation 검증
- [ ] Network egress 정책 결정
- [ ] Branch protection 체크 구현
- [ ] Audit log schema 작성
- [ ] Prompt injection 테스트 케이스 작성
- [ ] 개인/회사 Workspace 혼입 E2E 테스트 작성
