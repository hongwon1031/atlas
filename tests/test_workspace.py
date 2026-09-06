"""Run별 격리 worktree/branch 테스트.

실제 임시 git repository를 만들어 검증합니다. network를 쓰지 않습니다.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.config import WorkspaceConfig
from atlas.gitcmd import GitRunner, redact
from atlas.intake import build_idempotency_key
from atlas.parser import parse_issue_body
from atlas.reconciliation import RunReconciler
from atlas.schema import RunFailure, RunStatus, WorkspaceStatus
from atlas.store import RunError, TaskStore, WorkspaceConflict
from atlas.validation import validate_intake
from atlas.workspace import (
    PROTECTED_BRANCHES,
    WorkspaceError,
    WorkspacePlanner,
    branch_name,
    is_atlas_branch,
    sanitize_segment,
)
from atlas.workspace_service import WorkspaceService
from tests.fixtures import make_issue

WORKER = "worker-a"
GIT_AVAILABLE = shutil.which("git") is not None


def git(*args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, shell=False
    )


def make_repo(path: Path) -> str:
    """base commit이 있는 임시 repository를 만듭니다."""

    path.mkdir(parents=True, exist_ok=True)
    git("init", "-b", "main", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)
    git("config", "user.name", "Atlas Test", cwd=path)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md", cwd=path)
    git("commit", "-m", "base commit", cwd=path)
    return GitRunner(path).resolve_revision("main")


class PureNamingTest(unittest.TestCase):
    """git 없이도 검증 가능한 규칙."""

    def test_branch_name_is_namespaced_and_deterministic(self):
        name = branch_name("ATLAS-0042", "run-abcdef0123456789")

        self.assertTrue(name.startswith("atlas/"))
        self.assertEqual(name, branch_name("ATLAS-0042", "run-abcdef0123456789"))
        self.assertTrue(is_atlas_branch(name))

    def test_different_runs_of_the_same_task_get_different_branches(self):
        first = branch_name("ATLAS-0042", "run-aaaaaaaaaaaaaaaa")
        second = branch_name("ATLAS-0042", "run-bbbbbbbbbbbbbbbb")

        self.assertNotEqual(first, second)

    def test_user_input_is_sanitized(self):
        name = branch_name("../../etc/passwd", "run-1234567890ab")

        self.assertNotIn("..", name)
        self.assertNotIn(" ", name)
        self.assertTrue(name.startswith("atlas/"))

    def test_sanitize_rejects_dangerous_segments(self):
        self.assertEqual(sanitize_segment(""), "unknown")
        self.assertNotIn("..", sanitize_segment("a..b"))
        self.assertNotIn("/", sanitize_segment("a/b"))
        self.assertNotIn(";", sanitize_segment("a;rm -rf /"))

    def test_protected_branches_are_declared(self):
        self.assertIn("main", PROTECTED_BRANCHES)
        self.assertIn("master", PROTECTED_BRANCHES)

    def test_branch_name_never_equals_a_protected_branch(self):
        for task in ("main", "master", "HEAD"):
            name = branch_name(task, "run-1234567890ab")
            self.assertNotIn(name, PROTECTED_BRANCHES)
            self.assertTrue(name.startswith("atlas/"))

    def test_redact_removes_credentials_and_tokens(self):
        # 토큰 형태 문자열을 파일에 그대로 두지 않으려고 런타임에 조립합니다.
        fake_token = "gh" + "p_" + ("a" * 24)

        self.assertNotIn("secret", redact("https://user:secret@github.com/x.git"))
        self.assertNotIn(fake_token, redact(f"fatal: {fake_token} rejected"))
        self.assertIn("<redacted>", redact(f"fatal: {fake_token} rejected"))

    def test_redact_truncates(self):
        self.assertLessEqual(len(redact("x" * 5000)), 200)


class WorkspaceConfigTest(unittest.TestCase):
    def test_workspaces_root_defaults_under_repository_root(self):
        config = WorkspaceConfig(repository_root="/tmp/repo")

        resolved = config.resolved_workspaces_root()
        self.assertIsNotNone(resolved)
        self.assertIn(".atlas", resolved)

    def test_no_repository_root_means_no_workspaces_root(self):
        self.assertIsNone(WorkspaceConfig().resolved_workspaces_root())

    def test_explicit_workspaces_root_wins(self):
        config = WorkspaceConfig(repository_root="/tmp/repo", workspaces_root="/tmp/ws")

        self.assertEqual(config.resolved_workspaces_root(), "/tmp/ws")

    def test_git_timeout_must_be_positive(self):
        with self.assertRaises(ValueError):
            WorkspaceConfig(git_timeout_seconds=0)


@unittest.skipUnless(GIT_AVAILABLE, "git 실행 파일이 없습니다")
class GitBackedTestCase(unittest.TestCase):
    """실제 git repository를 쓰는 통합 테스트 베이스."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.repo = self.root / "repo"
        self.base_revision = make_repo(self.repo)
        self.workspaces = self.root / "worktrees"
        self.db = str(self.root / "atlas.db")
        self.store = TaskStore(self.db)
        self.addCleanup(self._teardown)

        self.planner = WorkspacePlanner(self.repo, self.workspaces, base_branch="main")
        self.service = WorkspaceService(self.store, self.planner)
        self.register()
        self.claim = self.store.claim(WORKER, 900)

    def _teardown(self):
        self.store.close()
        try:
            self._dir.cleanup()
        except (PermissionError, OSError):
            pass

    def register(self, issue=None, *, approved=True):
        issue = issue if issue is not None else make_issue()
        key = build_idempotency_key(issue)
        result = validate_intake(issue, parse_issue_body(issue.body), key)
        return self.store.register(
            result, key, repository=issue.repository, issue_number=issue.number,
            labels=issue.labels, approved=approved,
            approval_signal="queue_label:atlas:queued" if approved else None,
        )

    def new_run(self, task_id="ATLAS-0042"):
        return self.store.start_run(task_id, WORKER)


