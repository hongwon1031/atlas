"""검증된 Run을 commit·push하고 draft PR로 전달합니다.

외부 side effect를 다루므로 앞 단계들과 원칙이 하나 더 늘어납니다.

- **side effect 하나마다 durable checkpoint.** commit은 성공했는데 DB 저장
  전에 죽으면, 재시작한 worker가 그 사실을 알아야 다시 commit하지 않습니다.
- **모호한 외부 상태를 자동으로 덮어쓰지 않습니다.** remote가 다른 commit을
  가리키면 force push가 아니라 recovery입니다.
- **이미 있는 것을 다시 만들지 않습니다.** 같은 commit이 이미 push돼 있으면
  push를 건너뛰고, 같은 head/base의 열린 PR이 있으면 그것을 채택합니다.

Run status는 건드리지 않습니다. **구현과 검증이 성공했다는 사실은 게시에
실패해도 변하지 않습니다.**
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import RunConfig
from .github_pr import GitHubPullRequestClient, PullRequestClient
from .gitcmd import GitError, GitRunner
from .publication_content import (
    commit_message,
    commit_subject,
    pull_request_body,
    pull_request_title,
)
from .publication_models import (
    GateResult,
    PublicationError,
    PublicationFailure,
    PublicationReport,
    PublicationStatus,
    PullRequestRef,
)
from .redaction import redact_line
from .schema import RunStatus
from .store import RunError, TaskStore, from_iso, to_iso, utcnow
from .validation_models import ValidationOutcome, ValidationStatus
from .workspace import PROTECTED_BRANCHES, GITHUB_HOSTS, parse_github_remote
from .workspace_service import WorkspaceService
from .worktree_changes import ContentDigest, safe_content_digest, safe_fingerprint

DEFAULT_REMOTE = "origin"
DEFAULT_AUTHOR_NAME = "Atlas"
DEFAULT_AUTHOR_EMAIL = "atlas@users.noreply.github.com"

# git 명령 timeout. push는 network를 쓰므로 조회보다 넉넉합니다.
DEFAULT_GIT_TIMEOUT_SECONDS = 30.0
DEFAULT_PUSH_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class PublicationConfig:
    """게시 정책.

    author identity를 설정으로 바꿀 수 있지만 값은 검증합니다. 빈 값이나
    개행이 들어간 값은 commit trailer를 깨뜨립니다.
    """

    remote: str = DEFAULT_REMOTE
    author_name: str = DEFAULT_AUTHOR_NAME
    author_email: str = DEFAULT_AUTHOR_EMAIL
    git_timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS
    push_timeout_seconds: float = DEFAULT_PUSH_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        for label, value in (
            ("remote", self.remote),
            ("author_name", self.author_name),
            ("author_email", self.author_email),
        ):
            text = str(value or "")
            if not text.strip():
                raise ValueError(f"{label}는 비어 있을 수 없습니다.")
            if any(char in text for char in "\r\n"):
                raise ValueError(f"{label}에 개행을 넣을 수 없습니다: {text!r}")
        if "@" not in self.author_email:
            raise ValueError(f"author_email 형식이 아닙니다: {self.author_email!r}")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> PublicationConfig:
        import os

        env = environ if environ is not None else dict(os.environ)
        config = cls()
        from dataclasses import replace

        if remote := env.get("ATLAS_GIT_REMOTE", "").strip():
            config = replace(config, remote=remote)
        if name := env.get("ATLAS_COMMIT_AUTHOR_NAME", "").strip():
            config = replace(config, author_name=name)
        if email := env.get("ATLAS_COMMIT_AUTHOR_EMAIL", "").strip():
            config = replace(config, author_email=email)
        return config


class RemoteIdentity(Protocol):
    """push 대상 remote가 Task repository와 같은지 확인하는 경계."""

    def verify(self, remote: str, url: str, expected_repository: str) -> None:
        """다르면 `PublicationError`를 던집니다."""
        ...


class GitHubRemoteIdentity:
    """GitHub host와 정확한 `owner/repo` 일치를 요구합니다.

    **`origin`을 무조건 믿지 않습니다.** suffix 비교는
    `https://github.com/evil/owner/repo.git` 같은 lookalike URL을 통과시키므로
    경로 조각을 정확히 확인합니다. 다른 host도 거부합니다.

    이 정책은 설정으로 약화할 수 없습니다. 다른 검증이 필요하면 호출자가
    명시적으로 다른 구현을 주입해야 합니다.
    """

    def verify(self, remote: str, url: str, expected_repository: str) -> None:
        parsed = parse_github_remote(url)
        if parsed is None:
            raise PublicationError(
                PublicationFailure.REMOTE_INVALID,
                "remote URL에서 owner/repo를 확정하지 못했습니다.",
                {"remote": remote},
            )
        host, slug = parsed
        if host not in GITHUB_HOSTS:
            raise PublicationError(
                PublicationFailure.REMOTE_INVALID,
                f"GitHub host가 아닙니다: {host}",
                {"host": host, "remote": remote},
            )
        if slug.lower() != (expected_repository or "").lower():
            raise PublicationError(
                PublicationFailure.REMOTE_INVALID,
                f"remote repository가 Task와 다릅니다: {slug} != {expected_repository}",
                {"remote_repository": slug, "task_repository": expected_repository},
            )


class PublicationGateFailed(RunError):
    """게시를 시작할 근거가 없습니다."""

    def __init__(self, message: str, checks: dict[str, bool]) -> None:
        super().__init__(PublicationFailure.GATE_FAILED.value, message)
        self.checks = checks

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(name for name, ok in self.checks.items() if not ok)


class PublicationService:
    """Run 하나를 게시합니다."""

    def __init__(
        self,
        store: TaskStore,
        workspaces: WorkspaceService,
        config: PublicationConfig | None = None,
        run_config: RunConfig | None = None,
        pull_requests: PullRequestClient | None = None,
        remote_identity: "RemoteIdentity | None" = None,
    ) -> None:
        self._store = store
        self._workspaces = workspaces
        self._config = config or PublicationConfig()
        self._run_config = run_config or RunConfig()
        self._pull_requests = pull_requests or GitHubPullRequestClient()
        # 기본값은 엄격한 GitHub 검증입니다. 환경변수로 끌 수 없습니다.
        self._remote_identity = remote_identity or GitHubRemoteIdentity()
        self._worker_id = ""

    @property
    def config(self) -> PublicationConfig:
        return self._config

    # -- gate --------------------------------------------------------------

    def authorization_checks(self, run, worker_id: str, now=None) -> dict[str, bool]:
        """게시를 계속해도 되는 근거만 모읍니다.

        시작 gate와 달리 **문맥에 의존하는 항목을 넣지 않습니다.** 진행 중에는
        Run이 `Validating`이 아니고 publication도 이미 active이므로, 그런
        항목까지 보면 정상 경로가 막힙니다.

        여기 있는 것은 외부 side effect를 만들기 직전에 항상 참이어야 하는
        것들입니다. 승인이 회수됐거나 claim을 잃었으면 GitHub에 무언가를
        만들면 안 됩니다.
        """

        moment = now or utcnow()
        checks = {
            "task_approved": False,
            "claim_active": False,
            "claim_owner_matches": False,
            "lease_valid": False,
            "workspace_ready": False,
            "branch_not_protected": bool(run.branch) and not _is_protected(run.branch),
        }
        checks["workspace_ready"] = (
            run.workspace_status.value == "ready" and bool(run.worktree_path)
        )

        task = self._store.task_by_fingerprint(run.fingerprint)
        checks["task_approved"] = bool(task and task["approved"] and task["is_current"])

        claim = self._store.claim_for(run.claim_id)
        if claim is not None and claim["released_at"] is None:
            checks["claim_active"] = True
            checks["claim_owner_matches"] = claim["lease_owner"] == worker_id
            checks["lease_valid"] = from_iso(claim["lease_expires_at"]) > moment
        return checks

    def _require_authorization(
        self,
        publication_id: str,
        run,
        worker_id: str,
        stage: str,
        *,
        side_effects_exist: bool,
    ) -> None:
        """외부 side effect 직전에 승인과 claim을 다시 확인합니다.

        `side_effects_exist`가 참이면 이미 만든 것(push된 branch 등)이
        있습니다. 그것을 되돌리지 않습니다. force push나 삭제로 정리하려 들면
        더 큰 문제를 만듭니다. checkpoint를 남기고 recovery로 넘깁니다.
        """

        checks = self.authorization_checks(run, worker_id)
        lost = [name for name, ok in checks.items() if not ok]
        if not lost:
            return

        self._store.update_publication(
            publication_id,
            event="publication_authorization_lost",
            detail={
                "stage": stage,
                "failed_checks": lost,
                "side_effects_exist": side_effects_exist,
                "detail": "이미 만든 side effect는 되돌리지 않습니다.",
            },
        )
        raise PublicationError(
            PublicationFailure.AUTHORIZATION_LOST,
            f"{stage} 직전에 실행 근거를 잃었습니다: {', '.join(lost)}",
            {
                "stage": stage,
                "failed_checks": lost,
                "side_effects_exist": side_effects_exist,
            },
        )

    def gate(self, run_id: str, worker_id: str, now=None) -> GateResult:
        """게시해도 되는지 확인합니다."""

        moment = now or utcnow()
        checks: dict[str, bool] = {
            "run_exists": False,
            "run_succeeded": False,
            "validation_passed": False,
            "workspace_ready": False,
            "workspace_valid": False,
            "branch_not_protected": False,
            "task_approved": False,
            "claim_active": False,
            "claim_owner_matches": False,
            "lease_valid": False,
            "no_active_execution": False,
            "no_active_validation": False,
            "not_already_published": False,
        }

        run = self._store.run(run_id)
        if run is None:
            return GateResult(checks)
        checks["run_exists"] = True
        checks["run_succeeded"] = run.status is RunStatus.SUCCEEDED
        checks["workspace_ready"] = (
            run.workspace_status.value == "ready" and bool(run.worktree_path)
        )
        checks["branch_not_protected"] = bool(run.branch) and not _is_protected(run.branch)
        checks["no_active_execution"] = self._store.active_execution(run_id) is None
        checks["no_active_validation"] = self._store.active_validation(run_id) is None

        validation = self._passing_validation(run_id)
        checks["validation_passed"] = validation is not None

        published = [
            row
            for row in self._store.publications(run_id)
            if row["status"] == PublicationStatus.PUBLISHED.value
        ]
        checks["not_already_published"] = not published

        if checks["workspace_ready"]:
            try:
                self._workspaces.planner.validate_existing(run.branch, run.worktree_path)
                checks["workspace_valid"] = True
            except Exception:  # noqa: BLE001 - 어떤 이유든 신뢰할 수 없습니다.
                checks["workspace_valid"] = False

        task = self._store.task_by_fingerprint(run.fingerprint)
        checks["task_approved"] = bool(task and task["approved"] and task["is_current"])

        claim = self._store.claim_for(run.claim_id)
        if claim is not None and claim["released_at"] is None:
            checks["claim_active"] = True
            checks["claim_owner_matches"] = claim["lease_owner"] == worker_id
            checks["lease_valid"] = from_iso(claim["lease_expires_at"]) > moment

        return GateResult(checks)

    def _passing_validation(self, run_id: str):
        for row in self._store.validations(run_id):
            if (
                row["status"] == ValidationStatus.FINISHED.value
                and row["outcome"] == ValidationOutcome.PASSED.value
            ):
                return row
        return None

    # -- remote identity ---------------------------------------------------

    def resolve_remote(self, run, task: dict[str, Any]) -> tuple[str, str]:
        """remote 이름과 URL을 확정합니다. 검증은 주입된 정책이 수행합니다."""

        expected = str(task.get("repository") or "").strip()
        if not expected:
            raise PublicationError(
                PublicationFailure.REMOTE_INVALID, "Task에 repository가 없습니다."
            )
        git = GitRunner(run.worktree_path, timeout_seconds=self._config.git_timeout_seconds)
        url = git.remote_url(self._config.remote)
        if not url:
            raise PublicationError(
                PublicationFailure.REMOTE_INVALID,
                f"remote {self._config.remote}를 찾을 수 없습니다.",
            )
        self._remote_identity.verify(self._config.remote, url, expected)
        return self._config.remote, url

    def _revalidate_remote(self, run, publication_id: str) -> str:
        """push 직전에 remote URL을 다시 읽고 검증합니다.

        예약 시점에 검증한 뒤 `git remote set-url`로 다른 repository를 가리키게
        만들 수 있습니다. remote **이름**만 믿고 push하면 그 repository로
        올라갑니다. 그래서 이름이 아니라 **매번 URL을 다시 확인합니다.**

        확인한 URL을 그대로 push 대상으로 씁니다. 이름을 거치지 않으므로
        확인과 사용 사이의 간격이 사라집니다.
        """

        row = self._store.publication(publication_id)
        expected_url = (row["remote_url"] or "") if row else ""
        expected_repository = row["github_repository"] if row else ""

        git = GitRunner(run.worktree_path, timeout_seconds=self._config.git_timeout_seconds)
        current = git.remote_url(self._config.remote) or ""
        if not current:
            raise PublicationError(
                PublicationFailure.REMOTE_CHANGED,
                f"remote {self._config.remote}가 사라졌습니다.",
                {"expected_url_present": bool(expected_url)},
            )
        if expected_url and current != expected_url:
            # URL 자체를 근거에 넣지 않습니다. credential이 박혀 있을 수 있습니다.
            raise PublicationError(
                PublicationFailure.REMOTE_CHANGED,
                "예약 이후 remote URL이 바뀌었습니다. push하지 않습니다.",
                {"remote": self._config.remote, "url_changed": True},
            )
        # 이름이 같아도 대상이 같다고 볼 수 없으므로 identity를 다시 봅니다.
        self._remote_identity.verify(self._config.remote, current, expected_repository)
        return current

    @staticmethod
    def _push_target(remote: str, url: str) -> str:
        """push와 조회에 쓸 대상.

        URL에 credential이 박혀 있으면 argv에 넣을 수 없으므로 remote 이름을
        씁니다. 그 경우에도 직전에 URL을 검증했습니다.
        """

        if _has_userinfo(url):
            return remote
        return url

    # -- 실행 ---------------------------------------------------------------

    def publish(self, run_id: str, worker_id: str) -> PublicationReport:
        """게시를 한 번 시도합니다."""

        gate = self.gate(run_id, worker_id)
        if not gate.passed:
            self._store.record_publication_event(
                None, run_id, "publication_gate_failed", {"stage": "initial", **gate.to_dict()}
            )
            raise PublicationGateFailed(
                f"게시 전 확인에 실패했습니다: {', '.join(gate.failed_checks)}", gate.checks
            )

        run = self._store.run(run_id)
        task = self._task_for(run)
        validation = self._passing_validation(run_id)
        remote, remote_url = self.resolve_remote(run, task)
        base_branch = run.base_branch or "main"
        repository = str(task.get("repository") or "")

        self._worker_id = worker_id
        publication_id = self._store.start_publication(
            run_id,
            worker_id=worker_id,
            github_repository=repository,
            branch=run.branch,
            base_branch=base_branch,
            remote=remote,
            remote_url=remote_url,
            validation_id=validation["validation_id"] if validation else None,
        )

        try:
            return self._execute(
                publication_id, run, task, validation, repository, base_branch, remote
            )
        except PublicationError as error:
            self._fail(publication_id, run_id, error)
            raise
        except (GitError, OSError) as error:
            wrapped = PublicationError(
                PublicationFailure.STATE_AMBIGUOUS,
                f"게시 도중 예기치 못한 오류입니다: {type(error).__name__}",
            )
            self._fail(publication_id, run_id, wrapped)
            raise wrapped from None

    def _execute(
        self,
        publication_id: str,
        run,
        task: dict[str, Any],
        validation,
        repository: str,
        base_branch: str,
        remote: str,
    ) -> PublicationReport:
        adopted: list[str] = []
        warnings: list[str] = []

        # B. 최종 무결성 재확인
        changed_files = self._final_integrity_check(publication_id, run, validation, task)

        # C~D. commit과 checkpoint
        self._store.update_publication(
            publication_id, status=PublicationStatus.COMMITTING
        )
        commit_sha, reused = self._commit(publication_id, run, task, changed_files)
        if reused:
            adopted.append("commit")

        # E~G. push 직전에 근거와 remote를 모두 다시 확인합니다.
        self._store.update_publication(publication_id, status=PublicationStatus.PUSHING)
        self._require_authorization(
            publication_id, run, self._worker_id, "push", side_effects_exist=False
        )
        pushed_new = self._push(publication_id, run, remote, commit_sha)
        if not pushed_new:
            adopted.append("remote_branch")

        # H~J. PR 생성 직전에도 다시 확인합니다. 이미 push한 branch는
        # 되돌리지 않고 recovery로 넘깁니다.
        self._store.update_publication(
            publication_id, status=PublicationStatus.CREATING_PR
        )
        self._require_authorization(
            publication_id, run, self._worker_id, "pull_request", side_effects_exist=True
        )
        pull_request, created = self._pull_request(
            publication_id,
            run,
            task,
            validation,
            repository,
            base_branch,
            commit_sha,
            changed_files,
            warnings,
        )
        if not created:
            adopted.append("pull_request")

        # K. 확정
        summary = (
            f"published: commit={commit_sha[:12]} branch={run.branch} "
            f"pr=#{pull_request.number}"
        )
        self._store.update_publication(
            publication_id,
            status=PublicationStatus.PUBLISHED,
            summary=summary,
            warnings=warnings,
            adopted=adopted,
            event="publication_published",
            detail={
                "commit_sha": commit_sha,
                "branch": run.branch,
                "pr_number": pull_request.number,
                "pr_url": pull_request.url,
                "adopted": adopted,
            },
        )
        return PublicationReport(
            publication_id=publication_id,
            run_id=run.run_id,
            status=PublicationStatus.PUBLISHED,
            branch=run.branch,
            base_branch=base_branch,
            repository=repository,
            commit_sha=commit_sha,
            remote=remote,
            pushed=True,
            pull_request=pull_request,
            summary=summary,
            adopted=tuple(adopted),
            warnings=tuple(warnings),
        )

    # -- B. 최종 무결성 ------------------------------------------------------

    def _final_integrity_check(
        self, publication_id: str, run, validation, task: dict[str, Any]
    ) -> tuple[str, ...]:
        """검증 이후 worktree가 그대로인지 확인합니다.

        **사람이 검증 뒤에 파일을 바꿨으면 절대 게시하지 않습니다.** 검증한
        내용과 게시하는 내용이 다르면 검증 결과가 무의미합니다.
        """

        from .worktree_changes import WorktreeState, compare

        try:
            self._workspaces.planner.validate_existing(run.branch, run.worktree_path)
        except Exception as error:  # noqa: BLE001
            raise PublicationError(
                PublicationFailure.WORKSPACE_DRIFT,
                f"workspace가 더 이상 유효하지 않습니다: {type(error).__name__}",
            ) from None

        git = GitRunner(run.worktree_path, timeout_seconds=self._config.git_timeout_seconds)
        head = git.head_revision()
        branch = git.current_branch()

        if branch != run.branch:
            raise PublicationError(
                PublicationFailure.WORKSPACE_DRIFT,
                f"branch가 바뀌었습니다: {branch} != {run.branch}",
                {"expected_branch": run.branch, "actual_branch": branch},
            )
        if _is_protected(branch):
            raise PublicationError(
                PublicationFailure.WORKSPACE_DRIFT, f"보호 branch입니다: {branch}"
            )

        # 재시도 경로입니다. 앞선 시도가 이미 commit을 만들었을 수 있습니다.
        # 그 경우 HEAD가 base보다 하나 앞서는 것이 정상입니다.
        expected = self._recorded_digest(publication_id)
        if self._atlas_commit(git, run, head, expected):
            return self._verify_committed(publication_id, run, git, head, task)
        if expected is not None and head != run.base_revision:
            # 지문은 있는데 HEAD 내용이 다릅니다. 다른 누군가의 commit입니다.
            raise PublicationError(
                PublicationFailure.CONTENT_MISMATCH,
                "branch에 검증한 내용과 다른 commit이 있습니다. 채택하지 않습니다.",
                {"head": head, "expected_digest": expected.digest},
            )

        baseline = self._validation_baseline(run.run_id)
        current = safe_fingerprint(run.worktree_path, self._config.git_timeout_seconds)
        expected_head = (baseline or {}).get("head") or run.base_revision

        if expected_head and head != expected_head:
            raise PublicationError(
                PublicationFailure.WORKSPACE_DRIFT,
                "검증 이후 새 commit이 생겼습니다.",
                {"expected_head": expected_head, "actual_head": head},
            )

        recorded_digest = (baseline or {}).get("digest")
        if recorded_digest and current.computed and current.digest != recorded_digest:
            raise PublicationError(
                PublicationFailure.WORKSPACE_DRIFT,
                "검증 이후 worktree 내용이 바뀌었습니다.",
                {"expected_digest": recorded_digest, "actual_digest": current.digest},
            )

        state = WorktreeState(head=head, branch=branch, entries=tuple(git.status_entries()))
        baseline_state = WorktreeState(
            head=run.base_revision or head, branch=branch, entries=()
        )
        changes = compare(baseline_state, state, task, process_succeeded=True)
        if changes.violations:
            raise PublicationError(
                PublicationFailure.WORKSPACE_DRIFT,
                f"허용 범위를 벗어난 변경입니다: {', '.join(changes.violations)}",
                {"violations": list(changes.violations)},
            )
        if not changes.changed_files:
            raise PublicationError(
                PublicationFailure.NOTHING_TO_PUBLISH, "게시할 변경이 없습니다."
            )

        # commit 채택이 metadata가 아니라 **내용**에 근거하도록, 지금 검증한
        # 변경 내용의 지문을 남깁니다. commit 전후로 같은 값이 나옵니다.
        digest = safe_content_digest(
            run.worktree_path, run.base_revision, timeout_seconds=self._config.git_timeout_seconds
        )
        self._store.update_publication(
            publication_id,
            content_digest=digest.to_dict(),
            event="publication_integrity_verified",
            detail={
                "head": head,
                "branch": branch,
                "changed_file_count": len(changes.changed_files),
                "fingerprint": current.to_dict(),
                "content_digest": digest.to_dict(),
                "baseline_source": "validation" if baseline else "run_base_revision",
            },
        )
        return changes.changed_files

    def _atlas_commit(self, git: GitRunner, run, head: str, expected: ContentDigest | None) -> bool:
        """HEAD를 이번 publication의 commit으로 채택해도 되는지 확인합니다.

        **metadata만으로는 증명되지 않습니다.** 사람이 같은 branch에서 base+1
        commit을 만들고 subject까지 똑같이 맞출 수 있습니다. 그래서 commit의
        **내용**이 검증한 내용과 같은지 확인합니다.

        기록된 지문이 없으면 채택하지 않습니다(fail closed). 증명할 근거가
        없는데 남의 commit을 우리 것으로 삼는 것보다 다시 판단하는 편이
        안전합니다.
        """

        base = run.base_revision
        if not base or head == base or expected is None or not expected.computed:
            return False
        try:
            if git.run("rev-list", "--count", f"{base}..{head}").text != "1":
                return False
            if git.is_dirty():
                return False
            if git.current_branch() != run.branch:
                return False
            if git.run("log", "-1", "--format=%s", head).text != commit_subject(run.task_id):
                return False
        except (GitError, OSError):
            return False

        actual = safe_content_digest(
            git.cwd, base, head, timeout_seconds=self._config.git_timeout_seconds
        )
        return expected.matches(actual)

    def _recorded_digest(self, publication_id: str) -> ContentDigest | None:
        """이 publication이 검증한 내용의 지문.

        이번 attempt에 아직 없으면 **같은 Run의 이전 attempt**가 남긴 것을
        찾습니다. 앞선 시도가 무결성 확인까지 마치고 commit한 뒤 실패했을 수
        있고, 그 지문은 여전히 유효한 근거입니다.

        어디에도 없으면 `None`입니다. 그 경우 채택하지 않습니다(fail closed).
        """

        row = self._store.publication(publication_id)
        if row is None:
            return None
        candidates = [row]
        candidates.extend(
            other
            for other in self._store.publications(row["run_id"])
            if other["publication_id"] != publication_id
        )
        for candidate in candidates:
            raw = candidate["content_digest"]
            if not raw:
                continue
            try:
                digest = ContentDigest.from_dict(json.loads(raw))
            except (ValueError, TypeError):
                continue
            if digest is not None:
                return digest
        return None

    def _verify_committed(
        self, publication_id: str, run, git: GitRunner, head: str, task: dict[str, Any]
    ) -> tuple[str, ...]:
        """이미 만들어진 commit의 변경 내용을 검증합니다.

        commit된 뒤에도 범위 검사를 건너뛰지 않습니다.
        """

        files = tuple(
            line.strip().replace("\\", "/")
            for line in git.run("show", "--name-only", "--format=", head).lines()
            if line.strip()
        )
        if not files:
            raise PublicationError(
                PublicationFailure.NOTHING_TO_PUBLISH, "commit에 변경이 없습니다."
            )

        from .worktree_changes import _matches, _scope_patterns

        allowed = _scope_patterns((task or {}).get("allowed_scope"))
        forbidden = _scope_patterns((task or {}).get("forbidden_scope"))
        violations: list[str] = []
        if any(path == ".git" or path.startswith(".git/") for path in files):
            violations.append("git_internals_modified")
        if any(any(_matches(path, rule) for rule in forbidden) for path in files):
            violations.append("forbidden_path_changed")
        if allowed and any(
            not any(_matches(path, rule) for rule in allowed) for path in files
        ):
            violations.append("out_of_scope_path_changed")
        if violations:
            raise PublicationError(
                PublicationFailure.WORKSPACE_DRIFT,
                f"허용 범위를 벗어난 변경입니다: {', '.join(violations)}",
                {"violations": violations},
            )

        self._store.update_publication(
            publication_id,
            event="publication_integrity_verified",
            detail={
                "head": head,
                "branch": run.branch,
                "changed_file_count": len(files),
                "baseline_source": "existing_atlas_commit",
                "resumed": True,
            },
        )
        return files

    def _validation_baseline(self, run_id: str) -> dict[str, Any] | None:
        """검증 시점의 worktree 지문을 찾습니다."""

        for row in self._store.events(limit=800):
            if row["run_id"] != run_id:
                continue
            if row["kind"] not in ("publication_integrity_verified", "implementation_completed"):
                continue
            try:
                detail = json.loads(row["detail"])
            except (ValueError, TypeError):
                continue
            fingerprint = detail.get("fingerprint")
            if isinstance(fingerprint, dict) and fingerprint.get("digest"):
                return fingerprint
        return None

    # -- C. commit ----------------------------------------------------------

    def _commit(
        self, publication_id: str, run, task: dict[str, Any], changed_files: tuple[str, ...]
    ) -> tuple[str, bool]:
        """검증이 승인한 경로만 stage해서 commit합니다.

        재시작 시 같은 commit을 다시 만들지 않습니다. 이미 저장된 commit이
        지금 HEAD면 그대로 채택합니다.
        """

        row = self._store.publication(publication_id)
        git = GitRunner(run.worktree_path, timeout_seconds=self._config.git_timeout_seconds)

        head = git.head_revision()
        recorded = (row["commit_sha"] or "") if row else ""
        expected = self._recorded_digest(publication_id)
        if recorded and head == recorded:
            return recorded, True
        if self._atlas_commit(git, run, head, expected):
            # 앞선 시도가 만든 commit입니다. 다시 만들지 않고 채택하고
            # checkpoint만 남깁니다.
            self._store.update_publication(
                publication_id,
                commit_sha=head,
                event="publication_commit_adopted",
                detail={"commit_sha": head, "source": "existing_atlas_commit"},
            )
            return head, True

        before = head
        git.stage_paths(changed_files)

        staged = git.staged_paths()
        approved = set(changed_files)
        if staged != approved:
            raise PublicationError(
                PublicationFailure.STAGE_MISMATCH,
                "stage된 경로가 검증된 경로와 다릅니다.",
                {
                    "unexpected": sorted(staged - approved)[:20],
                    "missing": sorted(approved - staged)[:20],
                },
            )

        message = commit_message(
            run.task_id,
            run.run_id,
            issue_number=_issue_number(task),
            objective=str(task.get("objective") or ""),
        )
        try:
            commit_sha = git.commit(
                message,
                author_name=self._config.author_name,
                author_email=self._config.author_email,
            )
        except GitError as error:
            raise PublicationError(
                PublicationFailure.COMMIT_FAILED,
                f"commit에 실패했습니다: {redact_line(error.stderr if hasattr(error, 'stderr') else str(error), limit=200)}",
            ) from None

        # commit 후 상태를 확인합니다. 한 걸음만 전진해야 합니다.
        parents = git.run("rev-list", "--count", f"{before}..{commit_sha}").text
        if parents != "1":
            raise PublicationError(
                PublicationFailure.COMMIT_FAILED,
                f"commit이 정확히 하나 전진하지 않았습니다: {parents}",
                {"before": before, "after": commit_sha},
            )
        if git.is_dirty():
            raise PublicationError(
                PublicationFailure.COMMIT_FAILED, "commit 후에도 working tree가 깨끗하지 않습니다."
            )
        if git.current_branch() != run.branch:
            raise PublicationError(
                PublicationFailure.COMMIT_FAILED, "commit 후 branch가 바뀌었습니다."
            )

        # 방금 만든 commit이 검증한 내용과 같은지 확인합니다. stage 과정에서
        # 예상치 못한 변환(줄바꿈, filter)이 있었다면 여기서 드러납니다.
        if expected is not None and expected.computed:
            actual = safe_content_digest(
                run.worktree_path,
                run.base_revision,
                commit_sha,
                timeout_seconds=self._config.git_timeout_seconds,
            )
            if not expected.matches(actual):
                raise PublicationError(
                    PublicationFailure.CONTENT_MISMATCH,
                    "만든 commit의 내용이 검증한 내용과 다릅니다.",
                    {"expected_digest": expected.digest, "actual_digest": actual.digest},
                )

        # D. checkpoint. 여기서 죽어도 재시작 시 HEAD를 보고 채택합니다.
        self._store.update_publication(
            publication_id,
            commit_sha=commit_sha,
            event="publication_committed",
            detail={
                "commit_sha": commit_sha,
                "parent": before,
                "staged_file_count": len(approved),
            },
        )
        return commit_sha, False

    # -- F. push ------------------------------------------------------------

    def _push(self, publication_id: str, run, remote: str, commit_sha: str) -> bool:
        """필요할 때만 push합니다. `True`면 이번에 push했습니다."""

        git = GitRunner(run.worktree_path, timeout_seconds=self._config.push_timeout_seconds)
        if _is_protected(run.branch):
            raise PublicationError(
                PublicationFailure.PUSH_FAILED, f"보호 branch에는 push하지 않습니다: {run.branch}"
            )

        # 이름이 아니라 **지금 다시 확인한 대상**으로 조회하고 push합니다.
        url = self._revalidate_remote(run, publication_id)
        target = self._push_target(remote, url)

        try:
            remote_sha = git.remote_head(target, run.branch)
        except GitError as error:
            raise self._push_error(error, "remote 상태를 확인하지 못했습니다.") from None

        if remote_sha == commit_sha:
            # 이미 같은 commit이 올라가 있습니다. 다시 push하지 않습니다.
            self._store.update_publication(
                publication_id,
                pushed_sha=commit_sha,
                event="publication_push_skipped",
                detail={"reason": "remote_already_matches", "commit_sha": commit_sha},
            )
            return False

        if remote_sha:
            # 다른 commit이 있습니다. **force push하지 않습니다.**
            raise PublicationError(
                PublicationFailure.REMOTE_CONFLICT,
                "remote branch가 다른 commit을 가리킵니다. 덮어쓰지 않습니다.",
                {"remote_sha": remote_sha, "local_sha": commit_sha, "branch": run.branch},
            )

        try:
            git.push_branch(target, run.branch, commit_sha)
        except GitError as error:
            raise self._push_error(error, "push에 실패했습니다.") from None

        self._store.update_publication(
            publication_id,
            pushed_sha=commit_sha,
            event="publication_pushed",
            detail={"commit_sha": commit_sha, "branch": run.branch, "remote": remote},
        )
        return True

    @staticmethod
    def _push_error(error: GitError, message: str) -> PublicationError:
        text = str(getattr(error, "stderr", "") or error).lower()
        if any(
            marker in text
            for marker in ("authentication", "permission denied", "could not read username", "403")
        ):
            return PublicationError(
                PublicationFailure.AUTHENTICATION_FAILED,
                "git 인증에 실패했습니다. credential을 확인하세요.",
            )
        return PublicationError(
            PublicationFailure.PUSH_FAILED,
            f"{message} {redact_line(str(getattr(error, 'stderr', '') or ''), limit=200)}",
        )

    # -- I. draft PR --------------------------------------------------------

    def _pull_request(
        self,
        publication_id: str,
        run,
        task: dict[str, Any],
        validation,
        repository: str,
        base_branch: str,
        commit_sha: str,
        changed_files: tuple[str, ...],
        warnings: list[str],
    ) -> tuple[PullRequestRef, bool]:
        """draft PR을 만듭니다. 이미 있으면 채택합니다."""

        row = self._store.publication(publication_id)
        if row and row["pr_number"]:
            return (
                PullRequestRef(
                    number=int(row["pr_number"]),
                    url=row["pr_url"] or "",
                    state=row["pr_state"] or "open",
                    node_id=row["pr_node_id"] or "",
                    head=run.branch,
                    base=base_branch,
                ),
                False,
            )

        existing = self._pull_requests.find_open(repository, run.branch, base_branch)
        if len(existing) > 1:
            raise PublicationError(
                PublicationFailure.PR_CONFLICT,
                f"같은 branch로 열린 PR이 여러 개입니다: {[p.number for p in existing]}",
                {"numbers": [p.number for p in existing]},
            )
        if existing:
            found = existing[0]
            if not found.draft:
                warnings.append(
                    f"이미 열린 PR #{found.number}은 draft가 아닙니다. 상태를 바꾸지 않았습니다."
                )
            self._store.update_publication(
                publication_id,
                pull_request=found.to_dict(),
                event="publication_pr_adopted",
                detail={"pr_number": found.number, "pr_url": found.url},
            )
            return found, False

        summary = self._validation_summary(validation)
        body = pull_request_body(
            task_id=run.task_id,
            run_id=run.run_id,
            repository=repository,
            branch=run.branch,
            base_branch=base_branch,
            commit_sha=commit_sha,
            issue_number=_issue_number(task),
            objective=str(task.get("objective") or ""),
            changed_files=changed_files,
            validation=summary,
            warnings=warnings,
        )
        title = pull_request_title(run.task_id, str(task.get("objective") or ""))

        created = self._pull_requests.create_draft(
            repository, run.branch, base_branch, title, body
        )
        # J. checkpoint. 여기서 죽으면 reconciliation이 head/base로 찾아냅니다.
        self._store.update_publication(
            publication_id,
            pull_request=created.to_dict(),
            event="publication_pr_created",
            detail={"pr_number": created.number, "pr_url": created.url, "draft": created.draft},
        )
        return created, True

    def _validation_summary(self, validation) -> dict[str, Any] | None:
        """PR 본문에 넣을 검증 요약. 로그 전문과 로컬 경로는 넣지 않습니다."""

        if validation is None:
            return None
        steps = []
        for step in self._store.validation_steps(validation["validation_id"]):
            steps.append(
                {
                    "name": step["name"],
                    "kind": step["kind"],
                    "status": step["status"],
                    "required": bool(step["required"]),
                    "reason": step["reason"],
                }
            )
        try:
            warnings = json.loads(validation["warnings"] or "[]")
        except (ValueError, TypeError):
            warnings = []
        trust = None
        try:
            plan = json.loads(validation["plan_json"] or "{}")
            trust = (plan.get("trust") or {}).get("policy")
        except (ValueError, TypeError):
            pass
        return {
            "outcome": validation["outcome"],
            "steps": steps,
            "warnings": warnings,
            "trust_policy": trust,
        }

    # -- 실패 처리 ------------------------------------------------------------

    def _fail(self, publication_id: str, run_id: str, error: PublicationError) -> None:
        """실패를 기록합니다. Run status는 건드리지 않습니다.

        **구현과 검증이 성공했다는 사실은 게시 실패로 바뀌지 않습니다.**
        게시는 operational failure입니다.
        """

        status = (
            PublicationStatus.RECOVERY_REQUIRED
            if error.recoverable
            else PublicationStatus.FAILED
        )
        self._store.update_publication(
            publication_id,
            status=status,
            failure_category=error.failure.value,
            summary=redact_line(error.message, limit=300),
            recovery_evidence=error.evidence,
            event="publication_failed",
            detail={
                "failure": error.failure.value,
                "recoverable": error.recoverable,
                "detail": redact_line(error.message, limit=300),
                **error.evidence,
            },
        )

    def _task_for(self, run) -> dict[str, Any]:
        row = self._store.task_by_fingerprint(run.fingerprint)
        if row is None:
            return {}
        try:
            task = json.loads(row["task_json"])
        except (ValueError, TypeError):
            return {}
        task.setdefault("_issue_number", row["issue_number"])
        return task

    # -- 조회 ----------------------------------------------------------------

    def show(self, run_id: str) -> dict[str, Any]:
        run = self._store.run(run_id)
        rows = []
        for row in self._store.publications(run_id):
            rows.append(
                {
                    "publication_id": row["publication_id"],
                    "status": row["status"],
                    "repository": row["github_repository"],
                    "branch": row["branch"],
                    "base_branch": row["base_branch"],
                    "commit_sha": row["commit_sha"],
                    "pushed_sha": row["pushed_sha"],
                    "pushed_at": row["pushed_at"],
                    "pr_number": row["pr_number"],
                    "pr_url": row["pr_url"],
                    "pr_state": row["pr_state"],
                    "validation_id": row["validation_id"],
                    "failure_category": row["failure_category"],
                    "summary": row["summary"],
                    "warnings": _load_list(row["warnings"]),
                    "adopted": _load_list(row["adopted"]),
                }
            )
        return {
            "run_id": run_id,
            "run_status": run.status.value if run else None,
            "publications": rows,
        }


def _has_userinfo(url: str) -> bool:
    """URL에 credential이 박혀 있는지 확인합니다.

    박혀 있으면 argv에 넣을 수 없습니다. token이 명령줄에 노출됩니다.
    """

    text = (url or "").strip()
    if "://" not in text:
        return "@" in text.split(":", 1)[0]
    authority = text.split("://", 1)[1].split("/", 1)[0]
    return "@" in authority


def _is_protected(branch: str) -> bool:
    name = (branch or "").strip()
    return name in PROTECTED_BRANCHES or name.rsplit("/", 1)[-1] in PROTECTED_BRANCHES


def _issue_number(task: dict[str, Any]) -> int | None:
    source = task.get("source") or {}
    uri = str(source.get("uri") or "")
    if "/issues/" in uri:
        tail = uri.rsplit("/issues/", 1)[-1].strip("/")
        if tail.isdigit():
            return int(tail)
    number = task.get("_issue_number")
    return int(number) if isinstance(number, int) else None


def _load_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []
