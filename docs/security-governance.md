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
- Claude Code executor는 `--no-session-persistence`로 실행해 Run 사이에 대화가 이어지지 않게 합니다.
- Claude Code에 주는 도구를 `Read,Edit,Write,Glob,Grep`으로 제한합니다. shell을 주지 않으므로 executor가 임의 명령이나 git commit·push를 실행할 수단이 없습니다. 권한 모드는 `acceptEdits`까지만 엽니다.
- **이 경계는 설정으로 우회할 수 없습니다.** permission mode와 도구는 allowlist로 강제하고, 권한 우회 mode와 명령 실행 도구는 환경변수로도 거부합니다. 모르는 도구 이름도 통과시키지 않습니다. 잘못된 설정은 실행 전에 configuration error로 실패합니다.
- executor의 유효 정책은 raw config가 아니라 검증을 통과한 정규화 값으로 event에 남깁니다.
- 사용자 유래 텍스트(Issue 본문 등)는 argv가 아니라 stdin으로 전달합니다. argv는 플랫폼에 따라 shell wrapper가 다시 파싱할 수 있습니다.
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

provider의 구조화된 출력을 해석해야 할 때는 **저장본이 아니라 별도의 임시 메모리 버퍼**를 씁니다. redaction은 텍스트 치환이므로 JSON 같은 구조를 깨뜨릴 수 있고, 깨진 구조를 되살리려고 저장본의 redaction을 약화해서는 안 됩니다. 임시 버퍼는 상한이 있고 디스크나 DB에 저장되지 않으며 한 번 읽히면 즉시 폐기됩니다.

### Validation process

**검증은 repository의 코드를 실제로 실행합니다. 격리하지 않습니다.**

`shell=True`를 쓰지 않는 것은 Atlas가 shell wrapper를 거치지 않는다는 뜻일 뿐이고, 환경변수를 줄이는 것도 sandbox가 아닙니다. 실행된 코드는 subprocess를 띄우고 network에 접속하고 host filesystem에 접근할 수 있습니다. Python 테스트는 import만으로, `npm run`은 script 본문으로, mypy는 plugin으로 임의 코드를 실행합니다.

executor에게 shell 도구를 주지 않았더라도, executor가 test나 `package.json`을 고친 뒤 validation이 그것을 실행하면 그 제한을 우회하는 경로가 됩니다.

그래서 **명시적 신뢰 정책 뒤에** 둡니다.

- 기본값은 `untrusted`이고 fail closed입니다.
- `untrusted`에서는 repository 코드를 실행하지 않는 step만 수행합니다. Node package script는 신뢰 없이 실행하지 않습니다.
- 신뢰는 `ATLAS_VALIDATION_TRUST=trusted` 또는 명시적 repository 목록으로만 부여합니다.
- 정적 검사만 수행한 결과에는 그 사실을 경고 evidence로 남깁니다.

Atlas가 지키는 것과 지키지 않는 것을 나눠 적습니다.

| 항목 | 보장 |
| --- | --- |
| shell wrapper 미사용, argv list | 예 |
| 명령 선택이 repository 근거 기반 | 예 |
| 사용자 텍스트가 명령에 들어가지 않음 | 예 |
| dependency 설치 안 함 | 예 |
| provider credential 환경 미전달 | 예 |
| 출력 redaction | 예 |
| **실행된 코드의 network 차단** | **아니오** |
| **실행된 코드의 filesystem 경계** | **아니오** |
| **실행된 코드의 subprocess 제한** | **아니오** |

### Git publication

- Atlas는 **merge하지 않습니다.** draft PR만 만들고 approve, ready-for-review 전환, merge, squash, rebase를 하지 않습니다. 사람이 최종 gate입니다.
- **force push를 하지 않습니다.** `--force`도 `--force-with-lease`도 `+refs/...` refspec도 쓰지 않습니다. remote가 다른 commit을 가리키면 덮어쓰지 않고 recovery로 남깁니다.
- `main`/`master` 같은 보호 branch에 push하지 않습니다.
- push refspec은 명시적입니다. 현재 branch나 기본 branch를 추측하지 않습니다.
- **`origin`을 무조건 믿지 않습니다.** remote URL을 parse해 GitHub host와 정확한 `owner/repo`가 Task repository와 같은지 확인합니다. lookalike 경로와 다른 host를 거부합니다. 이 검증은 환경변수로 끌 수 없습니다.
- `git add -A`를 쓰지 않습니다. 검증이 승인한 경로만 stage하고, stage 결과가 승인 집합과 다르면 중단합니다.
- 전역 git config를 바꾸지 않습니다. commit identity는 해당 명령에만 지정합니다.
- commit hook을 실행하지 않습니다. hook은 repository가 제어하는 임의 코드입니다.
- commit message에는 식별자만 넣습니다. 사용자 텍스트를 git history에 영구히 남기지 않습니다.
- PR 본문에 검증 로그 전문, 로컬 artifact 경로, provider 응답 전문을 넣지 않습니다.
- **git/GitHub credential 값을 읽어 저장하지 않습니다.** argv, DB, log, event, 오류 메시지 어디에도 넣지 않습니다. 환경의 credential helper와 Authorization 헤더로만 씁니다.
- 외부 side effect마다 durable checkpoint를 남기고, 모호한 외부 상태를 자동으로 덮어쓰지 않습니다.

### Provider credential

- Atlas는 provider credential의 raw value를 읽거나 저장하지 않습니다.
- auth 파일 내용을 복사하지 않습니다.
- API key를 argv에 넣지 않습니다.
- Claude Code는 현재 로그인된 CLI 세션을 사용하며, 실행 환경은 기존 allowlist를 그대로 씁니다.

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
