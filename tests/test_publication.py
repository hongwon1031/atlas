"""Git publication과 draft PR 생성 테스트.

실제 git과 bare remote를 씁니다. GitHub은 fake client로 대체합니다.
network를 쓰지 않고 실제 GitHub에 side effect를 만들지 않습니다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from atlas.config import RunConfig
from atlas.gitcmd import GitError, GitRunner
from atlas.intake import build_idempotency_key
from atlas.parser import parse_issue_body
from atlas.publication import (
    GitHubRemoteIdentity,
    PublicationConfig,
    PublicationGateFailed,
    PublicationService,
)
from atlas.publication_content import (
    MAX_PR_BODY_CHARS,
    commit_message,
    commit_subject,
    pull_request_body,
    pull_request_title,
)
from atlas.publication_models import (
    PublicationError,
    PublicationFailure,
    PublicationStatus,
    PullRequestRef,
)
from atlas.publication_reconciliation import PublicationReconciler
from atlas.schema import FAILURE_CATEGORIES, RunStatus
from atlas.store import PublicationConflict, TaskStore, SCHEMA_VERSION
from atlas.validation import validate_intake
from atlas.validation_models import ValidationStatus
from atlas.workspace import PROTECTED_BRANCHES, WorkspacePlanner
from atlas.workspace_service import WorkspaceService
from atlas.worktree_changes import content_digest
from tests.fixtures import make_issue

GIT_AVAILABLE = shutil.which("git") is not None
WORKER = "worker-a"
REPOSITORY = "hongwon1031/atlas"


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, shell=False)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class LocalRemoteIdentity:
    """테스트용 remote 검증.

    host 검사만 생략하고 **repository slug는 그대로 확인합니다.** 정책을
    약화하는 것이 아니라 로컬 bare remote로 push 동작을 확인하기 위한
    경계 주입입니다. 운영 경로는 항상 `GitHubRemoteIdentity`를 씁니다.
    """

    def __init__(self, expected: str = REPOSITORY) -> None:
        self.expected = expected
        self.calls: list[tuple[str, str, str]] = []

    def verify(self, remote: str, url: str, expected_repository: str) -> None:
        self.calls.append((remote, url, expected_repository))
        if expected_repository != self.expected:
            raise PublicationError(
                PublicationFailure.REMOTE_INVALID,
                f"repository가 다릅니다: {expected_repository}",
            )


class FakePullRequests:
    """GitHub PR client 대역."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.existing: list[PullRequestRef] = []
        self.find_calls = 0
        self.create_error: Exception | None = None
        self.next_number = 100

    def find_open(self, repository, head_branch, base_branch):
        self.find_calls += 1
        return [
            p
            for p in self.existing
            if p.head == head_branch and p.base == base_branch and p.state == "open"
        ]

    def create_draft(self, repository, head_branch, base_branch, title, body):
        if self.create_error is not None:
            raise self.create_error
        ref = PullRequestRef(
            number=self.next_number,
            url=f"https://github.com/{repository}/pull/{self.next_number}",
            draft=True,
            head=head_branch,
            base=base_branch,
        )
        self.next_number += 1
        self.created.append({"title": title, "body": body, "ref": ref})
        self.existing.append(ref)
        return ref


class CommitContentTest(unittest.TestCase):
    """commit message와 PR 텍스트는 deterministic해야 합니다."""

    def test_subject_is_stable(self):
        self.assertEqual(commit_subject("ATLAS-0042"), "atlas: implement ATLAS-0042")

    def test_message_contains_identifiers_only(self):
        message = commit_message("ATLAS-0042", "run-1", issue_number=7, objective="한 줄 추가")

        self.assertIn("atlas: implement ATLAS-0042", message)
        self.assertIn("Task: ATLAS-0042", message)
        self.assertIn("Run: run-1", message)
        self.assertIn("Issue: #7", message)
        # objective는 git history에 남기지 않습니다. PR에만 둡니다.
        self.assertNotIn("한 줄 추가", message)

    def test_message_is_deterministic(self):
        first = commit_message("ATLAS-0042", "run-1", issue_number=7, objective="x")
        second = commit_message("ATLAS-0042", "run-1", issue_number=7, objective="x")

        self.assertEqual(first, second)

    def test_long_objective_never_reaches_the_message(self):
        message = commit_message("ATLAS-0042", "run-1", objective="가" * 5000)

        self.assertLess(len(message), 200)
        self.assertNotIn("가", message)

    def test_issue_body_is_not_embedded(self):
        message = commit_message(
            "ATLAS-0042", "run-1", objective="첫 줄\n비밀 두 번째 줄\n세 번째"
        )

        self.assertNotIn("\n비밀 두 번째 줄", message)

    def test_secret_in_objective_never_reaches_the_message(self):
        secret = "ghp_" + "S" * 32

        message = commit_message("ATLAS-0042", "run-1", objective=f"token {secret}")

        self.assertNotIn(secret, message)
        self.assertNotIn("token", message)

    def test_title_format(self):
        title = pull_request_title("ATLAS-0042", "README에 한 줄 추가")

        self.assertTrue(title.startswith("[Atlas] ATLAS-0042: "))
        self.assertLessEqual(len(title), 120)

    def test_title_without_objective(self):
        self.assertEqual(pull_request_title("ATLAS-0042"), "[Atlas] ATLAS-0042")


class PullRequestBodyTest(unittest.TestCase):
    def body(self, **overrides):
        payload = {
            "task_id": "ATLAS-0042",
            "run_id": "run-1",
            "repository": REPOSITORY,
            "branch": "atlas/ATLAS-0042/abc",
            "base_branch": "main",
            "commit_sha": "a" * 40,
            "issue_number": 7,
            "objective": "README에 한 줄 추가",
            "changed_files": ("docs/note.md",),
            "validation": {
                "outcome": "passed",
                "steps": [
                    {"name": "unittest", "status": "passed", "required": True},
                    {"name": "ruff", "status": "skipped", "required": False, "reason": "no_contract"},
                ],
                "warnings": ["validation_passed_static_only"],
                "trust_policy": "untrusted",
            },
        }
        payload.update(overrides)
        return pull_request_body(**payload)

    def test_core_identifiers_present(self):
        body = self.body()

        for marker in ("ATLAS-0042", "run-1", REPOSITORY, "atlas/ATLAS-0042/abc", "docs/note.md"):
            self.assertIn(marker, body)

    def test_issue_is_referenced_not_closed(self):
        """자동 close는 정책이 확정되지 않았습니다."""

        body = self.body()

        self.assertIn("Refs #7", body)
        self.assertNotIn("Closes #", body)
        self.assertNotIn("Fixes #", body)

    def test_validation_summary_is_included(self):
        body = self.body()

        self.assertIn("passed", body)
        self.assertIn("unittest", body)
        self.assertIn("validation_passed_static_only", body)
        self.assertIn("untrusted", body)

    def test_human_review_is_stated(self):
        body = self.body()

        self.assertIn("Generated by Atlas", body)
        self.assertIn("사람의 검토와 merge가 필요합니다", body)

    def test_local_paths_are_not_exposed(self):
        body = self.body(
            validation={
                "outcome": "passed",
                "steps": [{"name": "unittest", "status": "passed", "required": True}],
            }
        )

        self.assertNotIn("C:\\", body)
        self.assertNotIn("/logs/", body)
        self.assertNotIn("stdout.log", body)

    def test_body_is_bounded(self):
        body = self.body(changed_files=[f"src/file{i}.py" for i in range(5000)])

        self.assertLessEqual(len(body), MAX_PR_BODY_CHARS)

    def test_secret_in_objective_is_redacted(self):
        secret = "ghp_" + "T" * 32

        body = self.body(objective=f"use {secret} please")

        self.assertNotIn(secret, body)