class RepositoryResolutionTest(GitBackedTestCase):
    def test_valid_repository_is_accepted(self):
        toplevel = self.planner.verify_repository()

        self.assertEqual(toplevel.resolve(), self.repo.resolve())

    def test_non_repository_is_refused(self):
        plain = self.root / "plain"
        plain.mkdir()
        planner = WorkspacePlanner(plain, self.workspaces)

        with self.assertRaises(WorkspaceError) as caught:
            planner.verify_repository()

        self.assertIn(caught.exception.category, ("not_a_repository", "not_repository_root"))

    def test_missing_path_is_refused(self):
        planner = WorkspacePlanner(self.root / "nope", self.workspaces)

        with self.assertRaises(WorkspaceError) as caught:
            planner.verify_repository()

        self.assertEqual(caught.exception.category, "repository_not_found")

    def test_subdirectory_is_not_accepted_as_root(self):
        sub = self.repo / "docs"
        sub.mkdir()
        planner = WorkspacePlanner(sub, self.workspaces)

        with self.assertRaises(WorkspaceError) as caught:
            planner.verify_repository()

        self.assertEqual(caught.exception.category, "not_repository_root")

    def test_remote_check_is_skipped_without_remote(self):
        planner = WorkspacePlanner(self.repo, self.workspaces, repository="hongwon1031/atlas")

        self.assertIsNone(planner.verify_remote())

    def test_matching_remote_is_accepted(self):
        git("remote", "add", "origin", "https://github.com/hongwon1031/atlas.git", cwd=self.repo)
        planner = WorkspacePlanner(self.repo, self.workspaces, repository="hongwon1031/atlas")

        self.assertIsNotNone(planner.verify_remote())

    def test_mismatched_remote_is_refused(self):
        git("remote", "add", "origin", "https://github.com/someone/other.git", cwd=self.repo)
        planner = WorkspacePlanner(self.repo, self.workspaces, repository="hongwon1031/atlas")

        with self.assertRaises(WorkspaceError) as caught:
            planner.verify_remote()

        self.assertEqual(caught.exception.category, "repository_mismatch")


