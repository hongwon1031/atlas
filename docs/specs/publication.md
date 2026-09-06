# Git Publication v0.1

이 문서는 검증을 통과한 Run의 결과를 GitHub로 전달하는 방법을 정의합니다. [Validation Pipeline](validation-pipeline.md)이 "그 변경이 통과하는가"를 다뤘다면 이 문서는 "그 변경을 어떻게 전달하는가"를 다룹니다.

## 위치

```
Issue → Task → claim → Run → isolated worktree → Claude 구현
     → AwaitingValidation → Validating → Succeeded
     → commit → push → draft PR → 사람의 검토와 merge
```

`Succeeded` 이후를 다룹니다. **merge는 하지 않습니다.** 사람이 최종 gate입니다.

## Run 성공과 publication 성공은 다른 사실

Run이 `Succeeded`여도 publication은 별도 operational attempt입니다.

- 코드가 검증됐다는 사실은 push가 실패해도 변하지 않습니다.
- push는 성공했는데 PR 생성이 실패할 수 있습니다.
- PR 생성은 성공했는데 DB 저장이 실패할 수 있습니다.
- 재시작 후 외부 side effect를 다시 만들면 중복이 생깁니다.

그래서 **publication 실패가 Run을 `Succeeded`에서 되돌리지 않습니다.** 구현과 검증의 성공은 사실로 남고, 게시 실패는 operational failure로 따로 기록합니다.

## Publication lifecycle

| 단계 | 의미 |
| --- | --- |
| `Starting` | DB에 예약. 아직 side effect 없음 |
| `Committing` | commit 수행 중 |
| `Pushing` | push 수행 중 |
| `CreatingPR` | draft PR 생성 중 |
| `Published` | 완료 |
| `RecoveryRequired` | 외부 상태가 모호하거나 충돌. **자동으로 덮어쓰지 않음** |
| `Failed` | 시작하거나 진행하다 실패 |

Run 하나에 active publication은 최대 하나입니다. **database의 partial unique index가 최종적으로 막습니다.**

## Start gate

다음이 모두 참일 때만 시작합니다.

- Run이 `Succeeded`
- validation attempt가 `Finished` + `passed`
- workspace가 `ready`이고 지금도 유효함
- branch가 기대한 atlas branch이고 보호 branch가 아님
- 승인이 아직 유효함
- claim이 살아 있고 owner가 일치하고 lease가 유효함
- active execution 없음
- active validation 없음
- 이미 게시되지 않음

**예약 transaction 안에서 다시 확인합니다.** git과 GitHub side effect는 이 transaction 밖에서 수행합니다.

publication은 terminal Run(`Succeeded`)에서 돌기 때문에 execution·validation이 쓰는 `run_active` guard를 쓰지 않고 별도 guard를 씁니다. 승인과 claim은 여전히 살아 있어야 합니다. **승인이 회수된 Task의 결과를 GitHub에 올리면 안 됩니다.**

## 최종 무결성 재확인

commit 직전에 검증 이후 worktree가 그대로인지 확인합니다.

- workspace integrity가 여전히 유효한가
- branch가 기대한 atlas branch인가
- 보호 branch가 아닌가
- HEAD가 검증 시점과 같은가 (새 commit이 없는가)
- 내용 지문이 검증 시점과 같은가
- forbidden/out-of-scope 변경이 없는가
- 게시할 변경이 존재하는가

하나라도 어긋나면 `publication_workspace_drift`입니다. **검증 이후 사람이 파일을 바꿨으면 절대 게시하지 않습니다.** 검증한 내용과 게시하는 내용이 다르면 검증 결과가 무의미합니다.

지문은 [Validation Pipeline](validation-pipeline.md)의 것과 같습니다. 파일 이름 집합이 아니라 내용 기반입니다.

### 재시도 경로

앞선 시도가 이미 commit을 만들었을 수 있습니다. 그 경우 HEAD가 base보다 하나 앞서는 것이 정상입니다. 다음이 **모두** 맞을 때만 우리 commit으로 채택합니다.

1. base보다 정확히 하나 앞선다
2. working tree가 깨끗하다
3. subject가 이 Task의 결정적 commit subject와 같다
4. branch가 기대한 atlas branch다

채택하더라도 범위 검사는 건너뛰지 않습니다.

## Commit policy

executor는 commit하지 않으므로 Atlas가 직접 만듭니다.

