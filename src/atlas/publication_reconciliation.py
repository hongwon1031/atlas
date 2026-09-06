"""publication의 외부 상태를 확인하고 판정합니다.

외부 side effect는 DB보다 먼저 일어납니다. 그래서 crash window마다 "DB는
모르는데 이미 일어난 일"이 생깁니다.

| 창 | 이미 일어난 일 | 복구 근거 |
| --- | --- | --- |
| commit 후 저장 전 | local commit | branch HEAD |
| push 후 저장 전 | remote branch | `ls-remote` |
| PR 생성 후 저장 전 | draft PR | head/base 검색 |

**안전하게 확정할 수 있을 때만 채택합니다.** 모호하거나 충돌하면
`RecoveryRequired`로 남기고 사람이 판단합니다. 같은 side effect를 다시
만들지 않습니다.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .github_pr import PullRequestClient
from .gitcmd import GitError, GitRunner
from .publication_models import PublicationFailure, PublicationStatus
from .worktree_changes import ContentDigest, safe_content_digest
from .store import TaskStore, utcnow

DEFAULT_GIT_TIMEOUT_SECONDS = 30.0


class PublicationReconciler:
    """기록된 publication과 실제 외부 상태를 맞춰 봅니다."""

    def __init__(
        self,
        store: TaskStore,
        pull_requests: PullRequestClient | None = None,
        git_timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
        check_remote: bool = True,
    ) -> None:
        self._store = store
        self._pull_requests = pull_requests
        self._git_timeout = git_timeout_seconds
        # network 없이 로컬 근거만 보고 싶을 때 끕니다.
        self._check_remote = check_remote

    def reconcile(self, now: datetime | None = None) -> list[dict[str, Any]]:
        moment = now or utcnow()
        findings: list[dict[str, Any]] = []
        for row in self._store.active_publications():
            findings.append(self._evaluate(row, moment))
        for row in self._store.published_publications():
            finding = self._verify_published(row, moment)
            if finding is not None:
                findings.append(finding)
        return findings

    # -- active ------------------------------------------------------------

    def _evaluate(self, row: Any, moment: datetime) -> dict[str, Any]:
        run = self._store.run(row["run_id"])
        if run is None or not run.worktree_path:
            return self._record(
                row,
                "publication_workspace_missing",
                "publication의 worktree를 찾을 수 없습니다.",
                {"severity": "high"},
                moment,
            )

        git = GitRunner(run.worktree_path, timeout_seconds=self._git_timeout)
        local_head = None
        branch = None
        try:
            local_head = git.head_revision()
            branch = git.current_branch()
        except (GitError, OSError):
            return self._record(
                row,
                "publication_state_ambiguous",
                "worktree 상태를 읽지 못했습니다.",
                {"severity": "high"},
                moment,
            )

        recorded_commit = row["commit_sha"] or ""
        detail: dict[str, Any] = {
            "branch": branch,
            "local_head": local_head,
            "recorded_commit": recorded_commit,
            "status": row["status"],
        }

        if branch != row["branch"]:
            return self._record(
                row,
                "publication_branch_mismatch",
                "worktree branch가 기록과 다릅니다.",
                {**detail, "severity": "high"},
                moment,
            )

        # 예약 시점 remote와 지금 remote가 같은지 확인합니다. URL 자체는
        # 근거에 넣지 않습니다. credential이 박혀 있을 수 있습니다.
        current_url = git.remote_url(row["remote"] or "origin") or ""
        expected_url = row["remote_url"] or ""
        if expected_url and current_url != expected_url:
            return self._record(
                row,
                "publication_remote_changed",
                "예약 이후 remote URL이 바뀌었습니다.",
                {**detail, "url_changed": True, "severity": "high"},
                moment,
            )

        # A. commit은 성공했는데 저장 전에 죽은 경우.
        if not recorded_commit:
            adopted = self._adoptable_commit(row, git, run, local_head)
            if adopted:
                self._store.update_publication(
                    row["publication_id"],
                    commit_sha=adopted,
                    event="publication_commit_adopted",
                    detail={"commit_sha": adopted, "source": "local_branch_head"},
                )
                detail["adopted_commit"] = adopted
                recorded_commit = adopted
            elif local_head != (run.base_revision or ""):
                # base보다 앞서 있는데 우리 것으로 증명하지 못했습니다.
                # 사람이 만든 commit일 수 있으므로 덮어쓰지 않습니다.
                return self._record(
                    row,
                    "publication_content_mismatch",
                    "branch에 검증한 내용과 다른 commit이 있습니다.",
                    {**detail, "severity": "high"},
                    moment,
                )
            else:
                return self._record(
                    row,
                    "publication_not_committed",
                    "아직 commit이 없습니다. 다시 시작할 수 있습니다.",
                    detail,
                    moment,
                )

        if local_head != recorded_commit:
            return self._record(
                row,
                "publication_local_drift",
                "worktree HEAD가 기록된 commit과 다릅니다.",
                {**detail, "severity": "high"},
                moment,
            )

        if not self._check_remote:
            return self._record(
                row, "publication_local_only_check", "remote 확인을 건너뛰었습니다.", detail, moment
            )

        # B. push는 성공했는데 저장 전에 죽은 경우.
        try:
            remote_sha = git.remote_head(row["remote"] or "origin", row["branch"])
        except (GitError, OSError):
            return self._record(
                row,
                "publication_remote_unreachable",
                "remote 상태를 확인하지 못했습니다.",
                detail,
                moment,
            )
        detail["remote_sha"] = remote_sha

        if remote_sha is None:
            return self._record(
                row,
                "publication_remote_branch_missing",
                "remote branch가 없습니다. push를 다시 시도할 수 있습니다.",
                detail,
                moment,
            )
        if remote_sha != recorded_commit:
            # **덮어쓰지 않습니다.** 다른 Run이 같은 branch를 썼을 수 있습니다.
            return self._record(
                row,
                "publication_remote_conflict",
                "remote branch가 다른 commit을 가리킵니다. 덮어쓰지 않습니다.",
                {**detail, "severity": "high"},
                moment,
            )
        if not row["pushed_sha"]:
            self._store.update_publication(
                row["publication_id"],
                pushed_sha=remote_sha,
                event="publication_push_adopted",
                detail={"commit_sha": remote_sha, "source": "remote_ref"},
            )
            detail["adopted_push"] = remote_sha

        # C. PR은 만들었는데 저장 전에 죽은 경우.
        if row["pr_number"]:
            return self._record(
                row, "publication_healthy", "commit·push·PR이 모두 기록돼 있습니다.", detail, moment
            )
        return self._reconcile_pull_request(row, detail, moment)

    def _adoptable_commit(self, row: Any, git: GitRunner, run, local_head: str) -> str | None:
        """local HEAD를 이번 publication의 commit으로 볼 수 있는지 확인합니다.

        **metadata만으로는 증명되지 않습니다.** base+1이고 깨끗하다는 사실은
        사람이 만든 commit도 만족할 수 있습니다. 예약 시점에 저장한 내용
        지문과 commit의 내용이 같은지 확인합니다.

        지문이 없으면 채택하지 않습니다(fail closed). service의 판정과 같은
        근거를 씁니다.
        """

        base = run.base_revision
        expected = self._recorded_digest(row)
        if not base or local_head == base or expected is None:
            return None
        try:
            if git.run("rev-list", "--count", f"{base}..{local_head}").text != "1":
                return None
            if git.is_dirty():
                return None
        except (GitError, OSError):
            return None

        actual = safe_content_digest(
            git.cwd, base, local_head, timeout_seconds=self._git_timeout
        )
        if not expected.matches(actual):
            return None
        return local_head

    @staticmethod
    def _recorded_digest(row: Any) -> ContentDigest | None:
        raw = row["content_digest"] if "content_digest" in row.keys() else None
        if not raw:
            return None
        try:
            return ContentDigest.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            return None

    def _reconcile_pull_request(
        self, row: Any, detail: dict[str, Any], moment: datetime
    ) -> dict[str, Any]:
        if self._pull_requests is None:
            return self._record(
                row,
                "publication_pr_unchecked",
                "PR client가 없어 확인하지 못했습니다.",
                detail,
                moment,
            )
        try:
            found = self._pull_requests.find_open(
                row["github_repository"], row["branch"], row["base_branch"]
            )
        except Exception as error:  # noqa: BLE001
            return self._record(
                row,
                "publication_pr_unreachable",
                f"PR을 확인하지 못했습니다: {type(error).__name__}",
                detail,
                moment,
            )

        if len(found) > 1:
            return self._record(
                row,
                "publication_pr_conflict",
                "같은 branch로 열린 PR이 여러 개입니다.",
                {**detail, "numbers": [p.number for p in found], "severity": "high"},
                moment,
            )
        if not found:
            return self._record(
                row,
                "publication_pr_missing",
                "PR이 아직 없습니다. 다시 만들 수 있습니다.",
                detail,
                moment,
            )

        adopted = found[0]
        self._store.update_publication(
            row["publication_id"],
            pull_request=adopted.to_dict(),
            status=PublicationStatus.PUBLISHED,
            summary=f"reconciliation이 PR #{adopted.number}을 채택했습니다.",
            event="publication_pr_adopted",
            detail={"pr_number": adopted.number, "pr_url": adopted.url, "source": "reconcile"},
        )
        return self._record(
            row,
            "publication_pr_adopted",
            "이미 있던 PR을 채택했습니다. 새로 만들지 않았습니다.",
            {**detail, "pr_number": adopted.number},
            moment,
        )

    # -- published ---------------------------------------------------------

    def _verify_published(self, row: Any, moment: datetime) -> dict[str, Any] | None:
        """Published 기록에 외부 증거가 실제로 남아 있는지 확인합니다.

        DB만 보고 "게시됐다"고 믿지 않습니다. remote branch와 PR이 실제로
        있는지 봅니다. 없으면 사람이 판단해야 합니다.

        **자동으로 고치지 않습니다.** 닫히거나 merge된 PR도 정책이 정해지지
        않았으므로 finding만 남깁니다.
        """

        if not row["pr_number"]:
            return self._record(
                row,
                "publication_published_without_pr",
                "Published인데 PR 번호가 없습니다.",
                {"severity": "high"},
                moment,
            )
        if not self._check_remote:
            return None

        # remote branch 증거
        run = self._store.run(row["run_id"])
        if run is not None and run.worktree_path:
            git = GitRunner(run.worktree_path, timeout_seconds=self._git_timeout)
            try:
                remote_sha = git.remote_head(row["remote"] or "origin", row["branch"])
            except (GitError, OSError):
                remote_sha = None
                return self._record(
                    row,
                    "publication_remote_unreachable",
                    "Published 기록의 remote를 확인하지 못했습니다.",
                    {},
                    moment,
                )
            if remote_sha is None:
                return self._record(
                    row,
                    "publication_remote_evidence_missing",
                    "Published인데 remote branch가 없습니다.",
                    {"severity": "high", "branch": row["branch"]},
                    moment,
                )
            if row["pushed_sha"] and remote_sha != row["pushed_sha"]:
                return self._record(
                    row,
                    "publication_remote_evidence_changed",
                    "Published 이후 remote branch가 다른 commit을 가리킵니다.",
                    {"severity": "high", "remote_sha": remote_sha},
                    moment,
                )

        # PR 증거
        if self._pull_requests is None:
            return None
        try:
            found = self._pull_requests.find_open(
                row["github_repository"], row["branch"], row["base_branch"]
            )
        except Exception as error:  # noqa: BLE001
            return self._record(
                row,
                "publication_pr_unreachable",
                f"Published 기록의 PR을 확인하지 못했습니다: {type(error).__name__}",
                {},
                moment,
            )
        numbers = [p.number for p in found]
        if row["pr_number"] not in numbers:
            # 닫혔거나 merge됐을 수 있습니다. 정책이 없으므로 기록만 합니다.
            return self._record(
                row,
                "publication_pr_no_longer_open",
                "Published 기록의 PR이 더 이상 열려 있지 않습니다. 자동으로 고치지 않습니다.",
                {"pr_number": row["pr_number"], "open_numbers": numbers},
                moment,
            )
        return None

    # -- 기록 ---------------------------------------------------------------

    def _record(
        self,
        row: Any,
        kind: str,
        reason: str,
        detail: dict[str, Any],
        moment: datetime,
    ) -> dict[str, Any]:
        payload = {
            "publication_id": row["publication_id"],
            "run_id": row["run_id"],
            "kind": kind,
            "reason": reason,
            "publication_status": row["status"],
            **detail,
        }
        self._store.record_publication_event(
            row["publication_id"], row["run_id"], kind, payload, now=moment
        )
        if kind in _RECOVERY_KINDS and row["status"] != PublicationStatus.RECOVERY_REQUIRED.value:
            self._store.update_publication(
                row["publication_id"],
                status=PublicationStatus.RECOVERY_REQUIRED,
                failure_category=_RECOVERY_KINDS[kind].value,
                recovery_evidence=payload,
            )
        return payload


# 자동으로 진행할 수 없는 판정. 사람이 외부 상태를 봐야 합니다.
_RECOVERY_KINDS: dict[str, PublicationFailure] = {
    "publication_remote_conflict": PublicationFailure.REMOTE_CONFLICT,
    "publication_content_mismatch": PublicationFailure.CONTENT_MISMATCH,
    "publication_remote_changed": PublicationFailure.REMOTE_CHANGED,
    "publication_pr_conflict": PublicationFailure.PR_CONFLICT,
    "publication_local_drift": PublicationFailure.STATE_AMBIGUOUS,
    "publication_branch_mismatch": PublicationFailure.STATE_AMBIGUOUS,
    "publication_workspace_missing": PublicationFailure.STATE_AMBIGUOUS,
    "publication_state_ambiguous": PublicationFailure.STATE_AMBIGUOUS,
    "publication_published_without_pr": PublicationFailure.STATE_AMBIGUOUS,
}
