# Validation Pipeline v0.1

이 문서는 Atlas가 구현 결과를 어떻게 검증하는지 정의합니다. [Execution Runtime](execution-runtime.md)이 "무엇을 바꿨는가"를 다룬다면 이 문서는 "그 변경이 통과하는가"를 다룹니다.

provider를 모릅니다. Claude Code가 만든 변경이든 사람이 만든 변경이든 같은 방식으로 검증합니다. provider별 validation logic을 만들지 않습니다.

## 위치

```
Issue → Task → claim → Run → isolated worktree → 구현
     → changes_applied → AwaitingValidation → Validating → Succeeded / Failed
```

이 문서는 `AwaitingValidation` 이후를 다룹니다. `Succeeded` 이후의 commit, push, PR 생성은 아직 구현되지 않았습니다.

## Run 상태 전이

| 전이 | 조건 |
| --- | --- |
| `AwaitingValidation` → `Validating` | validation 예약 성공 |
| `Validating` → `Succeeded` | required step 전부 통과 |
| `Validating` → `Failed` | required step 중 하나라도 실패하거나 수행 불가 |

`Validating`의 성질입니다.

| 성질 | 값 |
| --- | --- |
| terminal | 아님 |
| Task의 active Run 슬롯 | 차지함 |
| heartbeat 기대 | **예.** 검증 process가 실제로 돌고 있습니다 |
| staleness reconciliation | 대상. heartbeat가 끊기면 `Orphaned` |

`AwaitingValidation`은 heartbeat 대상이 아니고 `Validating`은 대상입니다. 전자는 아무 process도 돌지 않는 대기 상태이고, 후자는 검증 process가 살아 있어야 하는 상태입니다.

**`AwaitingValidation`에서만 validation을 시작할 수 있습니다.** 구현이 끝나지 않았거나 이미 검증된 Run을 다시 검증하면 근거 없는 결론이 나옵니다.

## Validation lifecycle

executor runtime과 같은 단계 구조를 씁니다.

| 단계 | 의미 |
| --- | --- |
| `Starting` | DB에 예약했고 첫 step은 아직 시작하지 않음 |
| `Running` | step을 수행 중 |
| `Finished` | 판정까지 끝남 |
| `Failed` | 시작하거나 진행하다 실패. 남은 process가 있을 수 있어 reconciliation 대상 |

Run 하나에 active validation은 최대 하나입니다. **database의 partial unique index가 최종적으로 막습니다.** 중복 시작을 코드 조건만으로 막지 않습니다.

### 시작 전 확인

validation을 시작하기 전에 다음을 봅니다.

- Run이 존재하고 `AwaitingValidation`인가
- workspace가 `ready`이고 지금도 유효한가
- 승인이 아직 유효한가
- claim이 살아 있고 owner가 일치하는가
- lease가 유효한가
- active execution이 없는가

**예약 transaction 안에서 다시 확인합니다.** 첫 확인과 예약 사이에 승인이 회수되거나 claim이 풀리는 창을 닫습니다. subprocess는 이 transaction 밖에서 띄웁니다. transaction이 process 수명만큼 열려 있으면 다른 worker가 막힙니다.

### 완료 직전 확인

검증이 통과했더라도 **확정 직전에 승인과 claim을 다시 봅니다.** 검증 도중 승인이 회수되면 `Succeeded`로 확정하지 않고 `policy_violation`으로 실패시킵니다.

## Validation plan

repository에서 **발견한 근거로만** 명령을 고릅니다. 추측으로 임의 shell 명령을 만들지 않습니다.

계획은 왜 그 step을 골랐는지 evidence를 함께 남깁니다.

### contract 강도

도구 설정이 있다고 해서 곧바로 "이 repository는 그 도구로 게이트한다"는 뜻은 아닙니다. `pyproject.toml`의 `[tool.ruff]`는 편집기 설정만 담는 경우가 흔합니다. 그래서 근거의 강도를 나눕니다.

| 강도 | 근거 | 도구가 없을 때 |
| --- | --- | --- |
| strong | 전용 config 파일(`pytest.ini`, `ruff.toml`, `mypy.ini`, `pyrightconfig.json` 등) 또는 프로젝트 dependency 선언 | `error` (required) |
| weak | `pyproject.toml`의 `[tool.X]` table만 존재 | `skipped` |
| none | 근거 없음 | `skipped` |

이 구분이 없으면 편집기 설정만 있는 repository가 도구 미설치만으로 실패하고, 구분을 아예 두지 않으면 진짜로 요구하는 repository를 통과시킵니다.

### Python