class WorkspaceCreationTest(GitBackedTestCase):
    def test_creates_branch_and_worktree(self):
        run = self.new_run()

        result = self.service.create(run.run_id)

        self.assertTrue(result.created)
        stored = self.store.run(run.run_id)
        self.assertEqual(stored.workspace_status, WorkspaceStatus.READY)
        self.assertTrue(stored.branch.startswith("atlas/ATLAS-0042/"))
        self.assertEqual(stored.base_revision, self.base_revision)
        self.assertIsNotNone(stored.workspace_created_at)
        self.assertTrue(Path(stored.worktree_path).exists())

    def test_worktree_is_on_the_expected_branch_at_base_revision(self):
        run = self.new_run()
        self.service.create(run.run_id)
        stored = self.store.run(run.run_id)

        runner = GitRunner(stored.worktree_path)
        self.assertEqual(runner.current_branch(), stored.branch)
        self.assertEqual(runner.head_revision(), self.base_revision)

    def test_main_worktree_is_untouched(self):
        run = self.new_run()
        self.service.create(run.run_id)

        main_runner = GitRunner(self.repo)
        self.assertEqual(main_runner.current_branch(), "main")
        self.assertEqual(main_runner.head_revision(), self.base_revision)

    def test_editing_the_worktree_does_not_touch_main(self):
        run = self.new_run()
        self.service.create(run.run_id)
        stored = self.store.run(run.run_id)

        (Path(stored.worktree_path) / "README.md").write_text("changed\n", encoding="utf-8")

        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "base\n")
        self.assertFalse(GitRunner(self.repo).is_dirty())

    def test_runs_are_isolated_from_each_other(self):
        self.register(make_issue(number=77))
        self.store.claim(WORKER, 900, task_id="ATLAS-0077")
        first = self.new_run()
        second = self.new_run("ATLAS-0077")

        self.service.create(first.run_id)
        self.service.create(second.run_id)

        a = self.store.run(first.run_id)
        b = self.store.run(second.run_id)
        self.assertNotEqual(a.branch, b.branch)
        self.assertNotEqual(a.worktree_path, b.worktree_path)
        (Path(a.worktree_path) / "a.txt").write_text("a\n", encoding="utf-8")
        self.assertFalse((Path(b.worktree_path) / "a.txt").exists())

    def test_retry_run_of_the_same_task_gets_a_new_worktree(self):
        first = self.new_run()
        self.service.create(first.run_id)
        first_stored = self.store.run(first.run_id)
        self.store.finish_run(
            first.run_id, RunStatus.FAILED, failure=RunFailure("timeout", "t")
        )

        retry = self.store.start_run("ATLAS-0042", WORKER, previous_run_id=first.run_id)
        self.service.create(retry.run_id)
        retry_stored = self.store.run(retry.run_id)

        self.assertNotEqual(first_stored.branch, retry_stored.branch)
        self.assertNotEqual(first_stored.worktree_path, retry_stored.worktree_path)

    def test_validation_evidence_is_recorded(self):
        run = self.new_run()

        result = self.service.create(run.run_id)

        self.assertTrue(result.validation["checks"]["branch_matches"])
        self.assertTrue(result.validation["checks"]["head_matches_base"])
        self.assertTrue(result.validation["checks"]["repository_matches"])

    def test_workspace_events_are_recorded(self):
        run = self.new_run()
        self.service.create(run.run_id)

        kinds = [row["kind"] for row in self.store.events()]
        self.assertIn("workspace_preparing", kinds)
        self.assertIn("workspace_ready", kinds)

    def test_terminal_run_cannot_get_a_workspace(self):
        run = self.new_run()
        self.store.finish_run(run.run_id, RunStatus.SUCCEEDED)

        with self.assertRaises(RunError) as caught:
            self.service.create(run.run_id)

        self.assertEqual(caught.exception.category, "run_terminal")

    def test_unknown_run_is_refused(self):
        with self.assertRaises(RunError):
            self.service.create("run-nope")


