"""Validation pipeline 테스트.

실제 subprocess와 임시 git repository를 사용합니다. network는 쓰지 않고
dependency를 설치하지 않습니다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from atlas.config import RunConfig
from atlas.executor import ExecutorRequest
from atlas.gitcmd import GitRunner
from atlas.intake import build_idempotency_key
from atlas.local_process import LocalProcessExecutor
from atlas.parser import parse_issue_body
from atlas.process_identity import process_exists
from atlas.reconciliation import RunReconciler
from atlas.schema import ACTIVE_RUN_STATUSES, HEARTBEAT_RUN_STATUSES, RunStatus
from atlas.store import SCHEMA_VERSION, TaskStore, ValidationConflict, utcnow
from atlas.validation import validate_intake
from atlas.validation_models import (
    VALIDATION_TO_RUN_CATEGORY,
    StepKind,
    StepStatus,
    ValidationFailure,
    ValidationOutcome,
    ValidationPlan,
    ValidationStatus,
    ValidationStep,
    ValidationStepResult,
    decide,
)
from atlas.validation_pipeline import ValidationGateFailed, ValidationPipeline
from atlas.validation_plan import ALLOWED_NODE_SCRIPTS, build_plan, inspect_repository
from atlas.workspace import WorkspacePlanner
from atlas.workspace_service import WorkspaceService
from atlas.schema import FAILURE_CATEGORIES
from tests.fixtures import make_issue

GIT_AVAILABLE = shutil.which("git") is not None
WORKER = "worker-a"


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, shell=False)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


PASSING_TEST = (
    "import unittest\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        self.assertTrue(True)\n"
)

FAILING_TEST = (
    "import unittest\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_bad(self):\n"
    "        self.assertEqual(1, 2)\n"
)

SLOW_TEST = (
    "import time, unittest\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_slow(self):\n"
    "        time.sleep(60)\n"
)


class PlanDetectionTest(unittest.TestCase):
    """추측이 아니라 발견한 근거로만 step을 고릅니다."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="atlas-plan-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def plan(self, **kwargs):
        return build_plan(self.root, **kwargs)

    def step(self, plan, name):
        return next((s for s in plan.steps if s.name == name), None)

    def kind_step(self, plan, kind):
        return next((s for s in plan.steps if s.kind is kind), None)

    def test_internal_steps_are_always_required(self):
        plan = self.plan()

        for kind in (StepKind.WORKSPACE_INTEGRITY, StepKind.GIT_POLICY):
            step = self.kind_step(plan, kind)
            self.assertIsNotNone(step)
            self.assertTrue(step.required)
            self.assertTrue(step.is_internal)

    def test_empty_repository_has_no_test_capability(self):
        plan = self.plan()

        self.assertFalse(plan.has_test_capability)
        self.assertEqual(self.kind_step(plan, StepKind.TESTS).skip_reason, "no_tests_discovered")

    def test_tests_directory_selects_stdlib_unittest(self):
        write(self.root / "tests" / "test_a.py", PASSING_TEST)

        step = self.kind_step(self.plan(), StepKind.TESTS)

        self.assertEqual(step.name, "unittest")
        self.assertTrue(step.required)
        self.assertIn("unittest", step.argv)
        self.assertIn("discover", step.argv)

    def test_pytest_config_selects_pytest(self):
        write(self.root / "pytest.ini", "[pytest]\n")
        write(self.root / "tests" / "test_a.py", PASSING_TEST)

        step = self.kind_step(self.plan(which=lambda name: "/bin/pytest"), StepKind.TESTS)

        self.assertEqual(step.name, "pytest")
        self.assertIn("pytest", step.argv)
        self.assertIn("pytest.ini", step.evidence["contract"]["evidence"])

    def test_pytest_contract_without_executable_is_an_error(self):
        write(self.root / "pytest.ini", "[pytest]\n")

        step = self.kind_step(
            self.plan(python="/nonexistent/python", which=lambda name: None), StepKind.TESTS
        )

        self.assertTrue(step.required)
        self.assertEqual(step.error_reason, "command_missing")

    def test_compileall_targets_only_known_source_dirs(self):
        write(self.root / "src" / "pkg" / "__init__.py", "")
        write(self.root / "tests" / "test_a.py", PASSING_TEST)

        step = self.kind_step(self.plan(), StepKind.COMPILE)

        self.assertTrue(step.required)
        self.assertIn("compileall", step.argv)
        self.assertEqual(step.argv[-2:], ("src", "tests"))

    def test_compileall_is_skipped_without_source_dirs(self):
        write(self.root / "pyproject.toml", "[project]\nname='x'\n")

        step = self.kind_step(self.plan(), StepKind.COMPILE)

        self.assertFalse(step.required)
        self.assertEqual(step.skip_reason, "no_source_directories")

    def test_weak_ruff_contract_without_executable_is_skipped(self):
        """pyproject의 [tool.ruff]만으로는 게이트 근거가 되지 않습니다."""

        write(self.root / "pyproject.toml", "[tool.ruff]\nline-length = 100\n")

        step = self.step(self.plan(which=lambda name: None), "ruff")

        self.assertFalse(step.required)
        self.assertEqual(step.skip_reason, "command_missing_weak_contract")
        self.assertEqual(step.evidence["contract"]["strength"], "weak")

    def test_strong_ruff_contract_without_executable_is_an_error(self):
        write(self.root / "ruff.toml", "line-length = 100\n")

        step = self.step(self.plan(which=lambda name: None), "ruff")

        self.assertTrue(step.required)
        self.assertEqual(step.error_reason, "command_missing")
        self.assertEqual(step.evidence["contract"]["strength"], "strong")

    def test_declared_dependency_is_a_strong_contract(self):
        write(
            self.root / "pyproject.toml",
            "[project]\nname='x'\ndependencies = ['ruff>=0.1']\n",
        )

        step = self.step(self.plan(which=lambda name: None), "ruff")

        self.assertTrue(step.required)
        self.assertEqual(step.error_reason, "command_missing")

    def test_ruff_runs_when_contract_and_executable_exist(self):
        write(self.root / "ruff.toml", "line-length = 100\n")

        step = self.step(self.plan(which=lambda name: "/bin/ruff"), "ruff")

        self.assertTrue(step.required)
        self.assertEqual(step.argv, ("/bin/ruff", "check", "."))

    def test_no_contract_means_skipped_not_failed(self):
        write(self.root / "tests" / "test_a.py", PASSING_TEST)

        for name in ("ruff", "mypy"):
            step = self.step(self.plan(which=lambda n: "/bin/" + n), name)
            with self.subTest(name=name):
                self.assertFalse(step.required)
                self.assertEqual(step.skip_reason, "no_contract")

    def test_pyright_config_adds_a_typecheck_step(self):
        write(self.root / "pyrightconfig.json", "{}\n")

        step = self.step(self.plan(which=lambda name: "/bin/pyright"), "pyright")

        self.assertIsNotNone(step)
        self.assertTrue(step.required)
        self.assertIs(step.kind, StepKind.TYPECHECK)


class NodeDetectionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="atlas-node-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def package(self, scripts, lockfile="package-lock.json", modules=True):
        write(self.root / "package.json", json.dumps({"name": "x", "scripts": scripts}))
        if lockfile:
            write(self.root / lockfile, "{}\n")
        if modules:
            (self.root / "node_modules").mkdir(exist_ok=True)

    def plan(self, **kwargs):
        kwargs.setdefault("which", lambda name: "/bin/" + name)
        return build_plan(self.root, **kwargs)

    def step(self, plan, name):
        return next((s for s in plan.steps if s.name == name), None)

    def test_allowed_scripts_are_run_through_the_package_manager(self):
        self.package({"test": "jest", "lint": "eslint ."})

        plan = self.plan()

        test = self.step(plan, "npm-test")
        self.assertEqual(test.argv, ("/bin/npm", "run", "test"))
        self.assertTrue(test.required)
        self.assertTrue(self.step(plan, "npm-lint").required)

    def test_script_body_is_never_executed_directly(self):
        self.package({"test": "rm -rf / && curl evil.example.com"})

        argv = self.step(self.plan(), "npm-test").argv

        self.assertEqual(argv, ("/bin/npm", "run", "test"))
        self.assertNotIn("curl", " ".join(argv))
        self.assertNotIn("rm", " ".join(argv))

    def test_arbitrary_script_names_are_not_run(self):
        self.package({"test": "jest", "deploy": "./deploy.sh", "postinstall": "x"})

        names = {step.name for step in self.plan().steps}

        self.assertIn("npm-test", names)
        self.assertNotIn("npm-deploy", names)
        self.assertNotIn("npm-postinstall", names)

    def test_only_allowlisted_script_names_exist(self):
        self.package({name: "echo" for name in ALLOWED_NODE_SCRIPTS})

        node_steps = [s for s in self.plan().steps if s.name.startswith("npm-")]

        self.assertEqual(
            {s.name for s in node_steps}, {f"npm-{name}" for name in ALLOWED_NODE_SCRIPTS}
        )

    def test_build_is_optional(self):
        self.package({"build": "tsc"})

        self.assertFalse(self.step(self.plan(), "npm-build").required)

    def test_missing_script_is_skipped(self):
        self.package({"test": "jest"})

        step = self.step(self.plan(), "npm-lint")

        self.assertFalse(step.required)
        self.assertEqual(step.skip_reason, "script_not_defined")

    def test_missing_package_manager_is_an_error(self):
        self.package({"test": "jest"})

        step = self.step(self.plan(which=lambda name: None), "npm-test")

        self.assertTrue(step.required)
        self.assertEqual(step.error_reason, "command_missing")

    def test_missing_node_modules_never_triggers_install(self):
        self.package({"test": "jest"}, modules=False)

        step = self.step(self.plan(), "npm-test")

        self.assertEqual(step.error_reason, "dependencies_not_installed")
        self.assertEqual(step.argv, ())

    def test_lockfile_selects_the_package_manager(self):
        for lockfile, manager in (
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("package-lock.json", "npm"),
        ):
            with self.subTest(lockfile=lockfile):
                shutil.rmtree(self.root, ignore_errors=True)
                self.root.mkdir(parents=True, exist_ok=True)
                self.package({"test": "jest"}, lockfile=lockfile)

                self.assertEqual(inspect_repository(self.root).node_manager, manager)