- **`git add -A`를 쓰지 않습니다.** 검증이 승인한 경로만 stage합니다. `--`로 경로 인자를 구분해 `-`로 시작하는 이름이 옵션으로 해석되지 않게 합니다.
- 삭제된 파일을 반영하려면 지정 경로에 대한 `--all` pathspec 모드가 필요합니다. 이것은 `git add -A`와 다릅니다. 지정한 경로에만 적용됩니다.
- commit 전에 **staged 경로 집합 == 검증된 경로 집합**을 확인합니다. 다르면 `publication_stage_mismatch`입니다.
- commit 후에 HEAD가 정확히 하나 전진했는지, working tree가 깨끗한지, branch가 그대로인지 확인합니다.
- commit hook을 실행하지 않습니다(`--no-verify`). hook은 repository가 제어하는 임의 코드입니다.

### Commit author

전역 git config를 바꾸지 않습니다. **이 명령에만** identity를 지정합니다.

```
git -c user.name=Atlas -c user.email=atlas@users.noreply.github.com commit ...
```

설정으로 바꿀 수 있지만 값은 검증합니다. 빈 값이나 개행이 들어간 값은 거부합니다.

### Commit message

```
atlas: implement <task-id>

Task: <task-id>
Run: <run-id>
Issue: #<number>
```

**식별자만 넣습니다.** objective를 포함해 사용자 텍스트를 git history에 영구히 남기지 않습니다. 사람이 읽을 요약은 PR title과 body에 있습니다.

## Push policy

push 직전에 다시 확인합니다.

- 현재 branch가 기대한 atlas branch인가
- HEAD가 방금 만든 commit인가
- 보호 branch가 아닌가
- remote가 Task repository인가

refspec은 명시적입니다.

```
git push --no-verify <remote> <commit>:refs/heads/<expected-branch>
```

- **현재 branch나 기본 branch를 추측하지 않습니다.**
- **force 계열 옵션을 쓰지 않습니다.** `--force`도 `--force-with-lease`도 쓰지 않습니다. 남의 commit을 덮어쓸 수단을 두지 않습니다.
- `+refs/...` 형태의 강제 refspec도 쓰지 않습니다.

## Remote identity

**`origin`을 무조건 믿지 않습니다.** remote URL을 parse해 확인합니다.

- GitHub host인가
- 경로 조각이 정확히 `owner/repo`인가
- Task repository와 같은가

suffix 비교는 `https://github.com/evil/owner/repo.git` 같은 lookalike URL을 통과시키므로 쓰지 않습니다. 다른 host도 거부합니다.

이 검증은 **주입 가능한 경계**입니다. 기본 구현은 엄격한 GitHub 검증이고 환경변수로 끌 수 없습니다. 다른 검증이 필요하면 호출자가 명시적으로 다른 구현을 넘겨야 합니다.

## Credential

Atlas는 **token 값을 읽어 저장하지 않습니다.**

- argv에 넣지 않습니다.
- DB에 넣지 않습니다.
- log와 event에 넣지 않습니다.
- 오류 메시지에 넣지 않습니다.

git push는 환경의 credential helper를 씁니다. GitHub API는 환경변수의 token을 Authorization 헤더로만 씁니다. 값 자체는 client 객체 밖으로 나가지 않고 출력에는 존재 여부만 남습니다.

credential이 없으면 `publication_authentication_failed`로 분류합니다.

## Draft PR

**항상 draft입니다.** Atlas는 approve, ready-for-review 전환, merge, squash, rebase를 하지 않습니다. 사람의 검토와 merge가 최종 gate입니다.

| 항목 | 값 |
| --- | --- |
| base | Run의 base branch (기본 `main`) |
| head | 정확한 atlas branch |
| draft | `true` |
| title | `[Atlas] <Task ID>: <objective 요약>` |

본문에는 Task ID, Run ID, source Issue 참조, 검증 요약, 변경 파일 목록, 유의사항, 사람 검토 필요 문구가 들어갑니다.

들어가지 않는 것입니다.

- 검증 로그 전문
- 로컬 artifact 경로
- Claude 응답 전문
- secret

### Issue 연결

`Refs #N`만 씁니다. **`Closes #N`을 쓰지 않습니다.** PR merge가 곧 Task 종료인지는 아직 정해지지 않았고, 정해지지 않은 정책을 자동화가 먼저 확정하면 안 됩니다.

## Side effect 순서와 checkpoint

