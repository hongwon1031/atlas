"""테스트용 Issue body와 fake source.

body는 .github/ISSUE_TEMPLATE/atlas-task.yml이 렌더링하는 형태를 따릅니다.
"""

from __future__ import annotations

from atlas.issue_source import IssueRecord, IssueSourceError

VALID_BODY = """### Project

atlas

### Objective

Issue intake vertical slice의 동작을 검증한다.

### Constraints

- 문서와 테스트만 변경
- application dependency 추가 금지

### Acceptance Criteria

- [ ] 유효한 Issue가 Task로 변환된다.
- [ ] 잘못된 Issue가 검증 오류를 돌려준다.

### Allowed Scope

- docs/**
- src/**
- create
- update

### Forbidden Scope

- .env
- secrets/**
- delete
- production

### Risk Level

documentation

### Priority

normal

### Validation

- Markdown 형식 검사가 통과한다.
- 변경 범위가 Task를 벗어나지 않는다.

### Context and References

docs/specs/task-schema.md

### Risk / Notes

_No response_

### Safety Confirmations

- [X] 이 Task에는 secret, 개인정보, 회사 내부 정보가 포함되지 않았습니다.
- [X] AI가 `main`에 직접 push하거나 merge해서는 안 된다는 점을 확인했습니다.
- [X] 완료 조건과 허용 범위를 사람이 검토할 수 있을 만큼 구체적으로 작성했습니다.
"""


def body_without(*labels: str) -> str:
    """지정한 `###` section을 통째로 제거한 body를 만듭니다."""

    blocks = VALID_BODY.split("### ")
    kept = [block for block in blocks if not any(block.startswith(label) for label in labels)]
    return "### ".join(kept)


def body_replacing(label: str, value: str) -> str:
    """지정한 section의 값을 바꿉니다."""

    blocks = VALID_BODY.split("### ")
    rebuilt = []
    for block in blocks:
        if block.startswith(label):
            rebuilt.append(f"{label}\n\n{value}\n\n")
        else:
            rebuilt.append(block)
    return "### ".join(rebuilt)


def make_issue(
    body: str = VALID_BODY,
    *,
    number: int = 42,
    title: str = "[Atlas Task] intake slice",
    repository: str = "hongwon1031/atlas",
    state: str = "open",
    is_pull_request: bool = False,
) -> IssueRecord:
    return IssueRecord(
        repository=repository,
        repository_id="123456",
        number=number,
        issue_id=f"issue-{number}",
        title=title,
        body=body,
        state=state,
        author="github:hongwon1031",
        created_at="2026-09-01T00:00:00Z",
        updated_at="2026-09-01T00:00:00Z",
        is_pull_request=is_pull_request,
    )


class FakeIssueSource:
    """호출 횟수를 세는 in-memory `IssueSource`."""

    def __init__(self, *issues: IssueRecord) -> None:
        self._issues = {(issue.repository, issue.number): issue for issue in issues}
        self.calls = 0

    def fetch_issue(self, repository: str, number: int) -> IssueRecord:
        self.calls += 1
        try:
            return self._issues[(repository, number)]
        except KeyError:
            raise IssueSourceError("not_found", "Issue를 찾을 수 없습니다.") from None