| step | 선택 근거 | 명령 |
| --- | --- | --- |
| tests (pytest) | pytest config 또는 dependency 선언 | `python -m pytest -q` |
| tests (unittest) | `tests/` 디렉터리 존재, pytest contract 없음 | `python -m unittest discover -s tests -t .` |
| compile | `src/` 또는 `tests/` 존재 | `python -m compileall -q <dirs>` |
| lint | ruff contract | `ruff check .` |
| typecheck | mypy contract | `python -m mypy .` |
| typecheck | pyright contract | `pyright` |

pytest contract가 없으면 표준 라이브러리 `unittest`를 씁니다. dependency를 요구하지 않는 쪽을 먼저 고릅니다.

`compileall`은 stdlib이고 결정적이라 source 디렉터리가 있으면 **required**입니다. 대상은 `src`와 `tests`로 한정합니다. repository 전체를 무작정 훑지 않습니다.

### Node

`package.json`의 `scripts` 중 **allowlist에 있는 이름만** 실행합니다.

| 허용 script | required |
| --- | --- |
| `test` | 예 |
| `lint` | 예 |
| `typecheck` | 예 |
| `build` | 아니오 (이번 범위에서 optional) |

- package manager는 lockfile로 정합니다. `pnpm-lock.yaml` → pnpm, `yarn.lock` → yarn, `package-lock.json` → npm.
- **script 본문을 직접 실행하지 않습니다.** `<manager> run <script>`로 실행하고 정의 해석은 package manager에 맡깁니다.
- package manager 실행 파일이 없으면 `error`입니다.
- `node_modules`가 없으면 **설치하지 않고** `dependencies_not_installed`로 보고합니다.

### 금지

- dependency 설치. `npm install`, `pip install`, `poetry install`, `uv sync`를 자동 실행하지 않습니다.
- network 사용.
- allowlist 밖 script 실행.
- 사용자 Issue 본문이나 임의 텍스트를 명령에 넣는 일.

## Command safety

- `shell=True`를 쓰지 않습니다. 모든 명령은 argv list입니다.
- cwd는 Run의 검증된 worktree로 고정합니다.
- 환경은 OS 기본 allowlist만 씁니다. **executor에 주었던 credential 환경을 넘기지 않습니다.** 검증은 provider 인증이 필요 없습니다.
- 모든 step에 timeout이 있습니다.
- 출력은 상한 있는 redacted artifact로 저장하고 process tree까지 정리합니다. executor runtime의 process 경로를 그대로 재사용합니다.

## Step 종류와 상태

| kind | 의미 |
| --- | --- |
| `workspace_integrity` | 실행 직전 workspace 경계 재확인 |
| `git_policy` | 구현 이후 repository 상태 확인 |
| `tests` | 테스트 실행 |
| `compile` | 컴파일 가능 여부 |
| `lint` | lint |
| `typecheck` | 타입 검사 |
| `build` | 빌드 (optional) |

| status | 의미 |
| --- | --- |
| `passed` | 검사했고 통과했습니다 |
| `failed` | 검사했고 실패했습니다 |
| `skipped` | **검사할 근거가 없었습니다.** 실패가 아닙니다 |
| `error` | 검사하려 했지만 수행하지 못했습니다. 통과로 볼 수 없습니다 |

`skipped`와 `passed`를 구분합니다. "검사했고 통과했다"와 "검사할 근거가 없었다"는 전혀 다른 사실이고, 뭉뚱그리면 검증되지 않은 변경을 통과시킵니다.

step 결과에는 명령(redacted), exit code, 시작·종료 시각, 소요 시간, stdout/stderr artifact 경로, truncation 여부, 사유, 근거가 남습니다.

## Workspace integrity

실행 직전에 다시 확인합니다. gate에서 한 번 봤지만 그 사이에 바뀔 수 있고, 잘못된 경로에서 검증을 돌리면 결과 자체가 무의미합니다.

- cwd가 기록된 worktree인가
- git toplevel이 그 worktree인가
- 실제 checkout branch가 기대한 atlas branch인가
- common git dir가 대상 repository인가
- 보호 branch가 아닌가
- worktree가 git에 등록돼 있고 worker root 안에 있는가

실패하면 검증 process를 시작하지 않습니다.

## Git policy

기준선은 **workspace를 만든 시점**입니다. base revision에 깨끗한 트리로 시작했으므로, 지금 상태와의 차이가 곧 이번 Run이 만든 변경입니다. 현재 상태를 자기 자신과 비교하면 아무 변경도 보이지 않습니다.

확인 항목입니다.

- HEAD가 base에서 바뀌지 않았는가 (commit이 없는가)
- branch가 유지되는가
- 변경된 파일이 존재하는가
- forbidden scope 변경이 없는가
- allowed scope를 벗어난 변경이 없는가
- `.git` 내부가 수정되지 않았는가

구현 시점의 `implementation_completed` evidence와 지금 변경 목록을 비교합니다. 다르면 **누군가 중간에 worktree를 건드린 것**이므로 `workspace_changed_after_implementation`으로 실패시킵니다. 조용히 통과시키지 않습니다.