```
A. publication 예약
B. 최종 무결성 확인
C. commit
D. commit SHA 저장          ← checkpoint
E. remote branch 확인
F. 필요하면 push
G. pushed 상태 저장          ← checkpoint
H. 기존 PR 확인
I. 없으면 draft PR 생성
J. PR 연결 저장              ← checkpoint
K. Published 확정
```

각 side effect 뒤에 durable checkpoint를 남깁니다. 저장 전에 죽어도 reconciliation이 외부 증거로 복구합니다.

## Idempotency

**같은 side effect를 다시 만들지 않습니다.**

| 대상 | 이미 있으면 |
| --- | --- |
| commit | 조건을 만족하면 채택 |
| remote branch (같은 commit) | push를 건너뜀 |
| remote branch (다른 commit) | `publication_remote_conflict`. **덮어쓰지 않음** |
| 같은 head/base의 열린 PR | 채택 |
| 같은 head/base의 열린 PR이 여러 개 | `publication_pr_conflict` |

이미 `Published`인 Run은 gate에서 거부합니다.

## Crash window와 reconciliation

| 창 | 이미 일어난 일 | 복구 근거 |
| --- | --- | --- |
| commit 후 저장 전 | local commit | branch HEAD |
| push 후 저장 전 | remote branch | `ls-remote` |
| PR 생성 후 저장 전 | draft PR | head/base 검색 |

판정과 처리입니다.

| 판정 | 처리 |
| --- | --- |
| `publication_not_committed` | 다시 시작 가능 |
| `publication_commit_adopted` | 채택 |
| `publication_remote_branch_missing` | push 재시도 가능 |
| `publication_push_adopted` | 채택 |
| `publication_pr_missing` | PR 생성 재시도 가능 |
| `publication_pr_adopted` | 채택 후 `Published` |
| `publication_remote_conflict` | **RecoveryRequired.** 덮어쓰지 않음 |
| `publication_pr_conflict` | **RecoveryRequired** |
| `publication_local_drift` | **RecoveryRequired** |
| `publication_branch_mismatch` | **RecoveryRequired** |
| `publication_published_without_pr` | **RecoveryRequired** |

**reconciliation은 side effect를 만들지 않습니다.** 확인과 채택과 기록만 합니다. commit·push·PR 생성을 대신 수행하지 않습니다.

## Failure taxonomy

Run failure taxonomy와 **분리합니다.**

| category | 의미 | recovery |
| --- | --- | --- |
| `publication_gate_failed` | 시작 근거 없음 | 아니오 |
| `publication_workspace_drift` | 검증 이후 worktree가 바뀜 | 아니오 |
| `publication_commit_failed` | commit 실패 | 아니오 |
| `publication_stage_mismatch` | stage 경로가 검증 경로와 다름 | 아니오 |
| `publication_remote_invalid` | remote identity 불일치 | 아니오 |
| `publication_authentication_failed` | credential 문제 | 아니오 |
| `publication_push_failed` | push 실패 | 아니오 |
| `publication_remote_conflict` | remote가 다른 commit | **예** |
| `publication_pr_create_failed` | PR 생성 실패 | 아니오 |
| `publication_pr_conflict` | PR이 여러 개거나 조건 충돌 | **예** |
| `publication_state_ambiguous` | 외부 상태를 확정할 수 없음 | **예** |
| `publication_nothing_to_publish` | 게시할 변경 없음 | 아니오 |

## CLI

```bash
python -m atlas publication-start --run-id <run-id>
python -m atlas publication-show --run-id <run-id>
python -m atlas publication-reconcile [--skip-remote]
```

`publication-start`는 `Succeeded` 외의 상태에서 거부합니다.

`publication-show`는 commit SHA, remote branch, PR number/url, status, validation id, warnings를 보여 줍니다.

| 환경변수 | 기본값 | 의미 |
| --- | --- | --- |
| `ATLAS_GIT_REMOTE` | `origin` | push할 remote 이름 |
| `ATLAS_COMMIT_AUTHOR_NAME` | `Atlas` | commit author |
| `ATLAS_COMMIT_AUTHOR_EMAIL` | `atlas@users.noreply.github.com` | commit email |
| `ATLAS_GITHUB_TOKEN` / `GITHUB_TOKEN` | (없음) | GitHub API token |

## 이번 범위 밖

- auto merge, ready-for-review 자동 전환, approve
- reviewer AI
- deployment, release
- branch cleanup 자동화
- PR comment automation
- GitHub 외 Git hosting
- force push
