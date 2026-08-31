# Pull Request Output Contract v0.1

이 문서는 Atlas Agent가 사람에게 전달하는 Pull Request의 최소 품질과 정보 계약을 정의합니다. `.github/pull_request_template.md`는 이 계약의 작성 인터페이스이며, 이 문서가 의미의 정규 정의입니다.

이 계약은 Current manual workflow와 Target MVP 모두에 적용됩니다. 현재는 사람이 Issue를 Executor에게 전달하고 Executor가 PR을 엽니다. Target MVP에서는 self-hosted Claude Code worker와 Delivery Adapter가 같은 계약으로 PR을 생성합니다.

## Purpose

Pull Request만 읽어도 Reviewer가 다음을 판단할 수 있어야 합니다.

- 어떤 Task를 왜 수행했는가
- 무엇을 변경했고 무엇을 의도적으로 제외했는가
- Acceptance Criteria를 어떻게 검증했는가
- 어떤 위험과 미해결 질문이 남아 있는가
- 변경이 Project, security, approval 경계를 지켰는가

## Branch and Delivery Rules

- PR base는 Task가 승인한 default branch이며 MVP에서는 `main`입니다.
- head는 하나의 Task만을 위한 독립 branch입니다.
- AI는 `main`에 직접 push하거나 PR을 merge하지 않습니다.
- PR은 최소 한 개의 검토 가능한 commit을 포함합니다.
- 검증이 끝나지 않았으면 Draft PR로 표시하고 Ready라고 주장하지 않습니다.
- 사람의 최종 승인이 없으면 `Approved` 또는 `Completed` 상태로 이동하지 않습니다.

## Title

권장 형식은 다음과 같습니다.

```text
<type>(<optional-scope>): <imperative summary>
```

허용되는 기본 type은 `docs`, `feat`, `fix`, `test`, `chore`입니다. 제목은 변경 방법보다 결과를 설명하고 Task가 하나임을 드러내야 합니다.

## Required PR Sections

### Linked Task

- GitHub Issue URL 또는 Task source URI
- 안정적인 Task ID
- 관련 ADR 또는 상위 Epic 링크

### Summary

- Objective와 사용자에게 제공되는 결과
- 3~5개의 핵심 변경
- application code, dependency, CI, infrastructure 변경 여부
- workflow mode: `manual` 또는 `automated`
- 실제 Executor와 Adapter; Target MVP primary는 `claude_code_self_hosted`, Codex Cloud는 manual/secondary

### Scope

- 포함한 작업
- 명시적으로 제외한 작업
- Task의 Allowed Scope를 벗어나지 않았다는 설명

### Changed Files and Artifacts

각 파일 또는 artifact 경로와 역할을 나열합니다. 생성, 수정, 삭제를 구분하고 생성된 report나 외부 artifact에는 URI와 checksum을 기록합니다.

### Decisions and Structural Changes

- 이번 PR에서 Accepted로 제안하는 결정
- Proposed 상태로 남은 결정
- 문서 hierarchy나 public contract를 바꾼 이유
- 대안과 migration 또는 링크 호환성 영향

### Validation

검증마다 다음 값을 포함합니다.

| 필드 | 설명 |
| --- | --- |
| check | command, manual review, policy check 이름 |
| result | `passed`, `failed`, `not_run`, `not_applicable` |
| evidence | 요약된 출력, artifact, diff, checklist |
| reason | `not_run` 또는 `not_applicable`의 이유 |

`not_run`을 `passed`로 표현하지 않습니다. 문서 전용 변경은 application test가 `not_applicable`일 수 있지만 Markdown, YAML, 링크, scope, secret 검사는 별도로 보고합니다.

### Risks and Reviewer Focus

- 알려진 위험과 영향 범위
- Reviewer가 집중해야 할 파일과 질문
- 안전한 rollback 또는 PR 폐기 방법

### Open Questions

- 사용자 결정이 필요한 항목
- 후속 ADR이나 Task가 필요한 항목
- 없으면 `없음`이라고 명시

## Validation Summary Example

```markdown
| 검증 | 결과 | 근거 또는 미실행 이유 |
| --- | --- | --- |
| Acceptance Criteria | passed | AC-01~AC-04 확인 |
| `git diff --check` | passed | exit 0 |
| Issue Form YAML parse | passed | parser exit 0 |
| application tests | not_applicable | documentation-only change |
| secret pattern scan | passed | known credential pattern 0건 |
```

긴 command output과 원문 로그 전체를 PR body에 붙이지 않습니다. 필요한 경우 redacted artifact로 보관하고 checksum과 링크를 제공합니다.

## Automated Run Metadata

자동 실행이 도입되면 다음 정보를 PR 또는 연결된 artifact에서 확인할 수 있어야 합니다.

```yaml
task_id: ATLAS-0001
run_id: run-0001
agent_role: Implementer
executor_adapter: claude_code_self_hosted
workflow_mode: automated
base_sha: <sha>
head_sha: <sha>
started_at: <timestamp>
completed_at: <timestamp>
validation_summary: <artifact-uri>
```

Target MVP primary 예시의 `executor_adapter` 값은 `claude_code_self_hosted`입니다. Codex Cloud로 수동 수행한 PR은 `workflow_mode: manual`, `executor_adapter: codex_cloud`로 사실대로 기록합니다.

비용과 token 사용량은 비밀정보가 아니고 정확히 측정할 수 있을 때만 요약합니다.

## Security and Privacy

- PR 제목, body, diff, artifact에 secret, 개인정보, 회사 정보를 포함하지 않습니다.
- credential 오류는 provider 응답 원문 대신 분류된 원인만 기록합니다.
- 외부 링크는 Project가 허용한 source만 사용합니다.
- 다른 Project의 경로, branch, Issue, memory를 evidence로 사용하지 않습니다.
- secret 또는 project boundary 위반이 발견되면 PR을 Ready로 전환하지 않습니다.

## Review and Approval

- 가능하면 Implementer와 다른 Agent가 먼저 diff와 계약 준수 여부를 검토합니다.
- 자동 Reviewer는 사람 approval을 대신하지 않습니다.
- change request는 Task를 `RevisionRequested`로 되돌리고 원래 PR과 Run chain을 유지합니다.
- 사람 approval은 승인 actor와 정확한 head commit SHA에 연결합니다.
- approval 후 commit이 추가되면 이전 approval을 그대로 재사용하지 않습니다.

## Failure Delivery

Current manual workflow에서 PR을 만들 수 없으면 Executor가 연결된 Issue 또는 사람에게 다음을 보고합니다. Target MVP에서는 Delivery Adapter가 같은 내용을 Issue에 기록합니다.

- 실패한 단계와 분류된 원인
- 실행한 검증과 마지막 안전 상태
- 생성된 branch 또는 artifact의 정리 상태
- 자동 retry 횟수와 소진 여부
- 사람이 선택할 수 있는 다음 action

실패를 숨기기 위해 빈 PR이나 검증되지 않은 PR을 생성하지 않습니다.

## Open Questions

- automated Run metadata를 PR body와 별도 artifact 중 어디에 둘지
- commit signature와 provenance attestation을 MVP에 포함할지
- Reviewer approval 뒤 head 변경 시 자동 dismiss 정책을 branch protection으로 강제할지
- manual Run과 automated Run의 metadata 필수 수준을 다르게 둘지
