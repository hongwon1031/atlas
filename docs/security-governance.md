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

개인과 회사 Workspace는 별도 authentication profile 또는 worker registration, 저장소 허용 목록, Runner, usage record, Memory Store를 사용합니다. 한 profile의 credential이나 usage 상태를 다른 Workspace에 fallback으로 사용하지 않습니다.

### Project Boundary

Runner는 지정 저장소와 작업 디렉터리만 mount합니다. repository와 Project별 credential scope를 분리하고 Task마다 전용 worktree 또는 clone, branch, process, log scope를 사용합니다.

### Credential Boundary

GitHub와 Executor credential은 repository, Issue, PR, log, event에 저장하지 않습니다. 각 Adapter에 필요한 최소 token만 process environment 또는 service identity가 읽을 수 있는 host-local credential file로 주입합니다. credential file은 source tree와 worktree 밖에 두고 권한을 제한하며 profile reference만 registry에 기록합니다.

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
- Task/Run claim lease와 branch lock
- Run별 process, stdout/stderr, artifact scope
- cancel, retry, worker restart recovery와 orphan cleanup

## Worker Server Boundary

- Atlas worker는 가능한 경우 dedicated OS user로 실행합니다.
- GitHub 권한은 허용 repository의 Issue metadata 읽기, Task branch push, Issue/PR comment, PR 생성에 필요한 최소 범위로 제한합니다.
- `main` direct write, merge, repository administration, secret management 권한은 worker에 제공하지 않습니다.
- personal/company profile, Project, repository마다 credential scope와 allowlist를 명시합니다.
- server address, OS account, token, private repository, 내부 network topology를 public repository에 기록하지 않습니다.
- PoC의 tmux socket과 session은 service identity와 승인된 operator만 접근하며, tmux scrollback도 log redaction 범위로 취급합니다.

stable operation의 systemd 또는 Docker 설정은 별도 승인·구현 Task이며 이 문서는 provisioning configuration을 제공하지 않습니다.

## Command, Path, and Process Restrictions

- Issue, comment, prompt의 문자열을 shell command, branch, path에 직접 보간하지 않습니다.
- command allow/deny policy와 Task allowed/forbidden operations를 함께 적용합니다.
- worktree/clone의 resolved path가 Project별 worker root 아래인지 확인하고 path traversal과 symlink escape를 거부합니다.
- 한 Task마다 새 executor process를 시작하고 이전 conversation, shell, environment를 재사용하지 않습니다.
- 여러 Task가 mutable worktree를 공유하거나 여러 Run이 같은 branch를 동시에 수정하지 않습니다.
- timeout 또는 cancel 시 child process까지 종료하고 cleanup 결과를 audit event로 남깁니다.

## Logging and Redaction

다음 모든 출력에서 secret, token, cookie, authorization header, 개인 정보, private repository detail, provider raw authentication error를 redact합니다.

- worker와 executor stdout/stderr
- structured error와 audit log
- GitHub Issue comment와 mobile result summary
- Pull Request 제목과 설명
- 저장된 GitHub event와 validation artifact

redaction 실패 또는 secret 탐지는 Run과 PR delivery를 중단하는 policy violation입니다.

## Temporary Resource Cleanup

- success, failure, timeout, cancel별로 process, worktree/clone, temporary file, credential material, artifact cleanup 결과를 기록합니다.
- stale lease를 회수하기 전에 heartbeat, process identity, branch ownership을 확인합니다.
- orphan process와 stale worktree는 resolved Project root와 Run ownership을 검증한 뒤에만 정리합니다.
- failed artifact는 정해진 retention 동안 redacted 형태로 보존하고 만료 후 삭제합니다.
- cleanup 실패를 숨기지 않고 operator action이 필요한 recovery 상태로 보고합니다.

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
- [ ] dedicated OS user와 최소 GitHub permission matrix 검증
- [ ] environment/credential file injection과 회수 절차 정의
- [ ] Run별 stdout/stderr redaction acceptance test 작성
- [ ] stale lease, orphan process, stale worktree recovery test 작성