class SafetyBoundaryTest(GitBackedTestCase):
    def test_main_is_never_used_as_the_run_branch(self):
        run = self.new_run()
        self.service.create(run.run_id)

        stored = self.store.run(run.run_id)
        self.assertNotIn(stored.branch, PROTECTED_BRANCHES)
        self.assertTrue(stored.branch.startswith("atlas/"))

    def test_path_outside_worker_root_is_refused(self):
        outside = self.root / "elsewhere" / "escape"

        with self.assertRaises(WorkspaceError) as caught:
            self.planner.remove_worktree(str(outside))

        self.assertEqual(caught.exception.category, "path_outside_root")

    def test_parent_traversal_is_refused(self):
        traversal = self.workspaces / ".." / "escape"

        with self.assertRaises(WorkspaceError) as caught:
            self.planner.remove_worktree(str(traversal))

        self.assertEqual(caught.exception.category, "path_outside_root")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink 미지원")
    def test_symlink_escape_is_refused(self):
        self.workspaces.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside"
        outside.mkdir()
        link = self.workspaces / "sneaky"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink를 만들 권한이 없습니다")

        with self.assertRaises(WorkspaceError) as caught:
            self.planner.remove_worktree(str(link / "inner"))

        self.assertEqual(caught.exception.category, "path_outside_root")

    def test_non_atlas_branch_cannot_be_deleted(self):
        git("branch", "user-feature", cwd=self.repo)

        with self.assertRaises(WorkspaceError) as caught:
            self.planner.delete_branch("user-feature")

        self.assertEqual(caught.exception.category, "branch_not_owned")
        self.assertTrue(GitRunner(self.repo).branch_exists("user-feature"))

    def test_branch_collision_is_refused(self):
        run = self.new_run()
        planned = branch_name("ATLAS-0042", run.run_id)
        git("branch", planned, cwd=self.repo)

        with self.assertRaises(WorkspaceError) as caught:
            self.service.create(run.run_id)

        self.assertEqual(caught.exception.category, "branch_exists")

    def test_failed_creation_does_not_mark_workspace_ready(self):
        """partial git failure 시 DB가 거짓 ready 상태가 되면 안 됩니다."""

        run = self.new_run()
        git("branch", branch_name("ATLAS-0042", run.run_id), cwd=self.repo)

        with self.assertRaises(WorkspaceError):
            self.service.create(run.run_id)

        stored = self.store.run(run.run_id)
        self.assertNotEqual(stored.workspace_status, WorkspaceStatus.READY)

    def test_git_failure_after_preparing_leaves_recoverable_evidence(self):
        run = self.new_run()
        plan = self.planner.plan(run.run_id, "ATLAS-0042")
        self.store.begin_workspace(
            run.run_id, branch=plan.branch, worktree_path=plan.worktree_path,
            base_branch=plan.base_branch, base_revision=plan.base_revision,
        )
        self.store.fail_workspace(run.run_id, {"category": "worktree_create_failed"})

        stored = self.store.run(run.run_id)
        self.assertEqual(stored.workspace_status, WorkspaceStatus.FAILED)
        self.assertEqual(stored.branch, plan.branch)
        self.assertIn(stored.run_id, [r.run_id for r in self.store.runs_with_workspace()])


class IdempotencyTest(GitBackedTestCase):
    def test_duplicate_create_returns_existing_workspace(self):
        run = self.new_run()
        first = self.service.create(run.run_id)

        second = self.service.create(run.run_id)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.run.branch, second.run.branch)
        self.assertEqual(first.run.worktree_path, second.run.worktree_path)

    def test_duplicate_create_does_not_add_a_second_worktree(self):
        run = self.new_run()
        self.service.create(run.run_id)
        before = len(GitRunner(self.repo).worktrees())

        self.service.create(run.run_id)

        self.assertEqual(len(GitRunner(self.repo).worktrees()), before)

    def test_begin_workspace_refuses_a_second_preparing(self):
        run = self.new_run()
        self.service.create(run.run_id)

        with self.assertRaises(WorkspaceConflict) as caught:
            self.store.begin_workspace(
                run.run_id, branch="atlas/x/y", worktree_path=str(self.workspaces / "x"),
                base_branch="main", base_revision=self.base_revision,
            )

        self.assertEqual(caught.exception.category, "workspace_already_exists")

    def test_workspace_is_reidentified_after_restart(self):
        run = self.new_run()
        created = self.service.create(run.run_id)
        self.store.close()

        reopened = TaskStore(self.db)
        self.addCleanup(reopened.close)
        service = WorkspaceService(reopened, self.planner)

        result = service.create(run.run_id)

        self.assertFalse(result.created)
        self.assertEqual(result.run.branch, created.run.branch)
        self.assertEqual(result.run.worktree_path, created.run.worktree_path)


