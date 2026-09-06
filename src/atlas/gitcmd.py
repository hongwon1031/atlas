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

    def status_entries(self) -> list[str]:
        """`git status --porcelain=v1` 줄 목록. untracked 파일도 포함합니다."""

        return self.run("status", "--porcelain=v1", "--untracked-files=all").lines()

    def staged_paths(self) -> set[str]:
        """staging area에 올라간 경로.

        rename은 `R  old -> new`가 아니라 `--name-only`가 새 경로만 주므로,
        원본 경로도 얻기 위해 status를 함께 봅니다.
        """

        paths = {
            line.strip().replace("\\", "/")
            for line in self.run("diff", "--cached", "--name-only").lines()
            if line.strip()
        }
        for entry in self.status_entries():
            if len(entry) < 4 or entry[0] == " " or entry[0] == "?":
                continue
            for part in entry[3:].split(" -> "):
                cleaned = part.strip().strip('"').replace("\\", "/")
                if cleaned:
                    paths.add(cleaned)
        return paths

    def stage_paths(self, paths: tuple[str, ...] | list[str]) -> None:
        """지정한 경로만 stage합니다.

        `git add -A`를 쓰지 않습니다. 검증이 승인한 경로만 올려야 합니다.
        `--`로 경로 인자를 구분해 `-`로 시작하는 이름이 옵션으로 해석되지
        않게 합니다. `--` 이후는 pathspec이므로 임의 옵션이 들어갈 수 없습니다.
        """

        targets = [str(path).strip() for path in paths if str(path).strip()]
        if not targets:
            return

        # git에 아무것도 해당하지 않는 경로가 섞이면 `git add`가 통째로
        # 실패합니다. 한 번도 추적된 적 없는 파일이 사라진 경우가 그렇습니다.
        # 그 경로는 stage할 것이 없으므로 조용히 빼되, 나머지는 정상 처리합니다.
        tracked = {
            line.strip().replace("\\", "/")
            for line in self.run("ls-files", "--", *targets).lines()
            if line.strip()
        }
        stageable = [
            path for path in targets if (self.cwd / path).exists() or path in tracked
        ]
        if not stageable:
            return
        # 삭제된 파일도 반영하려면 `--all` pathspec 모드가 필요합니다. 이것은
        # `git add -A`와 다릅니다. 지정한 경로에만 적용됩니다.
        self.run("add", "--all", "--", *stageable)

    def commit(
        self,
        message: str,
        *,
        author_name: str,
        author_email: str,
        allow_empty: bool = False,
    ) -> str:
        """staged 내용을 commit하고 새 HEAD를 돌려줍니다.

        전역 git config를 바꾸지 않습니다. 이 명령에만 identity를 지정합니다.
        """

        args = [
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            # commit hook이 임의 코드를 실행할 수 있습니다. Atlas가 만드는
            # commit에는 실행하지 않습니다.
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            message,
        ]
        if allow_empty:
            args.append("--allow-empty")
        self.run(*args)
        return self.head_revision()

    def remote_head(self, remote: str, branch: str) -> str | None:
        """remote branch가 가리키는 commit. 없으면 `None`입니다."""

        result = self.run("ls-remote", "--heads", remote, f"refs/heads/{branch}")
        for line in result.lines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == f"refs/heads/{branch}":
                return parts[0]
        return None

    def push_branch(self, remote: str, branch: str, expected_head: str) -> None:
        """정확한 refspec으로 push합니다.

        - 현재 branch나 기본 branch를 추측하지 않습니다.
        - **force 계열 옵션을 쓰지 않습니다.** `--force`도 `--force-with-lease`도
          쓰지 않습니다. 남의 commit을 덮어쓸 수단을 두지 않습니다.
        - push할 commit이 지금 HEAD인지 먼저 확인합니다.
        """

        head = self.head_revision()
        if head != expected_head:
            raise GitError(
                ["git", "push"],
                1,
                f"HEAD가 기대한 commit과 다릅니다: {head} != {expected_head}",
                "head_mismatch",
            )
        self.run("push", "--no-verify", remote, f"{expected_head}:refs/heads/{branch}")

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
