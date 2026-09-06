"""git CLI adapter.

subprocess로 git을 호출하는 유일한 경계입니다. 다른 모듈은 git 세부사항을 알지
않습니다.

docs/security-governance.md의 command 제약을 따릅니다.

- `shell=True`를 쓰지 않고 argument list로만 호출합니다.
- 사용자 입력을 command에 그대로 넣지 않습니다. 호출자가 먼저 sanitize합니다.
- 모든 호출에 timeout을 둡니다.
- stderr에는 credential이 섞일 수 있으므로 persistence에 그대로 넣지 않습니다.
  `GitError.redacted()`가 저장 가능한 형태를 돌려줍니다.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

DEFAULT_TIMEOUT_SECONDS = 30.0

# URL에 박힌 credential(https://user:token@host)과 token 형태 문자열을 지웁니다.
_CREDENTIAL_IN_URL = re.compile(r"(https?://)[^/\s@]+@")
_TOKEN_LIKE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,})\b")
_MAX_DETAIL_CHARS = 200


def redact(text: str) -> str:
    """secret이 섞일 수 있는 git 출력을 저장 가능한 형태로 만듭니다."""

    cleaned = _CREDENTIAL_IN_URL.sub(r"\1<redacted>@", text or "")
    cleaned = _TOKEN_LIKE.sub("<redacted>", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:_MAX_DETAIL_CHARS]


class GitError(Exception):
    """git 호출 실패.

    `stderr`는 진단용으로만 들고 있으며 persistence에는 `redacted()`를 씁니다.
    """

    def __init__(
        self, command: Sequence[str], exit_code: int, stderr: str = "", category: str = "git_failed"
    ) -> None:
        self.command = tuple(command)
        self.exit_code = exit_code
        self.stderr = stderr
        self.category = category
        super().__init__(f"git {' '.join(self.subcommand)} 실패 (exit {exit_code})")

    @property
    def subcommand(self) -> tuple[str, ...]:
        """path나 branch 같은 인자를 제외한 git 하위 명령 이름."""

        parts: list[str] = []
        for token in self.command[1:]:
            if token.startswith("-"):
                continue
            parts.append(token)
            if len(parts) == 2:
                break
        return tuple(parts)

    def redacted(self) -> dict[str, Any]:
        """event log에 넣어도 되는 형태."""

        return {
            "git_command": " ".join(self.subcommand),
            "exit_code": self.exit_code,
            "category": self.category,
            "detail": redact(self.stderr),
        }


@dataclass(frozen=True)
class GitResult:
    stdout: str
    stderr: str

    @property
    def text(self) -> str:
        return self.stdout.strip()

    def lines(self) -> list[str]:
        return [line for line in self.stdout.splitlines() if line.strip()]


class GitRunner:
    """지정된 repository에서 git을 실행합니다."""

    def __init__(self, cwd: Path | str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.cwd = Path(cwd)
        self.timeout_seconds = timeout_seconds

    def run(self, *args: str, check: bool = True) -> GitResult:
        command = ["git", "-C", str(self.cwd), *args]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                # shell을 쓰지 않습니다. argument list로만 호출합니다.
                shell=False,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            raise GitError(command, 127, "git 실행 파일을 찾을 수 없습니다.", "git_missing") from None
        except subprocess.TimeoutExpired:
            raise GitError(command, 124, "git 명령이 시간 안에 끝나지 않았습니다.", "timeout") from None

        if check and completed.returncode != 0:
            raise GitError(command, completed.returncode, completed.stderr or "")
        return GitResult(completed.stdout or "", completed.stderr or "")

    # -- 조회 -----------------------------------------------------------

    def toplevel(self) -> Path:
        return Path(self.run("rev-parse", "--show-toplevel").text)

    def common_dir(self) -> Path:
        path = Path(self.run("rev-parse", "--git-common-dir").text)
        return path if path.is_absolute() else (self.cwd / path).resolve()

    def is_repository(self) -> bool:
        try:
            return self.run("rev-parse", "--is-inside-work-tree").text == "true"
        except GitError:
            return False

    def resolve_revision(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", f"{ref}^{{commit}}").text

    def current_branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD").text

    def head_revision(self) -> str:
        return self.run("rev-parse", "HEAD").text

    def branch_exists(self, branch: str) -> bool:
        try:
            self.run("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
            return True
        except GitError:
            return False

    def is_valid_branch_name(self, branch: str) -> bool:
        try:
            self.run("check-ref-format", "--branch", branch)
            return True
        except GitError:
            return False

    def remote_url(self, remote: str = "origin") -> str | None:
        try:
            return self.run("remote", "get-url", remote).text or None
        except GitError:
            return None

    def is_dirty(self) -> bool:
        """추적/미추적 변경이 있는지 확인합니다."""

        return bool(self.run("status", "--porcelain").lines())

    def worktrees(self) -> list[dict[str, str]]:
        """`git worktree list --porcelain` 결과를 dict 목록으로 돌려줍니다."""

        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in self.run("worktree", "list", "--porcelain").stdout.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            entries.append(current)
        return entries

    # -- 변경 -----------------------------------------------------------

    def add_worktree(self, path: Path, branch: str, base_revision: str) -> None:
        """`base_revision`에서 새 branch를 만들고 worktree를 붙입니다."""

        self.run("worktree", "add", "-b", branch, str(path), base_revision)

    def remove_worktree(self, path: Path, force: bool = False) -> None:
        """worktree를 제거합니다.

        git은 저장되지 않은 변경이 있는 worktree를 기본적으로 거부합니다.
        `force`는 호출자가 dirty 상태를 명시적으로 허용했을 때만 씁니다.
        """

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        self.run(*args, str(path))

    def prune_worktrees(self) -> None:
        self.run("worktree", "prune")

    def delete_branch(self, branch: str, force: bool = False) -> None:
        self.run("branch", "-D" if force else "-d", branch)