class CleanupTest(GitBackedTestCase):
    def setUp(self):
        super().setUp()
        self.run = self.new_run()
        self.service.create(self.run.run_id)
        self.stored = self.store.run(self.run.run_id)

    def finish(self, status=RunStatus.SUCCEEDED, failure=None):
        return self.store.finish_run(self.run.run_id, status, failure=failure)

    def test_running_run_cannot_be_cleaned_up(self):
        with self.assertRaises(WorkspaceError) as caught:
            self.service.cleanup(self.run.run_id)

        self.assertEqual(caught.exception.category, "run_not_terminal")

    def test_success_removes_worktree_and_keeps_branch(self):
        self.finish()

        result = self.service.cleanup(self.run.run_id)

        self.assertTrue(result.worktree_removed)
        self.assertTrue(result.branch_kept)
        self.assertFalse(Path(self.stored.worktree_path).exists())
        self.assertTrue(GitRunner(self.repo).branch_exists(self.stored.branch))

    def test_cleanup_records_removal(self):
        self.finish()
        self.service.cleanup(self.run.run_id)

        stored = self.store.run(self.run.run_id)
        self.assertEqual(stored.workspace_status, WorkspaceStatus.REMOVED)
        self.assertIsNotNone(stored.workspace_removed_at)

    def test_dirty_worktree_is_not_removed(self):
        (Path(self.stored.worktree_path) / "wip.txt").write_text("작업 중\n", encoding="utf-8")
        self.finish()

        result = self.service.cleanup(self.run.run_id)

        self.assertFalse(result.worktree_removed)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error["category"], "worktree_dirty")
        self.assertTrue(Path(self.stored.worktree_path).exists())

    def test_dirty_cleanup_failure_is_logged(self):
        (Path(self.stored.worktree_path) / "wip.txt").write_text("wip\n", encoding="utf-8")
        self.finish()
        self.service.cleanup(self.run.run_id)

        kinds = [row["kind"] for row in self.store.events()]
        self.assertIn("workspace_cleanup_failed", kinds)

    def test_dirty_worktree_can_be_removed_explicitly(self):
        (Path(self.stored.worktree_path) / "wip.txt").write_text("wip\n", encoding="utf-8")
        self.finish()

        result = self.service.cleanup(self.run.run_id, allow_dirty=True)

        self.assertTrue(result.worktree_removed)

    def test_failed_run_keeps_the_branch(self):
        self.finish(RunStatus.FAILED, RunFailure("validation_failed", "실패"))

        result = self.service.cleanup(self.run.run_id)

        self.assertTrue(result.branch_kept)
        self.assertTrue(GitRunner(self.repo).branch_exists(self.stored.branch))

    def test_orphaned_run_keeps_the_branch(self):
        self.finish(RunStatus.ORPHANED, RunFailure("worker_lost", "잃음"))

        result = self.service.cleanup(self.run.run_id)

        self.assertTrue(result.branch_kept)

    def test_branch_can_be_deleted_explicitly(self):
        self.finish()

        result = self.service.cleanup(self.run.run_id, delete_branch=True)

        self.assertFalse(result.branch_kept)
        self.assertFalse(GitRunner(self.repo).branch_exists(self.stored.branch))

    def test_cleanup_is_idempotent(self):
        self.finish()
        self.service.cleanup(self.run.run_id)

        again = self.service.cleanup(self.run.run_id)

        self.assertFalse(again.worktree_removed)
        self.assertIsNone(again.error)

    def test_cleanup_refuses_resources_without_provenance(self):
        """DB가 Atlas 소유임을 증명하지 못하면 건드리지 않습니다."""

        self.finish()
        self.store._connection.execute(
            "UPDATE runs SET branch = ? WHERE run_id = ?", ("user-feature", self.run.run_id)
        )
        self.store._connection.commit()

        with self.assertRaises(WorkspaceError) as caught:
            self.service.cleanup(self.run.run_id)

        self.assertEqual(caught.exception.category, "branch_not_owned")


