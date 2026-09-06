"""commit message와 PR 본문을 deterministic하게 만듭니다.

정책과 실행 코드를 분리하라는 AGENTS.md 원칙을 따릅니다. 같은 Task와 같은
Run이면 항상 같은 텍스트가 나옵니다.

두 가지를 지킵니다.

- **사용자 텍스트를 그대로 옮기지 않습니다.** Issue 본문 전체를 commit
  message나 PR 본문에 넣지 않습니다. 길이를 제한하고 redaction을 거칩니다.
- **검증 로그 전문을 붙이지 않습니다.** 요약과 개수만 남깁니다. 로컬 artifact
  경로도 노출하지 않습니다.
"""

from __future__ import annotations

from typing import Any

from .redaction import redact_line

# commit message 각 부분의 상한입니다.
MAX_SUBJECT_CHARS = 72
MAX_OBJECTIVE_CHARS = 120
MAX_COMMIT_BODY_CHARS = 1_000

# PR title/body 상한.
MAX_TITLE_CHARS = 120
MAX_PR_BODY_CHARS = 8_000
MAX_LISTED_FILES = 50


def _clean(text: Any, limit: int) -> str:
    """공백을 접고 redaction을 거쳐 길이를 제한합니다."""

    value = redact_line(str(text or ""), limit=limit * 2)
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def commit_subject(task_id: str) -> str:
    """`atlas: implement ATLAS-0042` 형태의 고정 제목."""

    return _clean(f"atlas: implement {task_id}", MAX_SUBJECT_CHARS)


def commit_message(
    task_id: str, run_id: str, issue_number: int | None = None, objective: str = ""
) -> str:
    """commit message를 만듭니다.

    본문에는 **식별자만** 넣습니다. Task ID, Run ID, source Issue 번호입니다.

    objective를 넣지 않습니다. 사용자 텍스트를 git history에 영구히 남길
    이유가 없고, 사람이 읽을 요약은 PR title과 body에 있습니다. `objective`
    인자는 호출부 호환을 위해 남기되 사용하지 않습니다.
    """

    lines = [commit_subject(task_id), ""]
    lines.append(f"Task: {task_id}")
    lines.append(f"Run: {run_id}")
    if issue_number:
        lines.append(f"Issue: #{issue_number}")
    body = "\n".join(lines)
    if len(body) > MAX_COMMIT_BODY_CHARS:
        body = body[:MAX_COMMIT_BODY_CHARS].rstrip()
    return body + "\n"


def pull_request_title(task_id: str, objective: str = "") -> str:
    """`[Atlas] ATLAS-0042: 요약` 형태."""

    summary = _clean(objective, MAX_TITLE_CHARS - len(task_id) - 12)
    if summary:
        return _clean(f"[Atlas] {task_id}: {summary}", MAX_TITLE_CHARS)
    return _clean(f"[Atlas] {task_id}", MAX_TITLE_CHARS)


def _validation_section(validation: dict[str, Any] | None) -> list[str]:
    """검증 결과를 요약합니다. 로그 전문과 로컬 경로는 넣지 않습니다."""

    if not validation:
        return ["- 검증 기록을 찾지 못했습니다."]

    lines: list[str] = []
    outcome = validation.get("outcome") or "unknown"
    lines.append(f"- 결과: `{outcome}`")

    steps = validation.get("steps") or []
    passed = [s for s in steps if s.get("status") == "passed"]
    skipped = [s for s in steps if s.get("status") == "skipped"]
    required_passed = [s for s in passed if s.get("required")]
    lines.append(
        f"- required step {len(required_passed)}개 통과, "
        f"전체 {len(passed)}개 통과 / {len(skipped)}개 건너뜀"
    )

    ran = [s.get("name") for s in passed if s.get("name")]
    if ran:
        lines.append(f"- 수행: {', '.join(str(name) for name in ran[:10])}")
    if skipped:
        detail = ", ".join(
            f"{s.get('name')}({s.get('reason') or 'no_reason'})" for s in skipped[:10]
        )
        lines.append(f"- 건너뜀: {detail}")

    warnings = validation.get("warnings") or []
    for warning in warnings[:10]:
        lines.append(f"- ⚠️ `{warning}`")
    trust = validation.get("trust_policy")
    if trust:
        lines.append(f"- 신뢰 정책: `{trust}`")
    return lines


def pull_request_body(
    *,
    task_id: str,
    run_id: str,
    repository: str,
    branch: str,
    base_branch: str,
    commit_sha: str,
    issue_number: int | None = None,
    objective: str = "",
    changed_files: tuple[str, ...] | list[str] = (),
    validation: dict[str, Any] | None = None,
    warnings: tuple[str, ...] | list[str] = (),
) -> str:
    """PR 본문을 만듭니다.

    source Issue는 **참조만** 합니다. `Closes #N`을 쓰지 않습니다. PR merge가
    곧 Task 종료인지는 아직 정해지지 않았고, 정해지지 않은 정책을 자동화가
    먼저 확정하면 안 됩니다.
    """

    lines: list[str] = []
    lines.append("Atlas가 만든 draft Pull Request입니다.")
    lines.append("")
    lines.append("## Task")
    lines.append("")
    lines.append(f"- Task ID: `{task_id}`")
    lines.append(f"- Run ID: `{run_id}`")
    lines.append(f"- Repository: `{repository}`")
    lines.append(f"- Branch: `{branch}` → `{base_branch}`")
    if commit_sha:
        lines.append(f"- Commit: `{commit_sha}`")
    if issue_number:
        # 자동 close하지 않습니다. 참조만 남깁니다.
        lines.append(f"- Source Issue: Refs #{issue_number}")
    summary = _clean(objective, MAX_OBJECTIVE_CHARS * 2)
    if summary:
        lines.append("")
        lines.append("## Objective")
        lines.append("")
        lines.append(summary)

    lines.append("")
    lines.append("## Validation")
    lines.append("")
    lines.extend(_validation_section(validation))

    files = [str(path) for path in (changed_files or ()) if str(path).strip()]
    lines.append("")
    lines.append(f"## 변경 파일 ({len(files)}개)")
    lines.append("")
    if files:
        for path in files[:MAX_LISTED_FILES]:
            lines.append(f"- `{path}`")
        if len(files) > MAX_LISTED_FILES:
            lines.append(f"- …외 {len(files) - MAX_LISTED_FILES}개")
    else:
        lines.append("- (없음)")

    notes = [str(w) for w in (warnings or ()) if str(w).strip()]
    if notes:
        lines.append("")
        lines.append("## 유의사항")
        lines.append("")
        for note in notes[:20]:
            lines.append(f"- {note}")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "Generated by Atlas. **사람의 검토와 merge가 필요합니다.** "
        "Atlas는 이 PR을 approve하거나 ready-for-review로 바꾸거나 merge하지 않습니다."
    )

    body = "\n".join(lines)
    if len(body) > MAX_PR_BODY_CHARS:
        body = body[: MAX_PR_BODY_CHARS - 40].rstrip() + "\n\n…(본문이 잘렸습니다)\n"
    return body