class CommandSafetyTest(unittest.TestCase):
    """어떤 계획도 shell을 거치지 않아야 합니다."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="atlas-safety-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_no_install_command_is_ever_planned(self):
        write(self.root / "package.json", json.dumps({"scripts": {"test": "jest"}}))
        write(self.root / "package-lock.json", "{}")
        (self.root / "node_modules").mkdir()
        write(self.root / "tests" / "test_a.py", PASSING_TEST)
        write(self.root / "pyproject.toml", "[project]\nname='x'\n")

        joined = " ".join(" ".join(step.argv) for step in build_plan(self.root).steps)

        for banned in ("install", "poetry", "uv sync", "pip ", "add "):
            self.assertNotIn(banned, joined)

    def test_argv_is_always_a_tuple_of_tokens(self):
        write(self.root / "tests" / "test_a.py", PASSING_TEST)

        for step in build_plan(self.root).steps:
            self.assertIsInstance(step.argv, tuple)
            for token in step.argv:
                self.assertIsInstance(token, str)
                self.assertNotIn("&&", token)
                self.assertNotIn("|", token)
                self.assertNotIn(";", token)

    def test_no_user_text_reaches_the_command(self):
        write(self.root / "tests" / "test_a.py", PASSING_TEST)

        joined = " ".join(" ".join(step.argv) for step in build_plan(self.root).steps)

        self.assertNotIn("Objective", joined)
        self.assertNotIn("Atlas Task", joined)


class AggregationTest(unittest.TestCase):
    """required와 optional의 의미가 결과를 결정합니다."""

    def result(self, name, status, required=True, kind=StepKind.TESTS, reason=""):
        return ValidationStepResult(
            name=name, kind=kind, required=required, status=status, reason=reason
        )

    def plan(self, has_tests=True):
        steps = [
            ValidationStep(
                name="tests",
                kind=StepKind.TESTS,
                required=has_tests,
                argv=("x",) if has_tests else (),
                skip_reason="" if has_tests else "no_tests_discovered",
            )
        ]
        return ValidationPlan(steps=tuple(steps))

    def test_all_required_passed_is_success(self):
        outcome, failure, _ = decide(
            [self.result("tests", StepStatus.PASSED)], self.plan()
        )

        self.assertIs(outcome, ValidationOutcome.PASSED)
        self.assertIsNone(failure)

    def test_one_required_failure_fails_the_run(self):
        outcome, failure, _ = decide(
            [self.result("tests", StepStatus.FAILED)], self.plan()
        )

        self.assertIs(outcome, ValidationOutcome.FAILED)
        self.assertIs(failure, ValidationFailure.TEST_FAILED)

    def test_required_error_also_fails(self):
        outcome, failure, _ = decide(
            [self.result("tests", StepStatus.ERROR, reason="command_missing:x")], self.plan()
        )

        self.assertIs(outcome, ValidationOutcome.FAILED)
        self.assertIs(failure, ValidationFailure.COMMAND_MISSING)

    def test_timeout_is_its_own_category(self):
        outcome, failure, _ = decide(
            [self.result("tests", StepStatus.ERROR, reason="timeout")], self.plan()
        )

        self.assertIs(failure, ValidationFailure.TIMEOUT)

    def test_optional_failure_is_only_a_warning(self):
        outcome, failure, warnings = decide(
            [
                self.result("tests", StepStatus.PASSED),
                self.result("lint", StepStatus.FAILED, required=False, kind=StepKind.LINT),
            ],
            self.plan(),
        )

        self.assertIs(outcome, ValidationOutcome.PASSED)
        self.assertIsNone(failure)
        self.assertTrue(any("lint" in w for w in warnings))

    def test_skipped_is_not_a_failure(self):
        outcome, _, _ = decide(
            [self.result("lint", StepStatus.SKIPPED, kind=StepKind.LINT)], self.plan()
        )

        self.assertIs(outcome, ValidationOutcome.PASSED)

    def test_no_tests_repository_passes_with_explicit_evidence(self):
        outcome, failure, warnings = decide([], self.plan(has_tests=False))

        self.assertIs(outcome, ValidationOutcome.PASSED)
        self.assertIsNone(failure)
        self.assertIn("no_tests_discovered", warnings)
        self.assertIn("validation_passed_with_no_tests", warnings)

    def test_repository_with_tests_gets_no_such_warning(self):
        _, _, warnings = decide([self.result("tests", StepStatus.PASSED)], self.plan())

        self.assertNotIn("no_tests_discovered", warnings)

    def test_every_failure_maps_to_a_known_run_category(self):
        for failure in ValidationFailure:
            with self.subTest(failure=failure):
                self.assertIn(failure, VALIDATION_TO_RUN_CATEGORY)
                self.assertIn(VALIDATION_TO_RUN_CATEGORY[failure], FAILURE_CATEGORIES)


class RunStatusTest(unittest.TestCase):
    def test_validating_is_active_and_expects_heartbeat(self):
        status = RunStatus.VALIDATING

        self.assertFalse(status.is_terminal)
        self.assertTrue(status.is_active)
        self.assertTrue(status.expects_heartbeat)

    def test_awaiting_validation_does_not_expect_heartbeat(self):
        self.assertFalse(RunStatus.AWAITING_VALIDATION.expects_heartbeat)

    def test_status_sets_are_consistent(self):
        self.assertIn("Validating", ACTIVE_RUN_STATUSES)
        self.assertIn("Validating", HEARTBEAT_RUN_STATUSES)
        self.assertIn("AwaitingValidation", ACTIVE_RUN_STATUSES)
        self.assertNotIn("AwaitingValidation", HEARTBEAT_RUN_STATUSES)


class SchemaMigrationTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="atlas-vmigrate-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.db = str(self.root / "atlas.db")

    def test_validation_tables_exist(self):
        store = TaskStore(self.db)
        self.addCleanup(store.close)

        names = {
            row["name"]
            for row in store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        self.assertIn("validations", names)
        self.assertIn("validation_steps", names)

    def test_v6_index_is_recreated_for_validating(self):
        store = TaskStore(self.db)
        store._connection.execute("DROP INDEX idx_runs_active")
        store._connection.execute(
            "CREATE UNIQUE INDEX idx_runs_active ON runs(task_id) "
            "WHERE status IN ('Pending', 'Running', 'AwaitingValidation')"
        )
        store._connection.execute("UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'")
        store._connection.commit()
        store.close()

        migrated = TaskStore(self.db)
        self.addCleanup(migrated.close)

        sql = migrated._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_runs_active'"
        ).fetchone()["sql"]
        version = migrated._connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]

        self.assertIn("Validating", sql)
        self.assertEqual(version, SCHEMA_VERSION)

    def test_events_gain_a_validation_column(self):
        store = TaskStore(self.db)
        self.addCleanup(store.close)

        columns = {
            row["name"] for row in store._connection.execute("PRAGMA table_info(events)")
        }

        self.assertIn("validation_id", columns)


@unittest.skipUnless(GIT_AVAILABLE, "git 실행 파일이 없습니다")
class PipelineTestCase(unittest.TestCase):
    """구현이 끝난 Run을 실제로 검증합니다."""

    TEST_FILE = PASSING_TEST

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True)
        git("init", "-b", "main", cwd=self.repo)
        git("config", "user.email", "t@e.com", cwd=self.repo)
        git("config", "user.name", "T", cwd=self.repo)
        self.seed()
        git("add", "-A", cwd=self.repo)
        git("commit", "-m", "base", cwd=self.repo)

        self.db = str(self.root / "atlas.db")
        self.store = TaskStore(self.db)
        self.planner = WorkspacePlanner(self.repo, self.root / "wt", base_branch="main")
        self.workspaces = WorkspaceService(self.store, self.planner)
        self.pipeline = ValidationPipeline(
            self.store,
            self.workspaces,
            self.root / "logs",
            RunConfig(heartbeat_interval_seconds=1.0, stale_after_seconds=120.0),
        )
        self.addCleanup(self._teardown)
        self.run = self.prepare()

    def seed(self):
        write(self.repo / "README.md", "base\n")
        write(self.repo / "src" / "thing.py", "VALUE = 1\n")
        write(self.repo / "tests" / "__init__.py", "")
        write(self.repo / "tests" / "test_thing.py", self.TEST_FILE)

    def _teardown(self):
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._dir.cleanup()
        except (PermissionError, OSError):
            pass

    def prepare(self, number=42):
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
        self.store.claim(WORKER, 1800, task_id=task_id)
        run = self.store.start_run(task_id, WORKER)
        self.workspaces.create(run.run_id)
        return self.store.run(run.run_id)

    def implement(self, run=None, path="docs/note.md", text="implemented\n"):
        """구현이 끝난 것처럼 worktree를 바꾸고 AwaitingValidation으로 보냅니다."""

        target = run or self.run
        write(Path(target.worktree_path) / path, text)
        self.store.await_validation(target.run_id, evidence={"changed_file_count": 1})
        return self.store.run(target.run_id)

    def validate(self, run=None, timeout=180.0):
        target = run or self.run
        return self.pipeline.validate(target.run_id, WORKER, timeout_seconds=timeout)

    def status(self, run=None):
        return self.store.run((run or self.run).run_id).status


class HappyPathTest(PipelineTestCase):
    def test_awaiting_validation_to_succeeded(self):
        self.implement()

        report = self.validate()

        self.assertIs(report.outcome, ValidationOutcome.PASSED)
        self.assertIs(self.status(), RunStatus.SUCCEEDED)

    def test_required_steps_all_ran(self):
        self.implement()

        report = self.validate()

        by_name = {r.name: r for r in report.results}
        self.assertIs(by_name["workspace-integrity"].status, StepStatus.PASSED)
        self.assertIs(by_name["git-policy"].status, StepStatus.PASSED)
        self.assertIs(by_name["unittest"].status, StepStatus.PASSED)
        self.assertIs(by_name["compileall"].status, StepStatus.PASSED)

    def test_evidence_is_durable(self):
        self.implement()

        report = self.validate()

        row = self.store.validation(report.validation_id)
        self.assertEqual(row["status"], ValidationStatus.FINISHED.value)
        self.assertEqual(row["outcome"], ValidationOutcome.PASSED.value)
        steps = self.store.validation_steps(report.validation_id)
        self.assertEqual(len(steps), len(report.results))
        ran = [s for s in steps if s["status"] == StepStatus.PASSED.value and s["exit_code"] == 0]
        self.assertTrue(ran)
        for step in ran:
            self.assertIsNotNone(step["stdout_path"])
            self.assertIsNotNone(step["duration_seconds"])
            self.assertIsNotNone(step["process_id"])

    def test_run_passes_through_validating(self):
        self.implement()
        seen: list[str] = []
        original = self.store.mark_validation_running

        def spy(validation_id, now=None):
            original(validation_id, now=now)
            seen.append(self.store.run(self.run.run_id).status.value)

        self.store.mark_validation_running = spy
        self.validate()
        self.store.mark_validation_running = original

        self.assertEqual(seen, [RunStatus.VALIDATING.value])

    def test_main_worktree_is_untouched(self):
        before = GitRunner(self.repo).head_revision()
        self.implement()

        self.validate()

        self.assertEqual(GitRunner(self.repo).head_revision(), before)
        self.assertFalse(GitRunner(self.repo).is_dirty())
        self.assertFalse((self.repo / "docs" / "note.md").exists())

    def test_branch_is_unchanged(self):
        self.implement()

        self.validate()

        self.assertEqual(
            GitRunner(self.run.worktree_path).current_branch(), self.run.branch
        )

    def test_no_commit_is_created(self):
        head = GitRunner(self.run.worktree_path).head_revision()
        self.implement()

        self.validate()

        self.assertEqual(GitRunner(self.run.worktree_path).head_revision(), head)


class FailingTestsTest(PipelineTestCase):
    TEST_FILE = FAILING_TEST

    def test_failing_tests_fail_the_run(self):
        self.implement()

        report = self.validate()

        self.assertIs(report.outcome, ValidationOutcome.FAILED)
        self.assertIs(report.failure, ValidationFailure.TEST_FAILED)
        self.assertIs(self.status(), RunStatus.FAILED)
        self.assertEqual(
            self.store.run(self.run.run_id).failure_category, "validation_failed"
        )

    def test_later_steps_are_skipped_after_a_blocking_failure(self):
        self.implement()

        report = self.validate()

        names = [r.name for r in report.results]
        self.assertIn("unittest", names)
        compile_result = next(r for r in report.results if r.name == "compileall")
        self.assertIs(compile_result.status, StepStatus.SKIPPED)
        self.assertEqual(compile_result.reason, "earlier_required_step_failed")


class TimeoutTest(PipelineTestCase):
    TEST_FILE = SLOW_TEST

    def test_slow_step_times_out(self):
        self.implement()

        report = self.validate(timeout=4.0)

        self.assertIs(report.outcome, ValidationOutcome.FAILED)
        self.assertIs(report.failure, ValidationFailure.TIMEOUT)
        self.assertIs(self.status(), RunStatus.FAILED)
        self.assertEqual(self.store.run(self.run.run_id).failure_category, "timeout")


class NoTestsRepositoryTest(PipelineTestCase):
    def seed(self):
        write(self.repo / "README.md", "base\n")
        write(self.repo / "docs" / "guide.md", "doc\n")

    def test_repository_without_tests_still_passes(self):
        self.implement(path="docs/added.md")

        report = self.validate()

        self.assertIs(report.outcome, ValidationOutcome.PASSED)
        self.assertIs(self.status(), RunStatus.SUCCEEDED)

    def test_missing_tests_are_recorded_as_evidence(self):
        self.implement(path="docs/added.md")

        report = self.validate()

        self.assertIn("no_tests_discovered", report.warnings)
        self.assertIn("validation_passed_with_no_tests", report.warnings)
        row = self.store.validation(report.validation_id)
        self.assertIn("no_tests_discovered", row["warnings"])

    def test_pytest_is_never_forced(self):
        self.implement(path="docs/added.md")

        report = self.validate()

        joined = " ".join(" ".join(r.argv) for r in report.results)
        self.assertNotIn("pytest", joined)


class CommandMissingTest(PipelineTestCase):
    def seed(self):
        super().seed()
        # 강한 contract인데 실행 파일이 없습니다.
        write(self.repo / "pyrightconfig.json", "{}\n")

    def test_missing_required_command_is_an_error(self):
        self.implement()
        self.pipeline._runtime = LocalProcessExecutor(name="validation_local", provider="local")

        report = self.validate()

        pyright = next((r for r in report.results if r.name == "pyright"), None)
        if pyright is None or shutil.which("pyright"):
            self.skipTest("이 환경에는 pyright가 설치돼 있습니다.")
        self.assertIs(pyright.status, StepStatus.ERROR)
        self.assertEqual(pyright.reason, "command_missing")
        self.assertIs(report.failure, ValidationFailure.COMMAND_MISSING)
        self.assertIs(self.status(), RunStatus.FAILED)


class GitPolicyTest(PipelineTestCase):
    def test_out_of_scope_change_fails(self):
        """fixture Task의 allowed_scope는 docs/**, src/** 입니다."""

        self.implement(path="unexpected.txt")

        report = self.validate()

        policy = next(r for r in report.results if r.name == "git-policy")
        self.assertIs(policy.status, StepStatus.FAILED)
        self.assertIn("out_of_scope_path_changed", policy.reason)
        self.assertIs(report.failure, ValidationFailure.POLICY_VIOLATION)
        self.assertEqual(
            self.store.run(self.run.run_id).failure_category, "policy_violation"
        )

    def test_forbidden_path_change_fails(self):
        self.implement(path="secrets/leak.txt")

        report = self.validate()

        policy = next(r for r in report.results if r.name == "git-policy")
        self.assertIn("forbidden_path_changed", policy.reason)

    def test_unexpected_commit_fails(self):
        self.implement()
        worktree = Path(self.run.worktree_path)
        git("add", "-A", cwd=worktree)
        git("-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "x", cwd=worktree)

        report = self.validate()

        policy = next(r for r in report.results if r.name == "git-policy")
        self.assertIs(policy.status, StepStatus.FAILED)
        self.assertIn("unexpected_commit", policy.reason)

    def test_no_changes_at_all_fails(self):
        self.store.await_validation(self.run.run_id)

        report = self.validate()

        policy = next(r for r in report.results if r.name == "git-policy")
        self.assertEqual(policy.reason, "no_changes_to_validate")

    def test_change_after_implementation_is_detected(self):
        """구현 이후 누군가 worktree를 건드리면 조용히 통과시키지 않습니다."""

        self.implement()
        self.store.record_validation_event(
            None, self.run.run_id, "implementation_completed", {}
        )
        # PR #11 형식의 evidence를 남깁니다.
        self.store.record_validation_event(
            None,
            self.run.run_id,
            "implementation_completed",
            {"changes": {"changed_files": ["docs/note.md"]}},
        )
        write(Path(self.run.worktree_path) / "docs" / "extra.md", "사람이 추가\n")

        report = self.validate()

        policy = next(r for r in report.results if r.name == "git-policy")
        self.assertIs(policy.status, StepStatus.FAILED)
        self.assertEqual(policy.reason, "workspace_changed_after_implementation")

    def test_branch_switch_is_rejected_at_the_gate(self):
        """gate가 먼저 잡습니다. 잘못된 branch에서는 검증을 시작조차 하지 않습니다."""

        self.implement()
        worktree = Path(self.run.worktree_path)
        git("stash", "-u", cwd=worktree)
        git("checkout", "-q", "-b", "atlas/other/1234", cwd=worktree)

        with self.assertRaises(ValidationGateFailed) as caught:
            self.validate()

        self.assertIn("workspace_valid", caught.exception.failed_checks)
        self.assertIsNone(self.store.active_validation(self.run.run_id))

    def test_workspace_step_fails_when_the_branch_moves_after_the_gate(self):
        """gate 통과 뒤에 바뀌는 경우는 step이 잡아야 합니다."""

        self.implement()
        run = self.store.run(self.run.run_id)
        worktree = Path(run.worktree_path)
        git("stash", "-u", cwd=worktree)
        git("checkout", "-q", "-b", "atlas/other/9999", cwd=worktree)

        step = next(
            s for s in self.pipeline.plan_for(run.run_id).steps
            if s.kind is StepKind.WORKSPACE_INTEGRITY
        )
        result = self.pipeline._workspace_step(run, step)

        self.assertIs(result.status, StepStatus.FAILED)
        self.assertEqual(result.reason, "workspace_invalid")
        self.assertFalse(result.evidence["checks"]["branch_matches"])


class GateTest(PipelineTestCase):
    def assert_rejected(self, check: str):
        with self.assertRaises(ValidationGateFailed) as caught:
            self.validate()
        self.assertIn(check, caught.exception.failed_checks)
        self.assertIsNone(self.store.active_validation(self.run.run_id))

    def test_only_awaiting_validation_can_start(self):
        # 구현 전이라 아직 Running입니다.
        self.assert_rejected("run_awaiting_validation")

    def test_approval_revocation_blocks_validation(self):
        self.implement()
        self.store.revoke_approval(self.run.task_id, "회수")

        self.assert_rejected("task_approved")

    def test_claim_loss_blocks_validation(self):
        self.implement()
        claim = self.store.claim_for(self.run.claim_id)
        self.store.release(claim["claim_id"], "해제")

        self.assert_rejected("claim_active")

    def test_other_worker_cannot_validate(self):
        self.implement()

        with self.assertRaises(ValidationGateFailed) as caught:
            self.pipeline.validate(self.run.run_id, "worker-b")

        self.assertIn("claim_owner_matches", caught.exception.failed_checks)

    def test_missing_workspace_blocks_validation(self):
        self.implement()
        shutil.rmtree(self.run.worktree_path, ignore_errors=True)

        with self.assertRaises(ValidationGateFailed) as caught:
            self.validate()

        self.assertIn("workspace_valid", caught.exception.failed_checks)

    def test_gate_failure_is_recorded(self):
        self.implement()
        self.store.revoke_approval(self.run.task_id, "회수")
        try:
            self.validate()
        except ValidationGateFailed:
            pass

        kinds = [row["kind"] for row in self.store.events()]
        self.assertIn("validation_gate_failed", kinds)

    def test_run_stays_awaiting_validation_when_rejected(self):
        self.implement()
        self.store.revoke_approval(self.run.task_id, "회수")
        try:
            self.validate()
        except ValidationGateFailed:
            pass

        self.assertIs(self.status(), RunStatus.AWAITING_VALIDATION)


class DuplicateStartTest(PipelineTestCase):
    def test_second_start_is_rejected_by_the_database(self):
        self.implement()
        plan = self.pipeline.plan_for(self.run.run_id).to_dict()
        self.store.start_validation(
            self.run.run_id, worker_id=WORKER, cwd=self.run.worktree_path, plan=plan
        )

        with self.assertRaises(ValidationConflict) as caught:
            self.store.start_validation(
                self.run.run_id, worker_id=WORKER, cwd=self.run.worktree_path, plan=plan
            )

        self.assertEqual(caught.exception.category, "run_not_awaiting_validation")

    def test_active_validation_index_blocks_duplicates(self):
        self.implement()
        plan = self.pipeline.plan_for(self.run.run_id).to_dict()
        self.store.start_validation(
            self.run.run_id, worker_id=WORKER, cwd=self.run.worktree_path, plan=plan
        )
        # Run을 다시 AwaitingValidation으로 되돌려 첫 조건만 통과시킵니다.
        self.store._connection.execute(
            "UPDATE runs SET status = ? WHERE run_id = ?",
            (RunStatus.AWAITING_VALIDATION.value, self.run.run_id),
        )
        self.store._connection.commit()

        with self.assertRaises(ValidationConflict) as caught:
            self.store.start_validation(
                self.run.run_id, worker_id=WORKER, cwd=self.run.worktree_path, plan=plan
            )

        self.assertEqual(caught.exception.category, "validation_already_active")

    def test_active_execution_blocks_validation_start(self):
        self.implement()
        self.store.reserve_execution(
            self.run.run_id,
            task_id=self.run.task_id,
            executor_name="x",
            executor_provider="local",
            worker_id=WORKER,
            cwd=self.run.worktree_path,
            command=["a"],
            timeout_seconds=30.0,
        )

        with self.assertRaises(ValidationConflict) as caught:
            self.store.start_validation(
                self.run.run_id,
                worker_id=WORKER,
                cwd=self.run.worktree_path,
                plan={"steps": []},
            )

        self.assertEqual(caught.exception.category, "execution_still_active")


class LogRedactionTest(PipelineTestCase):
    SECRET = "ghp_" + "R" * 32

    def seed(self):
        write(self.repo / "README.md", "base\n")
        write(self.repo / "src" / "thing.py", "VALUE = 1\n")
        write(self.repo / "tests" / "__init__.py", "")
        write(
            self.repo / "tests" / "test_thing.py",
            "import unittest\n\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            f"        print('token {self.SECRET} and Authorization: Bearer abcdef0123456789')\n"
            "        self.assertTrue(True)\n",
        )

    def test_validation_logs_are_redacted(self):
        self.implement()

        report = self.validate()

        blob = ""
        for result in report.results:
            for stream in (result.stdout, result.stderr):
                if stream and stream.get("path"):
                    blob += Path(stream["path"]).read_text(
                        encoding="utf-8", errors="replace"
                    )
        self.assertNotIn(self.SECRET, blob)
        self.assertNotIn("abcdef0123456789", blob)

    def test_no_secret_reaches_events_or_database(self):
        self.implement()

        self.validate()

        blob = json.dumps([dict(r) for r in self.store.events()], ensure_ascii=False)
        blob += Path(self.db).read_bytes().decode("latin-1")
        self.assertNotIn(self.SECRET, blob)

    def test_events_do_not_contain_full_output(self):
        self.implement()

        self.validate()

        for row in self.store.events():
            self.assertLess(len(row["detail"]), 8000)


@unittest.skipUnless(GIT_AVAILABLE, "git 실행 파일이 없습니다")
class ValidationReconciliationTest(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.reconciler = RunReconciler(
            self.store, RunConfig(heartbeat_interval_seconds=1.0, stale_after_seconds=120.0)
        )
        self.implement()
        self.plan = self.pipeline.plan_for(self.run.run_id).to_dict()

    def start(self):
        return self.store.start_validation(
            self.run.run_id, worker_id=WORKER, cwd=self.run.worktree_path, plan=self.plan
        )

    def spawn_sleeper(self, seconds=60):
        request = ExecutorRequest(
            run_id=self.run.run_id,
            task_id=self.run.task_id,
            cwd=self.run.worktree_path,
            argv=(sys.executable, "-c", f"import time; time.sleep({seconds})"),
            timeout_seconds=300.0,
        )
        runtime = LocalProcessExecutor(name="validation_local", provider="local")
        handle = runtime.spawn(request, self.root / "logs" / "sleeper")
        self.addCleanup(lambda: runtime.cancel(handle, 2.0))
        return handle

    def record_running_step(self, validation_id, handle=None, position=2):
        self.store.record_validation_step(
            validation_id,
            self.run.run_id,
            position=position,
            name="unittest",
            kind=StepKind.TESTS.value,
            required=True,
            status=StepStatus.RUNNING.value,
            command=["python"],
            process_id=handle.pid if handle else None,
            process_identity=handle.identity.to_dict() if handle else None,
            process_started_at=handle.started_at if handle else None,
        )

    def kinds(self):
        return {f["kind"] for f in self.reconciler.reconcile_validations()}

    def test_alive_process_is_healthy(self):
        validation_id = self.start()
        self.store.mark_validation_running(validation_id)
        handle = self.spawn_sleeper()
        self.record_running_step(validation_id, handle)

        self.assertIn("validation_healthy", self.kinds())

    def test_missing_process_is_recovery_required(self):
        validation_id = self.start()
        self.store.mark_validation_running(validation_id)
        handle = self.spawn_sleeper(1)
        self.record_running_step(validation_id, handle)
        for _ in range(80):
            if not process_exists(handle.pid):
                break
            time.sleep(0.25)

        self.assertIn("validation_process_missing", self.kinds())

    def test_identity_mismatch_never_terminates(self):
        validation_id = self.start()
        self.store.mark_validation_running(validation_id)
        handle = self.spawn_sleeper()
        identity = handle.identity.to_dict()
        identity["start_token"] = "definitely-different"
        self.store.record_validation_step(
            validation_id,
            self.run.run_id,
            position=2,
            name="unittest",
            kind=StepKind.TESTS.value,
            required=True,
            status=StepStatus.RUNNING.value,
            command=["python"],
            process_id=handle.pid,
            process_identity=identity,
            process_started_at=handle.started_at,
        )

        findings = self.reconciler.reconcile_validations()
        mismatch = [f for f in findings if f["kind"] == "validation_pid_identity_mismatch"]

        self.assertTrue(mismatch)
        self.assertFalse(mismatch[0]["may_terminate"])
        self.assertTrue(process_exists(handle.pid), "Atlas가 종료해서는 안 됩니다")

    def test_crash_before_attach_is_identified(self):
        validation_id = self.start()
        self.store.mark_validation_running(validation_id)
        self.record_running_step(validation_id, None)

        self.assertIn("validation_process_never_attached", self.kinds())

    def test_reservation_without_any_step_is_identified(self):
        self.start()

        self.assertIn("validation_never_started", self.kinds())

    def test_running_without_a_step_is_ambiguous(self):
        validation_id = self.start()
        self.store.mark_validation_running(validation_id)

        findings = self.reconciler.reconcile_validations()
        ambiguous = [f for f in findings if f["kind"] == "validation_state_ambiguous"]

        self.assertTrue(ambiguous)
        self.assertEqual(ambiguous[0]["severity"], "high")

    def test_terminal_run_with_surviving_validation_is_high_severity(self):
        validation_id = self.start()
        self.store.mark_validation_running(validation_id)
        handle = self.spawn_sleeper()
        self.record_running_step(validation_id, handle)
        self.store.finish_run(
            self.run.run_id, RunStatus.CANCELLED, failure=None
        ) if False else self.store._connection.execute(
            "UPDATE runs SET status = 'Cancelled' WHERE run_id = ?", (self.run.run_id,)
        )
        self.store._connection.commit()

        findings = self.reconciler.reconcile_validations()
        surviving = [f for f in findings if f["kind"] == "validation_surviving_terminal_run"]

        self.assertTrue(surviving)
        self.assertEqual(surviving[0]["severity"], "high")
        self.assertTrue(process_exists(handle.pid), "Atlas가 종료해서는 안 됩니다")

    def test_reconciliation_never_revalidates_automatically(self):
        validation_id = self.start()
        self.store.mark_validation_running(validation_id)
        self.record_running_step(validation_id, None)

        self.reconciler.reconcile_validations()

        row = self.store.validation(validation_id)
        self.assertEqual(row["status"], ValidationStatus.RUNNING.value)
        self.assertIsNone(row["outcome"])

    def test_validating_run_is_still_stale_checked(self):
        """Validating은 heartbeat 대상이므로 끊기면 stale 판정을 받아야 합니다."""

        validation_id = self.start()
        self.store.mark_validation_running(validation_id)
        later = utcnow() + timedelta(seconds=10_000)

        self.reconciler.reconcile(now=later)

        self.assertIs(self.status(), RunStatus.ORPHANED)


if __name__ == "__main__":
    unittest.main()