class WorkspaceReconciliationTest(GitBackedTestCase):
    def setUp(self):
        super().setUp()
        self.reconciler = RunReconciler(self.store, workspaces=self.service)
        self.run = self.new_run()
        self.service.create(self.run.run_id)
        self.stored = self.store.run(self.run.run_id)

    def test_healthy_workspace_produces_no_finding(self):
        findings = self.reconciler.reconcile_workspaces()

        self.assertEqual(findings, [])

    def test_missing_worktree_is_reported(self):
        shutil.rmtree(self.stored.worktree_path)

        findings = self.reconciler.reconcile_workspaces()

        self.assertEqual(len(findings), 1)
        self.assertIn("worktree_missing", findings[0]["problems"])
        self.assertEqual(findings[0]["kind"], "workspace_recovery_required")

    def test_missing_worktree_is_not_auto_repaired(self):
        shutil.rmtree(self.stored.worktree_path)

        self.reconciler.reconcile_workspaces()

        stored = self.store.run(self.run.run_id)
        self.assertEqual(stored.workspace_status, WorkspaceStatus.READY)
        self.assertTrue(GitRunner(self.repo).branch_exists(stored.branch))

    def test_wrong_branch_is_reported(self):
        self.store._connection.execute(
            "UPDATE runs SET branch = ? WHERE run_id = ?",
            ("atlas/ATLAS-0042/wrongbranch", self.run.run_id),
        )
        self.store._connection.commit()

        findings = self.reconciler.reconcile_workspaces()

        self.assertEqual(len(findings), 1)
        problems = findings[0]["problems"]
        self.assertTrue({"branch_mismatch", "branch_missing"} & set(problems))

    def test_findings_are_recorded_as_events(self):
        shutil.rmtree(self.stored.worktree_path)
        self.reconciler.reconcile_workspaces()

        kinds = [row["kind"] for row in self.store.events()]
        self.assertIn("workspace_recovery_required", kinds)

    def test_incomplete_workspace_with_leftovers_is_reported(self):
        second = self.new_run("ATLAS-0042") if False else None
        self.register(make_issue(number=88))
        self.store.claim(WORKER, 900, task_id="ATLAS-0088")
        run = self.new_run("ATLAS-0088")
        plan = self.planner.plan(run.run_id, "ATLAS-0088")
        self.store.begin_workspace(
            run.run_id, branch=plan.branch, worktree_path=plan.worktree_path,
            base_branch=plan.base_branch, base_revision=plan.base_revision,
        )
        self.planner.create(plan)
        self.store.fail_workspace(run.run_id, {"category": "simulated"})

        findings = self.reconciler.reconcile_workspaces()

        codes = [f for f in findings if f["run_id"] == run.run_id]
        self.assertEqual(len(codes), 1)
        self.assertIn("incomplete_workspace_left_resources", codes[0]["problems"])

    def test_reconcile_includes_workspace_findings(self):
        shutil.rmtree(self.stored.worktree_path)

        report = self.reconciler.reconcile()

        self.assertEqual(len(report.workspace_findings), 1)
        self.assertIn("workspace_findings", report.to_dict())

    def test_reconcile_without_workspace_service_is_a_noop(self):
        report = RunReconciler(self.store).reconcile()

        self.assertEqual(report.workspace_findings, ())


class SchemaMigrationTest(GitBackedTestCase):
    def test_v3_database_gains_workspace_columns(self):
        run = self.new_run()
        self.service.create(run.run_id)
        branch = self.store.run(run.run_id).branch
        self.store.close()

        import sqlite3

        connection = sqlite3.connect(self.db)
        for column in (
            "workspace_status", "branch", "worktree_path", "base_branch",
            "base_revision", "workspace_created_at", "workspace_removed_at",
            "workspace_error",
        ):
            connection.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        connection.execute("UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'")
        connection.commit()
        connection.close()

        migrated = TaskStore(self.db)
        self.addCleanup(migrated.close)

        version = migrated._connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        restored = migrated.run(run.run_id)
        self.assertEqual(version, "4")
        self.assertEqual(restored.workspace_status, WorkspaceStatus.NONE)
        self.assertIsNone(restored.branch)
        # Run 자체는 보존됩니다.
        self.assertEqual(restored.task_id, "ATLAS-0042")
        self.assertTrue(GitRunner(self.repo).branch_exists(branch))


if __name__ == "__main__":
    unittest.main()