## Success policy

**"실행한 명령이 모두 exit 0"을 성공으로 정의하지 않습니다.**

- required step이 하나라도 `failed` 또는 `error`면 Run `Failed`입니다.
- required step이 모두 `passed`면 Run `Succeeded`입니다.
- optional step의 실패는 경고로 남기고 판정을 막지 않습니다.

required는 다음과 같습니다.

- workspace integrity — 항상
- git policy — 항상
- tests — repository에서 test capability를 발견했을 때
- compile — source 디렉터리가 있을 때
- lint / typecheck — repository contract가 있을 때
- build — 항상 optional

required step이 막히면 남은 step은 `earlier_required_step_failed`로 건너뜁니다. 결론이 바뀌지 않는데 시간만 쓰기 때문입니다.

## 테스트가 없는 repository

무조건 pytest를 실행해 실패시키지 않습니다.

- 명시적 test config나 script가 있으면 실행합니다.
- `tests/` 디렉터리가 있으면 stdlib `unittest`로 실행합니다.
- 아무 근거도 없으면 `no_tests_discovered`를 기록합니다.

**MVP 정책**: workspace와 git policy가 통과하고 구현 변경이 존재하면, 테스트 capability가 없어도 `Succeeded`를 허용합니다. 다만 `no_tests_discovered`와 `validation_passed_with_no_tests`를 경고 evidence로 반드시 남깁니다.

이 선택의 의미는 명확합니다. **"검증했다"가 아니라 "검증할 것이 없었다"입니다.** 문서 전용 repository처럼 테스트가 없는 대상을 영원히 막지 않기 위한 절충이고, 근거가 남으므로 사람이 판단할 수 있습니다.

## Failure taxonomy

provider 어휘를 쓰지 않습니다.

| validation category | Run failure category |
| --- | --- |
| `validation_policy_violation` | `policy_violation` |
| `validation_workspace_invalid` | `policy_violation` |
| `validation_test_failed` | `validation_failed` |
| `validation_lint_failed` | `validation_failed` |
| `validation_typecheck_failed` | `validation_failed` |
| `validation_compile_failed` | `validation_failed` |
| `validation_build_failed` | `validation_failed` |
| `validation_command_missing` | `validation_failed` |
| `validation_timeout` | `timeout` |
| `validation_process_failed` | `transient_executor` |
| `validation_state_ambiguous` | `unknown` |
| `validation_gate_failed` | `policy_violation` |

## 저장

`validations` table에 attempt를, `validation_steps` table에 step 결과를 남깁니다. event만으로는 restart 후 "어디까지 끝났는가"를 복원할 수 없습니다.

step은 **시작할 때 먼저 `running`으로 기록합니다.** 결과만 기록하면 중간에 죽은 경우를 구분할 수 없습니다. process id와 identity도 함께 남깁니다.

event에는 전체 stdout/stderr를 저장하지 않습니다. 짧은 요약만 남기고 전문은 log artifact에 둡니다. 요약도 redaction을 거칩니다.

## Restart와 reconciliation

| 상황 | 판정 | 자동 종료 |
| --- | --- | --- |
| `AwaitingValidation`, 시작 전 | 정상 | 해당 없음 |
| `Starting`인데 step이 없음 | `validation_never_started` | 해당 없음 |
| `Running` + identity 일치 | `validation_healthy` | — |
| `Running` + process 없음 | `validation_process_missing` | 해당 없음 |
| `Running` + PID는 있지만 identity 불일치 | `validation_pid_identity_mismatch` | **금지** |
| identity 확인 불가 | `validation_identity_unverifiable` | **금지** |
| step은 있는데 process를 붙이지 못함 | `validation_process_never_attached` | 해당 없음 |
| `Running`인데 실행 중 step이 없음 | `validation_state_ambiguous` (high) | 해당 없음 |
| terminal Run인데 validation 생존 | `validation_surviving_terminal_run` (high) | **ownership 확인 전 금지** |

**자동으로 재검증하지 않습니다.** 판정과 기록만 하고 다시 실행할지는 사람이나 상위 정책이 결정합니다.

결과를 저장하기 전에 중단되면 `ambiguous`로 남깁니다. **자동으로 성공 처리하지 않습니다.**

## CLI

```bash
python -m atlas validation-start --run-id <run-id>
python -m atlas validation-show --run-id <run-id>
python -m atlas validation-reconcile
```

`validation-start`는 `AwaitingValidation` 외의 상태에서 거부합니다.

## 이번 범위 밖

- git commit, push, GitHub PR 생성
- dependency 설치
- 사용자가 제공하는 임의 shell validation 명령
- AI reviewer, 코드 품질 자동 수정
- 자동 재검증
- step 단위 resume (ambiguous 상태 식별까지만 구현)
