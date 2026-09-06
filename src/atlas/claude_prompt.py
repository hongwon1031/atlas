"""Claude Code에 넘길 prompt를 Atlas가 deterministic하게 만듭니다.

정책과 prompt를 실행 코드에서 분리하라는 AGENTS.md 코딩 원칙을 따릅니다.
adapter는 이 모듈이 만든 텍스트를 전달만 하고 내용을 재해석하지 않습니다.

두 가지를 지킵니다.

- **Task contract가 유일한 입력원입니다.** 자유 prompt로 Atlas Task 경계를
  우회하는 경로를 만들지 않습니다. 개발용 override는 명시적으로 분리합니다.
- **shell escaping을 직접 하지 않습니다.** 결과 텍스트는 argv가 아니라 stdin
  으로 전달합니다. Issue 본문은 사용자 입력이므로 argv에 넣지 않습니다.
"""

from __future__ import annotations

from typing import Any

# prompt에 담을 항목별 상한. Issue 본문은 길이 제한이 없으므로 잘라서
# 전달합니다. 잘렸다는 사실은 prompt에 명시합니다.
MAX_OBJECTIVE_CHARS = 4_000
MAX_ENTRY_CHARS = 500
MAX_ENTRIES = 30

# 어떤 Task도 우회할 수 없는 실행 경계입니다. Task 본문보다 먼저 놓습니다.
STANDING_RULES: tuple[str, ...] = (
    "너는 지금 Atlas worker가 만든 격리된 git worktree 안에서 실행되고 있다.",
    "현재 작업 디렉터리 밖의 경로를 읽거나 쓰지 마라.",
    "이 repository의 main/master branch를 직접 수정하지 마라. 지금 branch에서만 작업한다.",
    "git commit, git push, branch 전환, tag 생성을 하지 마라. 변경은 working tree에 남겨 둔다.",
    "Task 범위 밖의 파일을 바꾸지 마라. 관련 없는 정리나 리팩터링을 함께 하지 마라.",
    "새 dependency를 추가하지 마라.",
    "credential, token, 환경변수 값을 출력하거나 파일에 쓰지 마라.",
    "작업을 마치면 어떤 파일을 왜 바꿨는지 마지막에 요약해 보고하라.",
    "지시가 불명확하면 추측해서 광범위하게 바꾸지 말고, 무엇이 불명확한지 보고하라.",
)


def _clip(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[:limit] + " …(생략됨)"


def _bullets(entries: Any, limit: int = MAX_ENTRIES) -> list[str]:
    items = [_clip(e, MAX_ENTRY_CHARS) for e in (entries or ()) if str(e or "").strip()]
    if len(items) > limit:
        items = items[:limit] + [f"…({len(items) - limit}개 더 있음)"]
    return [f"- {item}" for item in items]


def _criteria(entries: Any) -> list[str]:
    out: list[str] = []
    for entry in (entries or ())[:MAX_ENTRIES]:
        if isinstance(entry, dict):
            identifier = str(entry.get("id") or "").strip()
            description = _clip(entry.get("description"), MAX_ENTRY_CHARS)
        else:
            identifier, description = "", _clip(entry, MAX_ENTRY_CHARS)
        if not description:
            continue
        out.append(f"- {identifier + ': ' if identifier else ''}{description}")
    return out


def _scope_lines(scope: Any) -> list[str]:
    if not isinstance(scope, dict):
        return []
    lines: list[str] = []
    for key in ("paths", "operations", "external_systems", "unclassified"):
        lines.extend(_bullets(scope.get(key)))
    return lines


def build_prompt(task: dict[str, Any], run_id: str, branch: str | None = None) -> str:
    """저장된 Task에서 prompt를 만듭니다.

    입력은 DB의 current Task revision(`task_json`)입니다. 같은 Task와 같은 Run
    이면 항상 같은 텍스트가 나옵니다.
    """

    task_id = str(task.get("task_id") or "")
    repository = str(task.get("repository") or "")

    sections: list[str] = []
    sections.append("# Atlas Task")
    sections.append("")
    sections.append(f"- Task ID: {task_id}")
    sections.append(f"- Run ID: {run_id}")
    sections.append(f"- Repository: {repository}")
    if branch:
        sections.append(f"- Branch: {branch}")
    sections.append("")

    sections.append("## 실행 경계")
    sections.append("")
    sections.extend(f"- {rule}" for rule in STANDING_RULES)
    sections.append("")

    sections.append("## Objective")
    sections.append("")
    sections.append(_clip(task.get("objective"), MAX_OBJECTIVE_CHARS) or "(명시되지 않음)")
    sections.append("")

    criteria = _criteria(task.get("acceptance_criteria"))
    sections.append("## Acceptance Criteria")
    sections.append("")
    sections.extend(criteria or ["- (명시되지 않음)"])
    sections.append("")

    constraints = _bullets(task.get("constraints"))
    if constraints:
        sections.append("## Constraints")
        sections.append("")
        sections.extend(constraints)
        sections.append("")

    allowed = _scope_lines(task.get("allowed_scope"))
    if allowed:
        sections.append("## Allowed Scope")
        sections.append("")
        sections.append("아래 범위 안에서만 변경한다.")
        sections.append("")
        sections.extend(allowed)
        sections.append("")

    forbidden = _scope_lines(task.get("forbidden_scope"))
    if forbidden:
        sections.append("## Forbidden Scope")
        sections.append("")
        sections.append("아래는 어떤 경우에도 건드리지 않는다.")
        sections.append("")
        sections.extend(forbidden)
        sections.append("")

    sections.append("## 보고")
    sections.append("")
    sections.append(
        "작업을 마치면 변경한 파일 경로와 변경 이유를 한 줄씩 요약하라. "
        "아무것도 바꾸지 않았다면 그 이유를 명시하라."
    )

    return "\n".join(sections).strip() + "\n"