class RemoteIdentityTest(unittest.TestCase):
    """`origin`을 무조건 믿지 않습니다."""

    def setUp(self):
        self.identity = GitHubRemoteIdentity()

    def accepts(self, url, expected=REPOSITORY):
        self.identity.verify("origin", url, expected)

    def test_https_and_ssh_forms_are_accepted(self):
        for url in (
            "https://github.com/hongwon1031/atlas.git",
            "https://github.com/hongwon1031/atlas",
            "git@github.com:hongwon1031/atlas.git",
            "ssh://git@github.com/hongwon1031/atlas.git",
        ):
            with self.subTest(url=url):
                self.accepts(url)

    def test_lookalike_path_is_rejected(self):
        with self.assertRaises(PublicationError) as caught:
            self.accepts("https://github.com/evil/hongwon1031/atlas.git")

        self.assertIs(caught.exception.failure, PublicationFailure.REMOTE_INVALID)

    def test_other_host_is_rejected(self):
        for url in (
            "https://gitlab.com/hongwon1031/atlas.git",
            "https://github.com.evil.example/hongwon1031/atlas.git",
            "git@evil.example:hongwon1031/atlas.git",
        ):
            with self.subTest(url=url):
                with self.assertRaises(PublicationError):
                    self.accepts(url)

    def test_different_repository_is_rejected(self):
        with self.assertRaises(PublicationError):
            self.accepts("https://github.com/someone/else.git")

    def test_local_path_is_rejected(self):
        with self.assertRaises(PublicationError):
            self.accepts("/tmp/bare.git")

    def test_case_insensitive_slug(self):
        self.accepts("https://github.com/HongWon1031/Atlas.git")


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = PublicationConfig()

        self.assertEqual(config.remote, "origin")
        self.assertEqual(config.author_name, "Atlas")
        self.assertIn("@", config.author_email)

    def test_newline_in_author_is_rejected(self):
        for field in ("author_name", "author_email"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    PublicationConfig(**{field: "a\nb@example.com"})

    def test_empty_remote_is_rejected(self):
        with self.assertRaises(ValueError):
            PublicationConfig(remote="  ")

    def test_env_override(self):
        config = PublicationConfig.from_env(
            {"ATLAS_GIT_REMOTE": "upstream", "ATLAS_COMMIT_AUTHOR_NAME": "Bot"}
        )

        self.assertEqual(config.remote, "upstream")
        self.assertEqual(config.author_name, "Bot")


class TaxonomyTest(unittest.TestCase):
    def test_publication_failures_are_separate_from_run_categories(self):
        for failure in PublicationFailure:
            self.assertNotIn(failure.value, FAILURE_CATEGORIES)

    def test_recovery_failures_are_marked(self):
        for failure, recoverable in (
            (PublicationFailure.REMOTE_CONFLICT, True),
            (PublicationFailure.PR_CONFLICT, True),
            (PublicationFailure.STATE_AMBIGUOUS, True),
            (PublicationFailure.PUSH_FAILED, False),
            (PublicationFailure.COMMIT_FAILED, False),
        ):
            with self.subTest(failure=failure):
                self.assertEqual(PublicationError(failure, "x").recoverable, recoverable)

    def test_status_sets(self):
        self.assertTrue(PublicationStatus.PUBLISHED.is_terminal)
        self.assertTrue(PublicationStatus.FAILED.is_terminal)
        self.assertFalse(PublicationStatus.RECOVERY_REQUIRED.is_terminal)
        self.assertTrue(PublicationStatus.RECOVERY_REQUIRED.is_active)


class SourceSafetyTest(unittest.TestCase):
    """소스 자체에 위험한 수단이 없어야 합니다."""

    def source(self, name):
        root = Path(__file__).resolve().parent.parent / "src" / "atlas"
        return (root / name).read_text(encoding="utf-8")

    def push_function(self):
        """`push_branch` 본문만 떼어 봅니다.

        파일 전체를 grep하면 worktree 제거의 `--force`처럼 무관한 것이 걸립니다.
        확인해야 하는 것은 **push가 force를 쓰지 않는다**는 사실입니다.
        """

        text = self.source("gitcmd.py")
        start = text.index("    def push_branch(")
        end = text.index("    def worktrees(", start)
        return text[start:end]

    def test_push_never_forces(self):
        """설명 문구가 아니라 실제 실행 문장을 봅니다."""

        calls = [
            line.strip()
            for line in self.push_function().splitlines()
            if 'self.run("push"' in line
        ]

        self.assertEqual(len(calls), 1, calls)
        self.assertNotIn("--force", calls[0])
        self.assertNotIn("force-with-lease", calls[0])
        self.assertNotIn("+refs/heads", calls[0])
        self.assertIn(":refs/heads/", calls[0])

    def test_publication_never_forces(self):
        for name in ("publication.py", "publication_reconciliation.py"):
            with self.subTest(name=name):
                for line in self.source(name).splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#") or stripped.startswith("-"):
                        continue
                    self.assertNotIn("--force", stripped)
                    self.assertNotIn("force-with-lease", stripped)

    def test_no_shell_true_call(self):
        for name in ("publication.py", "gitcmd.py", "publication_reconciliation.py"):
            with self.subTest(name=name):
                # 주석이 아니라 실제 호출 인자만 봅니다.
                for line in self.source(name).splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#") or stripped.startswith("-"):
                        continue
                    self.assertNotIn("shell=True", stripped)

    def test_no_add_all_shortcut(self):
        for name in ("publication.py", "gitcmd.py"):
            with self.subTest(name=name):
                text = self.source(name)
                self.assertNotIn('"add", "-A"', text)
                self.assertNotIn('"add", "."', text)


@unittest.skipUnless(GIT_AVAILABLE, "git 실행 파일이 없습니다")
class PublicationTestCase(unittest.TestCase):
    """bare remote를 상대로 실제 commit과 push를 수행합니다."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._dir.name)

        self.bare = self.root / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(self.bare)], check=True, capture_output=True
        )

        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True)
        git("init", "-b", "main", cwd=self.repo)
        git("config", "user.email", "t@e.com", cwd=self.repo)
        git("config", "user.name", "T", cwd=self.repo)
        write(self.repo / "README.md", "base\n")
        write(self.repo / "docs" / "seed.md", "seed\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-m", "base", cwd=self.repo)
        git("remote", "add", "origin", f"https://github.com/{REPOSITORY}.git", cwd=self.repo)
        git("remote", "add", "bare", str(self.bare), cwd=self.repo)

        self.db = str(self.root / "atlas.db")
        self.store = TaskStore(self.db)
        self.planner = WorkspacePlanner(self.repo, self.root / "wt", base_branch="main")
        self.workspaces = WorkspaceService(self.store, self.planner)
        self.pull_requests = FakePullRequests()
        self.identity = LocalRemoteIdentity()
        self.service = self._service()
        self.addCleanup(self._teardown)

        self.run = self.prepare()

    def _service(self, **overrides):
        config = PublicationConfig(remote="bare")
        if "config" in overrides:
            config = overrides.pop("config")
        return PublicationService(
            self.store,
            self.workspaces,
            config,
            RunConfig(),
            pull_requests=overrides.pop("pull_requests", self.pull_requests),
            remote_identity=overrides.pop("remote_identity", self.identity),
        )

    def _teardown(self):
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._dir.cleanup()
        except (PermissionError, OSError):
            pass

    def prepare(self, number=77, changed="docs/note.md", text="구현 결과\n"):
        issue = make_issue(number=number)
        key = build_idempotency_key(issue)
        self.store.register(
            validate_intake(issue, parse_issue_body(issue.body), key),
            key,
            repository=issue.repository,
            issue_number=issue.number,
            labels=issue.labels,
            approved=True,
            approval_signal="queue_label:atlas:queued",
        )
        task_id = f"ATLAS-{number:04d}"
        self.store.claim(WORKER, 3600, task_id=task_id)
        run = self.store.start_run(task_id, WORKER)
        self.workspaces.create(run.run_id)
        run = self.store.run(run.run_id)

        write(Path(run.worktree_path) / changed, text)
        self.store.await_validation(run.run_id)
        self.finish_validation(run.run_id)
        self.store.finish_run(run.run_id, RunStatus.SUCCEEDED)
        return self.store.run(run.run_id)

    def finish_validation(self, run_id, outcome="passed"):
        validation_id = self.store.start_validation(
            run_id,
            worker_id=WORKER,
            cwd=self.store.run(run_id).worktree_path,
            plan={"steps": [], "trust": {"policy": "trusted"}},
        )
        self.store.record_validation_step(
            validation_id,
            run_id,
            position=0,
            name="unittest",
            kind="tests",
            required=True,
            status="passed",
            command=["python"],
            exit_code=0,
        )
        self.store.finish_validation(
            validation_id,
            status=ValidationStatus.FINISHED,
            outcome=outcome,
            summary="passed",
            warnings=["validation_passed_static_only"],
        )
        return validation_id

    def publish(self, run=None):
        target = run or self.run
        return self.service.publish(target.run_id, WORKER)

    def worktree_git(self, run=None):
        return GitRunner((run or self.run).worktree_path)

    def remote_head(self, branch=None, run=None):
        return GitRunner(self.repo).remote_head("bare", branch or (run or self.run).branch)

    def bare_head(self, branch=None, run=None):
        """remote 이름이 바뀐 뒤에도 bare repository를 직접 확인합니다."""

        return GitRunner(self.repo).remote_head(
            str(self.bare), branch or (run or self.run).branch
        )


class HappyPathTest(PublicationTestCase):
    def test_succeeded_run_is_published(self):
        report = self.publish()

        self.assertIs(report.status, PublicationStatus.PUBLISHED)
        self.assertTrue(report.commit_sha)
        self.assertTrue(report.pushed)
        self.assertEqual(report.pull_request.number, 100)

    def test_commit_lands_on_the_expected_branch(self):
        report = self.publish()
        wt = self.worktree_git()

        self.assertEqual(wt.current_branch(), self.run.branch)
        self.assertEqual(wt.head_revision(), report.commit_sha)

    def test_worktree_is_clean_after_commit(self):
        self.publish()

        self.assertFalse(self.worktree_git().is_dirty())

    def test_head_advances_exactly_one_commit(self):
        report = self.publish()
        wt = self.worktree_git()

        distance = wt.run("rev-list", "--count", f"{self.run.base_revision}..{report.commit_sha}")
        self.assertEqual(distance.text, "1")

    def test_only_the_atlas_branch_is_pushed(self):
        report = self.publish()

        self.assertEqual(self.remote_head(), report.commit_sha)
        self.assertIsNone(self.remote_head("main"))

    def test_main_repository_is_untouched(self):
        before = GitRunner(self.repo).head_revision()

        self.publish()

        self.assertEqual(GitRunner(self.repo).head_revision(), before)
        self.assertFalse(GitRunner(self.repo).is_dirty())
        self.assertEqual(GitRunner(self.repo).current_branch(), "main")

    def test_commit_author_is_the_atlas_bot(self):
        report = self.publish()
        wt = self.worktree_git()

        name = wt.run("log", "-1", "--format=%an", report.commit_sha).text
        email = wt.run("log", "-1", "--format=%ae", report.commit_sha).text
        self.assertEqual(name, "Atlas")
        self.assertEqual(email, "atlas@users.noreply.github.com")

    def test_global_git_config_is_untouched(self):
        before = subprocess.run(
            ["git", "config", "--global", "--get", "user.name"],
            capture_output=True, text=True, shell=False,
        ).stdout

        self.publish()

        after = subprocess.run(
            ["git", "config", "--global", "--get", "user.name"],
            capture_output=True, text=True, shell=False,
        ).stdout
        self.assertEqual(before, after)

    def test_staged_paths_match_validated_changes(self):
        report = self.publish()
        wt = self.worktree_git()

        files = wt.run("show", "--name-only", "--format=", report.commit_sha).lines()
        self.assertEqual([f for f in files if f.strip()], ["docs/note.md"])

    def test_durable_linkage_is_queryable(self):
        report = self.publish()

        view = self.service.show(self.run.run_id)
        row = view["publications"][0]
        self.assertEqual(row["status"], PublicationStatus.PUBLISHED.value)
        self.assertEqual(row["commit_sha"], report.commit_sha)
        self.assertEqual(row["pr_number"], 100)
        self.assertTrue(row["pr_url"])
        self.assertTrue(row["validation_id"])

    def test_run_stays_succeeded(self):
        self.publish()

        self.assertIs(self.store.run(self.run.run_id).status, RunStatus.SUCCEEDED)

    def test_pr_is_draft(self):
        report = self.publish()

        self.assertTrue(report.pull_request.draft)
        self.assertEqual(len(self.pull_requests.created), 1)


class GateTest(PublicationTestCase):
    def assert_rejected(self, check):
        with self.assertRaises(PublicationGateFailed) as caught:
            self.publish()
        self.assertIn(check, caught.exception.failed_checks)
        self.assertIsNone(self.store.active_publication(self.run.run_id))

    def test_non_succeeded_run_is_rejected(self):
        other = self.prepare(number=78)
        self.store._connection.execute(
            "UPDATE runs SET status = 'Running' WHERE run_id = ?", (other.run_id,)
        )
        self.store._connection.commit()

        with self.assertRaises(PublicationGateFailed) as caught:
            self.service.publish(other.run_id, WORKER)

        self.assertIn("run_succeeded", caught.exception.failed_checks)

    def test_approval_revocation_blocks_publication(self):
        self.store.revoke_approval(self.run.task_id, "회수")

        self.assert_rejected("task_approved")

    def test_claim_loss_blocks_publication(self):
        claim = self.store.claim_for(self.run.claim_id)
        self.store.release(claim["claim_id"], "해제")

        self.assert_rejected("claim_active")

    def test_other_worker_is_rejected(self):
        with self.assertRaises(PublicationGateFailed) as caught:
            self.service.publish(self.run.run_id, "worker-b")

        self.assertIn("claim_owner_matches", caught.exception.failed_checks)

    def test_missing_workspace_blocks_publication(self):
        shutil.rmtree(self.run.worktree_path, ignore_errors=True)

        self.assert_rejected("workspace_valid")

    def test_failed_validation_blocks_publication(self):
        other = self.prepare(number=79)
        self.store._connection.execute(
            "UPDATE validations SET outcome = 'failed' WHERE run_id = ?", (other.run_id,)
        )
        self.store._connection.commit()

        with self.assertRaises(PublicationGateFailed) as caught:
            self.service.publish(other.run_id, WORKER)

        self.assertIn("validation_passed", caught.exception.failed_checks)

    def test_gate_failure_is_recorded(self):
        self.store.revoke_approval(self.run.task_id, "회수")
        try:
            self.publish()
        except PublicationGateFailed:
            pass

        kinds = [row["kind"] for row in self.store.events()]
        self.assertIn("publication_gate_failed", kinds)


class IntegrityTest(PublicationTestCase):
    def test_change_after_validation_blocks_publication(self):
        write(Path(self.run.worktree_path) / "docs" / "note.md", "사람이 수정\n")
        # 검증 시점 지문을 남깁니다.
        self.store.record_publication_event(
            None,
            self.run.run_id,
            "implementation_completed",
            {"fingerprint": {"digest": "0" * 64, "head": self.run.base_revision}},
        )

        with self.assertRaises(PublicationError) as caught:
            self.publish()

        self.assertIs(caught.exception.failure, PublicationFailure.WORKSPACE_DRIFT)
        self.assertEqual(len(self.pull_requests.created), 0)

    def test_new_commit_after_validation_blocks_publication(self):
        wt = self.worktree_git()
        git("add", "-A", cwd=Path(self.run.worktree_path))
        git(
            "-c", "user.email=t@e.com", "-c", "user.name=T",
            "commit", "-m", "사람이 만든 commit", cwd=Path(self.run.worktree_path),
        )

        with self.assertRaises(PublicationError) as caught:
            self.publish()

        self.assertIs(caught.exception.failure, PublicationFailure.WORKSPACE_DRIFT)
        self.assertIsNone(self.remote_head())

    def test_out_of_scope_change_blocks_publication(self):
        other = self.prepare(number=80, changed="unexpected.txt", text="범위 밖\n")

        with self.assertRaises(PublicationError) as caught:
            self.service.publish(other.run_id, WORKER)

        self.assertIs(caught.exception.failure, PublicationFailure.WORKSPACE_DRIFT)

    def test_nothing_to_publish_is_its_own_failure(self):
        other = self.prepare(number=81)
        (Path(other.worktree_path) / "docs" / "note.md").unlink()

        with self.assertRaises(PublicationError) as caught:
            self.service.publish(other.run_id, WORKER)

        self.assertIs(caught.exception.failure, PublicationFailure.NOTHING_TO_PUBLISH)

    def test_branch_switch_blocks_publication(self):
        worktree = Path(self.run.worktree_path)
        git("stash", "-u", cwd=worktree)
        git("checkout", "-q", "-b", "atlas/other/9999", cwd=worktree)

        with self.assertRaises((PublicationError, PublicationGateFailed)):
            self.publish()

        self.assertIsNone(self.remote_head())


class ProtectedBranchTest(PublicationTestCase):
    def test_protected_branch_is_never_published(self):
        for branch in sorted(PROTECTED_BRANCHES):
            with self.subTest(branch=branch):
                self.store._connection.execute(
                    "UPDATE runs SET branch = ? WHERE run_id = ?", (branch, self.run.run_id)
                )
                self.store._connection.commit()

                with self.assertRaises((PublicationGateFailed, PublicationError)):
                    self.publish()

                self.assertIsNone(self.remote_head("main"))
                self.assertIsNone(self.remote_head("master"))


class IdempotencyTest(PublicationTestCase):
    def test_second_publish_is_rejected(self):
        self.publish()

        with self.assertRaises(PublicationGateFailed) as caught:
            self.publish()

        self.assertIn("not_already_published", caught.exception.failed_checks)
        self.assertEqual(len(self.pull_requests.created), 1)

    def test_duplicate_start_is_blocked_by_the_database(self):
        self.store.start_publication(
            self.run.run_id,
            worker_id=WORKER,
            github_repository=REPOSITORY,
            branch=self.run.branch,
            base_branch="main",
            remote="bare",
            remote_url=str(self.bare),
        )

        with self.assertRaises(PublicationConflict) as caught:
            self.store.start_publication(
                self.run.run_id,
                worker_id=WORKER,
                github_repository=REPOSITORY,
                branch=self.run.branch,
                base_branch="main",
                remote="bare",
                remote_url=str(self.bare),
            )

        self.assertEqual(caught.exception.category, "publication_already_active")

    def test_retry_after_failure_adopts_everything(self):
        """앞선 attempt가 남긴 지문을 근거로 재시도가 채택합니다."""

        report = self.publish()
        # 첫 attempt를 실패로 되돌려 재시도 가능한 상태로 만듭니다. 내용
        # 지문은 그대로 남습니다.
        self.store._connection.execute(
            "UPDATE publications SET status = 'Failed' WHERE run_id = ?", (self.run.run_id,)
        )
        self.store._connection.commit()

        again = self.publish()

        self.assertEqual(again.commit_sha, report.commit_sha)
        self.assertIn("commit", again.adopted)
        self.assertIn("remote_branch", again.adopted)
        self.assertIn("pull_request", again.adopted)
        self.assertEqual(len(self.pull_requests.created), 1)

    def test_retry_without_any_recorded_digest_is_refused(self):
        """증명할 근거가 없으면 채택하지 않습니다."""

        self.publish()
        self.store._connection.execute(
            "UPDATE publications SET status = 'Failed', content_digest = NULL "
            "WHERE run_id = ?",
            (self.run.run_id,),
        )
        self.store._connection.commit()

        with self.assertRaises(PublicationError) as caught:
            self.publish()

        self.assertIs(caught.exception.failure, PublicationFailure.WORKSPACE_DRIFT)

    def test_existing_open_pr_is_adopted(self):
        self.pull_requests.existing.append(
            PullRequestRef(
                number=55,
                url="https://github.com/x/y/pull/55",
                draft=True,
                head=self.run.branch,
                base="main",
            )
        )

        report = self.publish()

        self.assertEqual(report.pull_request.number, 55)
        self.assertEqual(len(self.pull_requests.created), 0)
        self.assertIn("pull_request", report.adopted)

    def test_multiple_matching_prs_require_recovery(self):
        for number in (55, 56):
            self.pull_requests.existing.append(
                PullRequestRef(
                    number=number,
                    url=f"https://github.com/x/y/pull/{number}",
                    head=self.run.branch,
                    base="main",
                )
            )

        with self.assertRaises(PublicationError) as caught:
            self.publish()

        self.assertIs(caught.exception.failure, PublicationFailure.PR_CONFLICT)
        row = self.store.publications(self.run.run_id)[0]
        self.assertEqual(row["status"], PublicationStatus.RECOVERY_REQUIRED.value)

    def test_non_draft_existing_pr_is_adopted_with_a_warning(self):
        self.pull_requests.existing.append(
            PullRequestRef(
                number=57,
                url="https://github.com/x/y/pull/57",
                draft=False,
                head=self.run.branch,
                base="main",
            )
        )

        report = self.publish()

        self.assertEqual(report.pull_request.number, 57)
        self.assertTrue(any("draft" in w for w in report.warnings))

    def test_closed_pr_is_not_adopted(self):
        self.pull_requests.existing.append(
            PullRequestRef(
                number=58,
                url="https://github.com/x/y/pull/58",
                state="closed",
                head=self.run.branch,
                base="main",
            )
        )

        report = self.publish()

        self.assertEqual(report.pull_request.number, 100)
        self.assertEqual(len(self.pull_requests.created), 1)


class RemoteConflictTest(PublicationTestCase):
    def test_different_remote_commit_is_never_overwritten(self):
        # 다른 내용으로 같은 branch를 remote에 먼저 올려 둡니다.
        other = self.root / "intruder"
        subprocess.run(
            ["git", "clone", "-q", str(self.bare), str(other)], check=True, capture_output=True
        )
        git("config", "user.email", "x@e.com", cwd=other)
        git("config", "user.name", "X", cwd=other)
        git("checkout", "-q", "-b", self.run.branch, cwd=other)
        write(other / "intruder.txt", "다른 사람\n")
        git("add", "-A", cwd=other)
        git("commit", "-m", "intruder", cwd=other)
        git("push", "-q", "origin", self.run.branch, cwd=other)
        intruder_sha = GitRunner(other).head_revision()

        with self.assertRaises(PublicationError) as caught:
            self.publish()

        self.assertIs(caught.exception.failure, PublicationFailure.REMOTE_CONFLICT)
        # remote는 그대로여야 합니다.
        self.assertEqual(self.remote_head(), intruder_sha)
        row = self.store.publications(self.run.run_id)[0]
        self.assertEqual(row["status"], PublicationStatus.RECOVERY_REQUIRED.value)
        self.assertEqual(len(self.pull_requests.created), 0)


class PullRequestFailureTest(PublicationTestCase):
    def test_pr_creation_failure_leaves_the_push_recorded(self):
        self.pull_requests.create_error = PublicationError(
            PublicationFailure.PR_CREATE_FAILED, "GitHub 오류"
        )

        with self.assertRaises(PublicationError):
            self.publish()

        row = self.store.publications(self.run.run_id)[0]
        self.assertEqual(row["status"], PublicationStatus.FAILED.value)
        self.assertTrue(row["commit_sha"])
        self.assertTrue(row["pushed_sha"])
        self.assertEqual(row["failure_category"], PublicationFailure.PR_CREATE_FAILED.value)

    def test_authentication_failure_is_classified(self):
        self.pull_requests.create_error = PublicationError(
            PublicationFailure.AUTHENTICATION_FAILED, "인증 실패"
        )

        with self.assertRaises(PublicationError):
            self.publish()

        row = self.store.publications(self.run.run_id)[0]
        self.assertEqual(
            row["failure_category"], PublicationFailure.AUTHENTICATION_FAILED.value
        )
        self.assertIs(self.store.run(self.run.run_id).status, RunStatus.SUCCEEDED)


class CrashWindowTest(PublicationTestCase):
    """외부 side effect는 성공했는데 DB 저장 전에 죽는 경우."""

    def reconciler(self, **kwargs):
        kwargs.setdefault("pull_requests", self.pull_requests)
        return PublicationReconciler(self.store, **kwargs)

    def start_publication(self, with_digest=True):
        """실제 중단 상황을 흉내 냅니다.

        무결성 확인까지 마친 뒤 중단됐다면 내용 지문이 저장돼 있습니다.
        `with_digest=False`는 지문이 없을 때 채택하지 않는지 보기 위한 것입니다.
        """

        publication_id = self.store.start_publication(
            self.run.run_id,
            worker_id=WORKER,
            github_repository=REPOSITORY,
            branch=self.run.branch,
            base_branch="main",
            remote="bare",
            remote_url=str(self.bare),
        )
        if with_digest:
            digest = content_digest(self.run.worktree_path, self.run.base_revision)
            self.store.update_publication(publication_id, content_digest=digest.to_dict())
        return publication_id

    def make_commit(self):
        worktree = Path(self.run.worktree_path)
        wt = GitRunner(worktree)
        wt.stage_paths(("docs/note.md",))
        return wt.commit(
            "atlas: implement test",
            author_name="Atlas",
            author_email="atlas@users.noreply.github.com",
        )

    def test_commit_without_checkpoint_is_adopted(self):
        publication_id = self.start_publication()
        commit_sha = self.make_commit()

        findings = self.reconciler(check_remote=False).reconcile()

        row = self.store.publication(publication_id)
        self.assertEqual(row["commit_sha"], commit_sha)
        self.assertTrue(any(f["kind"] == "publication_local_only_check" for f in findings))

    def test_push_without_checkpoint_is_adopted(self):
        publication_id = self.start_publication()
        commit_sha = self.make_commit()
        GitRunner(self.run.worktree_path).push_branch("bare", self.run.branch, commit_sha)

        self.reconciler().reconcile()

        row = self.store.publication(publication_id)
        self.assertEqual(row["pushed_sha"], commit_sha)

    def test_pr_without_checkpoint_is_adopted_not_recreated(self):
        publication_id = self.start_publication()
        commit_sha = self.make_commit()
        GitRunner(self.run.worktree_path).push_branch("bare", self.run.branch, commit_sha)
        self.pull_requests.existing.append(
            PullRequestRef(
                number=77,
                url="https://github.com/x/y/pull/77",
                draft=True,
                head=self.run.branch,
                base="main",
            )
        )

        findings = self.reconciler().reconcile()

        row = self.store.publication(publication_id)
        self.assertEqual(row["pr_number"], 77)
        self.assertEqual(row["status"], PublicationStatus.PUBLISHED.value)
        self.assertEqual(len(self.pull_requests.created), 0)
        self.assertTrue(any(f["kind"] == "publication_pr_adopted" for f in findings))

    def test_no_commit_yet_is_restartable(self):
        self.start_publication()

        findings = self.reconciler(check_remote=False).reconcile()

        self.assertTrue(any(f["kind"] == "publication_not_committed" for f in findings))

    def test_remote_branch_missing_is_restartable(self):
        self.start_publication()
        self.make_commit()

        findings = self.reconciler().reconcile()

        self.assertTrue(
            any(f["kind"] == "publication_remote_branch_missing" for f in findings)
        )

    def test_remote_conflict_moves_to_recovery(self):
        publication_id = self.start_publication()
        commit_sha = self.make_commit()
        self.store.update_publication(publication_id, commit_sha=commit_sha)
        # remote에 다른 commit을 올려 둡니다.
        other = self.root / "intruder2"
        subprocess.run(
            ["git", "clone", "-q", str(self.bare), str(other)], check=True, capture_output=True
        )
        git("config", "user.email", "x@e.com", cwd=other)
        git("config", "user.name", "X", cwd=other)
        git("checkout", "-q", "-b", self.run.branch, cwd=other)
        write(other / "x.txt", "x\n")
        git("add", "-A", cwd=other)
        git("commit", "-m", "x", cwd=other)
        git("push", "-q", "origin", self.run.branch, cwd=other)
        intruder = GitRunner(other).head_revision()

        findings = self.reconciler().reconcile()

        self.assertTrue(any(f["kind"] == "publication_remote_conflict" for f in findings))
        row = self.store.publication(publication_id)
        self.assertEqual(row["status"], PublicationStatus.RECOVERY_REQUIRED.value)
        self.assertEqual(self.remote_head(), intruder)

    def test_local_drift_moves_to_recovery(self):
        publication_id = self.start_publication()
        self.store.update_publication(publication_id, commit_sha="b" * 40)

        findings = self.reconciler(check_remote=False).reconcile()

        self.assertTrue(any(f["kind"] == "publication_local_drift" for f in findings))
        row = self.store.publication(publication_id)
        self.assertEqual(row["status"], PublicationStatus.RECOVERY_REQUIRED.value)

    def test_reconciliation_never_creates_side_effects(self):
        self.start_publication()
        self.make_commit()

        self.reconciler().reconcile()

        self.assertEqual(len(self.pull_requests.created), 0)
        self.assertIsNone(self.remote_head())


class ContentBoundAdoptionTest(PublicationTestCase):
    """commit 채택은 metadata가 아니라 내용에 근거해야 합니다."""

    def reconciler(self, **kwargs):
        kwargs.setdefault("pull_requests", self.pull_requests)
        return PublicationReconciler(self.store, **kwargs)

    def start_with_digest(self):
        publication_id = self.store.start_publication(
            self.run.run_id,
            worker_id=WORKER,
            github_repository=REPOSITORY,
            branch=self.run.branch,
            base_branch="main",
            remote="bare",
            remote_url=str(self.bare),
        )
        digest = content_digest(self.run.worktree_path, self.run.base_revision)
        self.store.update_publication(publication_id, content_digest=digest.to_dict())
        return publication_id

    def human_commit(self, text, subject=None):
        """사람이 같은 branch에서 base+1 commit을 만듭니다."""

        worktree = Path(self.run.worktree_path)
        write(worktree / "docs" / "note.md", text)
        git("add", "-A", cwd=worktree)
        git(
            "-c", "user.email=h@e.com", "-c", "user.name=H",
            "commit", "-m", subject or commit_subject(self.run.task_id), cwd=worktree,
        )
        return GitRunner(worktree).head_revision()

    def atlas_commit(self):
        """Atlas가 만들었을 commit과 정확히 같은 내용으로 commit합니다."""

        worktree = Path(self.run.worktree_path)
        wt = GitRunner(worktree)
        wt.stage_paths(("docs/note.md",))
        return wt.commit(
            commit_message(self.run.task_id, self.run.run_id),
            author_name="Atlas",
            author_email="atlas@users.noreply.github.com",
        )

    def test_human_commit_with_matching_subject_is_rejected(self):
        """subject를 똑같이 맞춰도 내용이 다르면 채택하지 않습니다."""

        publication_id = self.start_with_digest()
        head = self.human_commit("사람이 쓴 다른 내용\n")
        self.store._connection.execute(
            "UPDATE publications SET status = 'Failed' WHERE publication_id = ?",
            (publication_id,),
        )
        self.store._connection.commit()

        with self.assertRaises(PublicationError) as caught:
            self.publish()

        self.assertIs(caught.exception.failure, PublicationFailure.CONTENT_MISMATCH)
        self.assertIsNone(self.remote_head())
        self.assertEqual(len(self.pull_requests.created), 0)

    def test_human_commit_in_allowed_paths_is_still_rejected(self):
        """경로가 허용 범위 안이어도 내용이 다르면 채택하지 않습니다."""

        publication_id = self.start_with_digest()
        self.human_commit("허용 경로지만 다른 내용\n")

        findings = self.reconciler(check_remote=False).reconcile()

        self.assertTrue(
            any(f["kind"] == "publication_content_mismatch" for f in findings), findings
        )
        row = self.store.publication(publication_id)
        self.assertIsNone(row["commit_sha"])
        self.assertEqual(row["status"], PublicationStatus.RECOVERY_REQUIRED.value)

    def test_exact_atlas_commit_is_adopted(self):
        publication_id = self.start_with_digest()
        head = self.atlas_commit()

        findings = self.reconciler(check_remote=False).reconcile()

        row = self.store.publication(publication_id)
        self.assertEqual(row["commit_sha"], head)
        self.assertTrue(any(f["kind"] == "publication_local_only_check" for f in findings))

    def test_rename_delete_untracked_content_is_comparable(self):
        """rename·삭제·untracked가 섞여도 commit 전후 지문이 같아야 합니다."""

        worktree = Path(self.run.worktree_path)
        write(worktree / "docs" / "renamed.md", "옮긴 내용\n")
        (worktree / "docs" / "note.md").unlink()
        write(worktree / "src" / "added.py", "VALUE = 1\n")
        before = content_digest(worktree, self.run.base_revision)

        wt = GitRunner(worktree)
        wt.stage_paths(("docs/renamed.md", "docs/note.md", "src/added.py"))
        head = wt.commit(
            commit_message(self.run.task_id, self.run.run_id),
            author_name="Atlas",
            author_email="atlas@users.noreply.github.com",
        )
        after = content_digest(worktree, self.run.base_revision, head)

        self.assertTrue(before.matches(after))
        self.assertEqual(before.entry_count, after.entry_count)

    def test_different_content_produces_a_different_digest(self):
        worktree = Path(self.run.worktree_path)
        first = content_digest(worktree, self.run.base_revision)
        write(worktree / "docs" / "note.md", "다른 내용\n")
        second = content_digest(worktree, self.run.base_revision)

        self.assertFalse(first.matches(second))

    def test_no_raw_source_in_digest_or_database(self):
        marker = "VERY-DISTINCTIVE-SOURCE-LINE"
        write(Path(self.run.worktree_path) / "docs" / "note.md", f"{marker}\n")
        digest = content_digest(self.run.worktree_path, self.run.base_revision)
        publication_id = self.start_with_digest()
        self.store.update_publication(publication_id, content_digest=digest.to_dict())

        blob = json.dumps(digest.to_dict(), ensure_ascii=False)
        blob += json.dumps([dict(r) for r in self.store.events()], ensure_ascii=False)
        blob += Path(self.db).read_bytes().decode("latin-1")
        self.assertNotIn(marker, blob)
        self.assertEqual(len(digest.digest), 64)

    def test_digest_is_checkpointed_during_publish(self):
        self.publish()

        row = self.store.publications(self.run.run_id)[0]
        self.assertTrue(row["content_digest"])
        stored = json.loads(row["content_digest"])
        self.assertEqual(len(stored["digest"]), 64)


class RemoteSwapTest(PublicationTestCase):
    """예약 이후 remote가 바뀌면 push하지 않습니다."""

    def swap_remote(self, url):
        git("remote", "set-url", "bare", url, cwd=Path(self.run.worktree_path))

    def publish_with_swap(self, url):
        original = self.service._revalidate_remote

        def swapping(run, publication_id):
            # 예약과 검증 사이가 아니라, 검증 직전에 바꿔치기합니다.
            self.swap_remote(url)
            return original(run, publication_id)

        self.service._revalidate_remote = swapping
        try:
            return self.publish()
        finally:
            self.service._revalidate_remote = original

    def test_remote_url_change_blocks_push(self):
        other = self.root / "other.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(other)], check=True, capture_output=True
        )

        with self.assertRaises(PublicationError) as caught:
            self.publish_with_swap(str(other))

        self.assertIs(caught.exception.failure, PublicationFailure.REMOTE_CHANGED)
        self.assertIsNone(self.bare_head())
        self.assertIsNone(GitRunner(self.repo).remote_head(str(other), self.run.branch))

    def test_lookalike_github_url_blocks_push(self):
        with self.assertRaises(PublicationError) as caught:
            self.publish_with_swap("https://github.com/evil/hongwon1031/atlas.git")

        self.assertIn(
            caught.exception.failure,
            (PublicationFailure.REMOTE_CHANGED, PublicationFailure.REMOTE_INVALID),
        )
        self.assertIsNone(self.bare_head())

    def test_different_owner_repo_blocks_push(self):
        with self.assertRaises(PublicationError) as caught:
            self.publish_with_swap("https://github.com/someone/else.git")

        self.assertIn(
            caught.exception.failure,
            (PublicationFailure.REMOTE_CHANGED, PublicationFailure.REMOTE_INVALID),
        )
        self.assertIsNone(self.bare_head())

    def test_unchanged_remote_publishes_normally(self):
        report = self.publish()

        self.assertIs(report.status, PublicationStatus.PUBLISHED)
        self.assertEqual(self.remote_head(), report.commit_sha)

    def test_identity_is_reverified_before_push(self):
        """주입한 identity 구현이 push 직전에도 호출돼야 합니다."""

        before = len(self.identity.calls)

        self.publish()

        self.assertGreater(len(self.identity.calls), before + 0)
        self.assertGreaterEqual(len(self.identity.calls), 2)

    def test_reconciliation_reports_remote_change(self):
        publication_id = self.store.start_publication(
            self.run.run_id,
            worker_id=WORKER,
            github_repository=REPOSITORY,
            branch=self.run.branch,
            base_branch="main",
            remote="bare",
            remote_url=str(self.bare),
        )
        self.swap_remote("https://github.com/someone/else.git")

        findings = PublicationReconciler(
            self.store, pull_requests=self.pull_requests
        ).reconcile()

        self.assertTrue(any(f["kind"] == "publication_remote_changed" for f in findings))
        row = self.store.publication(publication_id)
        self.assertEqual(row["status"], PublicationStatus.RECOVERY_REQUIRED.value)


class AuthorizationRaceTest(PublicationTestCase):
    """예약 이후에도 승인과 claim을 다시 확인합니다."""

    def sabotage_before_push(self, action):
        original = self.service._push

        def wrapped(publication_id, run, remote, commit_sha):
            raise AssertionError("push가 호출되면 안 됩니다")

        # 실제로는 _require_authorization이 먼저 막아야 하므로, push가 불리면
        # 테스트가 실패합니다.
        action()
        self.service._push = wrapped
        try:
            return self.publish()
        finally:
            self.service._push = original

    def test_approval_revoked_after_reservation_blocks_push(self):
        original = self.service._commit

        def revoke_then_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.revoke_approval(self.run.task_id, "회수")
            return result

        self.service._commit = revoke_then_commit
        with self.assertRaises(PublicationError) as caught:
            self.publish()
        self.service._commit = original

        self.assertIs(caught.exception.failure, PublicationFailure.AUTHORIZATION_LOST)
        self.assertIsNone(self.remote_head())
        self.assertEqual(len(self.pull_requests.created), 0)

    def test_claim_release_after_reservation_blocks_push(self):
        original = self.service._commit

        def release_then_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            claim = self.store.claim_for(self.run.claim_id)
            self.store.release(claim["claim_id"], "해제")
            return result

        self.service._commit = release_then_commit
        with self.assertRaises(PublicationError) as caught:
            self.publish()
        self.service._commit = original

        self.assertIs(caught.exception.failure, PublicationFailure.AUTHORIZATION_LOST)
        self.assertIsNone(self.remote_head())

    def test_lease_expiry_between_commit_and_push_blocks_push(self):
        original = self.service._commit

        def expire_then_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store._connection.execute(
                "UPDATE claims SET lease_expires_at = ? WHERE claim_id = ?",
                ("2000-01-01T00:00:00Z", self.run.claim_id),
            )
            self.store._connection.commit()
            return result

        self.service._commit = expire_then_commit
        with self.assertRaises(PublicationError) as caught:
            self.publish()
        self.service._commit = original

        self.assertIs(caught.exception.failure, PublicationFailure.AUTHORIZATION_LOST)
        self.assertIsNone(self.remote_head())

    def test_approval_revoked_after_push_blocks_pr_creation(self):
        """이미 push된 branch는 되돌리지 않고 recovery로 넘깁니다."""

        original = self.service._push

        def revoke_after_push(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.revoke_approval(self.run.task_id, "회수")
            return result

        self.service._push = revoke_after_push
        with self.assertRaises(PublicationError) as caught:
            self.publish()
        self.service._push = original

        self.assertIs(caught.exception.failure, PublicationFailure.AUTHORIZATION_LOST)
        # PR은 만들지 않았습니다.
        self.assertEqual(len(self.pull_requests.created), 0)
        # push는 이미 일어났고 되돌리지 않았습니다.
        row = self.store.publications(self.run.run_id)[0]
        self.assertTrue(row["pushed_sha"])
        self.assertEqual(self.remote_head(), row["pushed_sha"])
        self.assertTrue(caught.exception.evidence["side_effects_exist"])

    def test_owner_mismatch_after_reservation_blocks_side_effects(self):
        original = self.service._commit

        def steal_then_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store._connection.execute(
                "UPDATE claims SET lease_owner = 'worker-b' WHERE claim_id = ?",
                (self.run.claim_id,),
            )
            self.store._connection.commit()
            return result

        self.service._commit = steal_then_commit
        with self.assertRaises(PublicationError) as caught:
            self.publish()
        self.service._commit = original

        self.assertIs(caught.exception.failure, PublicationFailure.AUTHORIZATION_LOST)
        self.assertIsNone(self.remote_head())

    def test_authorization_loss_is_recorded(self):
        original = self.service._commit

        def revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.revoke_approval(self.run.task_id, "회수")
            return result

        self.service._commit = revoke
        try:
            self.publish()
        except PublicationError:
            pass
        self.service._commit = original

        kinds = [row["kind"] for row in self.store.events()]
        self.assertIn("publication_authorization_lost", kinds)

    def test_normal_path_is_unaffected(self):
        report = self.publish()

        self.assertIs(report.status, PublicationStatus.PUBLISHED)
        self.assertEqual(len(self.pull_requests.created), 1)

    def test_authorization_checks_exclude_context_dependent_items(self):
        run = self.store.run(self.run.run_id)
        checks = self.service.authorization_checks(run, WORKER)

        self.assertNotIn("not_already_published", checks)
        self.assertNotIn("run_succeeded", checks)
        self.assertNotIn("no_active_validation", checks)
        self.assertTrue(all(checks.values()))


class PublishedEvidenceTest(PublicationTestCase):
    """Published 기록에 외부 증거가 실제로 있는지 확인합니다."""

    def reconciler(self, **kwargs):
        kwargs.setdefault("pull_requests", self.pull_requests)
        return PublicationReconciler(self.store, **kwargs)

    def test_healthy_published_record_produces_no_finding(self):
        self.publish()

        findings = self.reconciler().reconcile()

        self.assertEqual(findings, [])

    def test_missing_remote_branch_is_reported(self):
        report = self.publish()
        # remote에서 branch를 지웁니다.
        subprocess.run(
            ["git", "-C", str(self.bare), "update-ref", "-d", f"refs/heads/{self.run.branch}"],
            check=True, capture_output=True, shell=False,
        )

        findings = self.reconciler().reconcile()

        self.assertTrue(
            any(f["kind"] == "publication_remote_evidence_missing" for f in findings), findings
        )

    def test_changed_remote_branch_is_reported(self):
        self.publish()
        other = self.root / "mover"
        subprocess.run(
            ["git", "clone", "-q", str(self.bare), str(other)], check=True, capture_output=True
        )
        git("config", "user.email", "m@e.com", cwd=other)
        git("config", "user.name", "M", cwd=other)
        git("checkout", "-q", self.run.branch, cwd=other)
        write(other / "extra.txt", "다른 사람\n")
        git("add", "-A", cwd=other)
        git("commit", "-m", "mover", cwd=other)
        git("push", "-q", "origin", self.run.branch, cwd=other)

        findings = self.reconciler().reconcile()

        self.assertTrue(
            any(f["kind"] == "publication_remote_evidence_changed" for f in findings), findings
        )

    def test_closed_pr_is_reported_not_fixed(self):
        report = self.publish()
        # PR을 닫습니다. 정책이 없으므로 자동으로 고치지 않아야 합니다.
        self.pull_requests.existing = []

        findings = self.reconciler().reconcile()

        self.assertTrue(
            any(f["kind"] == "publication_pr_no_longer_open" for f in findings), findings
        )
        row = self.store.publication(report.publication_id)
        self.assertEqual(row["status"], PublicationStatus.PUBLISHED.value)
        self.assertEqual(len(self.pull_requests.created), 1)

    def test_published_without_pr_number_is_reported(self):
        report = self.publish()
        self.store._connection.execute(
            "UPDATE publications SET pr_number = NULL WHERE publication_id = ?",
            (report.publication_id,),
        )
        self.store._connection.commit()

        findings = self.reconciler().reconcile()

        self.assertTrue(
            any(f["kind"] == "publication_published_without_pr" for f in findings), findings
        )


class SecurityTest(PublicationTestCase):
    SECRET = "ghp_" + "P" * 32

    def test_no_secret_in_database_or_events(self):
        import os

        os.environ["ATLAS_GITHUB_TOKEN"] = self.SECRET
        self.addCleanup(os.environ.pop, "ATLAS_GITHUB_TOKEN", None)

        self.publish()

        blob = json.dumps([dict(r) for r in self.store.events()], ensure_ascii=False)
        blob += Path(self.db).read_bytes().decode("latin-1")
        self.assertNotIn(self.SECRET, blob)

    def test_no_secret_in_pr_body(self):
        body = self.pull_requests.created[0]["body"] if self.pull_requests.created else ""
        self.publish()
        body = self.pull_requests.created[0]["body"]

        self.assertNotIn(self.SECRET, body)
        self.assertNotIn("Bearer", body)

    def test_malicious_objective_does_not_change_the_refspec(self):
        other = self.prepare(number=90)
        task_row = self.store.task_by_fingerprint(other.fingerprint)
        task = json.loads(task_row["task_json"])
        task["objective"] = "x --force +refs/heads/main:refs/heads/main; rm -rf /"
        self.store._connection.execute(
            "UPDATE tasks SET task_json = ? WHERE fingerprint = ?",
            (json.dumps(task, ensure_ascii=False), other.fingerprint),
        )
        self.store._connection.commit()

        report = self.service.publish(other.run_id, WORKER)

        # branch는 여전히 atlas branch이고 main은 건드리지 않았습니다.
        self.assertEqual(self.remote_head(run=other), report.commit_sha)
        self.assertIsNone(self.remote_head("main"))
        message = GitRunner(other.worktree_path).run(
            "log", "-1", "--format=%B", report.commit_sha
        ).text
        self.assertIn("atlas: implement", message)
        # commit message에는 식별자만 들어갑니다. objective를 옮기지 않습니다.
        self.assertNotIn("rm -rf", message)
        self.assertNotIn("--force", message)
        self.assertNotIn("refs/heads", message)

    def test_arbitrary_remote_is_rejected_by_default_policy(self):
        service = PublicationService(
            self.store,
            self.workspaces,
            PublicationConfig(remote="bare"),
            RunConfig(),
            pull_requests=self.pull_requests,
        )

        with self.assertRaises(PublicationError) as caught:
            service.publish(self.run.run_id, WORKER)

        self.assertIs(caught.exception.failure, PublicationFailure.REMOTE_INVALID)
        self.assertIsNone(self.remote_head())

    def test_push_refspec_is_explicit(self):
        calls: list[tuple[str, ...]] = []
        original = GitRunner.run

        def spy(self_runner, *args, **kwargs):
            calls.append(args)
            return original(self_runner, *args, **kwargs)

        GitRunner.run = spy
        try:
            report = self.publish()
        finally:
            GitRunner.run = original

        push = [c for c in calls if c and c[0] == "push"]
        self.assertEqual(len(push), 1)
        self.assertIn(f"{report.commit_sha}:refs/heads/{self.run.branch}", push[0])
        joined = " ".join(push[0])
        self.assertNotIn("--force", joined)
        self.assertNotIn("+refs", joined)


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="atlas-pubschema-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.db = str(self.root / "atlas.db")

    def test_publications_table_exists(self):
        store = TaskStore(self.db)
        self.addCleanup(store.close)

        names = {
            row["name"]
            for row in store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("publications", names)

    def test_active_publication_index_is_unique_per_run(self):
        store = TaskStore(self.db)
        self.addCleanup(store.close)

        sql = store._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_publications_active'"
        ).fetchone()["sql"]
        self.assertIn("UNIQUE", sql)
        self.assertIn("RecoveryRequired", sql)

    def test_events_gain_a_publication_column(self):
        store = TaskStore(self.db)
        self.addCleanup(store.close)

        columns = {row["name"] for row in store._connection.execute("PRAGMA table_info(events)")}
        self.assertIn("publication_id", columns)

    def test_schema_version_is_current(self):
        store = TaskStore(self.db)
        self.addCleanup(store.close)

        version = store._connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        self.assertEqual(version, SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
