"""Executor runtime 테스트.

실제 subprocess와 임시 git repository를 사용합니다. network는 쓰지 않습니다.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from atlas.config import ExecutorConfig, RunConfig, WorkspaceConfig
from atlas.execution_service import ExecutionService, SafetyGateFailed
from atlas.executor import (
    ACTIVE_EXECUTION_STATUSES,
    CancellationState,
    ExecutionStatus,
    ExecutorError,
    ExecutorFailure,
    ExecutorRequest,
)
from atlas.gitcmd import GitRunner
from atlas.intake import build_idempotency_key
from atlas.local_process import (
    LocalProcessExecutor,
    base_environment,
    read_log_tail,
)
from atlas.parser import parse_issue_body
from atlas.process_identity import IdentityVerdict, ProcessIdentity, capture, process_exists, verify
from atlas.reconciliation import RunReconciler
from atlas.redaction import redact, redact_argv, redact_line, redact_values
from atlas.schema import RunStatus
from atlas.store import ExecutionConflict, RunError, TaskStore
from atlas.validation import validate_intake
from atlas.workspace import WorkspacePlanner
from atlas.workspace_service import WorkspaceService
from tests.fixtures import make_issue

WORKER = "worker-a"
GIT_AVAILABLE = shutil.which("git") is not None
SRC_ROOT = str(Path(__file__).resolve().parent.parent / "src")
ENV = {"PYTHONPATH": SRC_ROOT}


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, shell=False)


def mock_argv(mode="success", **kwargs):
    argv = [sys.executable, "-m", "atlas.mock_executor", "--mode", mode]
    for key, value in kwargs.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return tuple(argv)


class RedactionTest(unittest.TestCase):
    def test_url_credentials_are_removed(self):
        self.assertNotIn("secret", redact("https://user:secret@github.com/x.git"))

    def test_token_shapes_are_removed(self):
        token = "gh" + "p_" + ("a" * 24)
        self.assertNotIn(token, redact(f"fatal: {token} denied"))

    def test_bearer_and_authorization_are_removed(self):
        self.assertNotIn("abc123def456", redact("Authorization: Bearer abc123def456"))
        self.assertNotIn("abc123def456", redact("bearer abc123def456"))

    def test_known_secret_values_are_removed(self):
        secret = "super-secret-value-1234"
        self.assertNotIn(secret, redact(f"env={secret}", secrets=(secret,)))

    def test_short_values_are_not_blindly_replaced(self):
        """짧은 값을 무차별 치환하면 로그를 읽을 수 없게 됩니다."""

        self.assertIn("ab", redact_values("ab cd", secrets=("ab",)))

    def test_longer_secret_is_replaced_before_shorter(self):
        long_secret = "abcdefgh12345678"
        short_secret = "abcdefgh"
        cleaned = redact_values(long_secret, secrets=(short_secret, long_secret))
        self.assertNotIn(long_secret, cleaned)

    def test_redact_line_collapses_and_limits(self):
        self.assertLessEqual(len(redact_line("x " * 500, limit=50)), 50)

    def test_redact_argv_scrubs_each_token(self):
        secret = "token-value-abcdef"
        argv = redact_argv(["run", f"--key={secret}"], secrets=(secret,))
        self.assertNotIn(secret, " ".join(argv))


class ProcessIdentityTest(unittest.TestCase):
    def test_self_identity_matches(self):
        self.assertIs(verify(capture(os.getpid(), "x")), IdentityVerdict.MATCH)

    def test_absent_pid_is_reported(self):
        identity = ProcessIdentity(pid=999_999, method="x", start_token="y", captured_at="z")
        self.assertIs(verify(identity), IdentityVerdict.PROCESS_ABSENT)

    def test_tampered_token_is_mismatch(self):
        identity = capture(os.getpid(), "x")
        tampered = ProcessIdentity(
            pid=identity.pid, method=identity.method, start_token="0", captured_at="x"
        )
        self.assertIs(verify(tampered), IdentityVerdict.MISMATCH)

    def test_unverifiable_identity_is_not_a_match(self):
        identity = ProcessIdentity(
            pid=os.getpid(), method="unavailable", start_token=None, captured_at="x"
        )
        verdict = verify(identity)
        self.assertIs(verdict, IdentityVerdict.UNVERIFIABLE)
        self.assertFalse(verdict.may_terminate)

    def test_only_match_permits_termination(self):
        self.assertTrue(IdentityVerdict.MATCH.may_terminate)
        for verdict in (
            IdentityVerdict.MISMATCH,
            IdentityVerdict.UNVERIFIABLE,
            IdentityVerdict.PROCESS_ABSENT,
        ):
            self.assertFalse(verdict.may_terminate)

    def test_exited_process_is_not_alive(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        time.sleep(0.3)
        self.assertFalse(process_exists(process.pid))

    def test_roundtrip_through_dict(self):
        identity = capture(os.getpid(), "x")
        self.assertEqual(ProcessIdentity.from_dict(identity.to_dict()), identity)


class EnvironmentAllowlistTest(unittest.TestCase):
    def test_only_allowlisted_variables_survive(self):
        environ = {"PATH": "/bin", "SECRET_TOKEN": "nope", "HOME": "/home/x"}
        result = base_environment(environ)

        self.assertIn("PATH", result)
        self.assertNotIn("SECRET_TOKEN", result)

    def test_empty_values_are_dropped(self):
        self.assertNotIn("PATH", base_environment({"PATH": ""}))


class AdapterTestCase(unittest.TestCase):
    """git 없이 adapter만 검증합니다."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._dir.name)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.logs = self.root / "logs"
        self.executor = LocalProcessExecutor()
        self.addCleanup(self._teardown)

    def _teardown(self):
        try:
            self._dir.cleanup()
        except (PermissionError, OSError):
            pass

    def request(self, mode="success", timeout=30.0, limit=1_048_576, grace=2.0, **kwargs):
        return ExecutorRequest(
            run_id="run-1",
            task_id="ATLAS-0001",
            cwd=str(self.cwd),
            argv=mock_argv(mode, **kwargs),
            timeout_seconds=timeout,
            environment=dict(ENV),
            max_output_bytes=limit,
            grace_period_seconds=grace,
        )

    def execute(self, request, name="x"):
        handle = self.executor.spawn(request, self.logs / name)
        return handle, self.executor.wait(handle, request)


class AdapterBasicsTest(AdapterTestCase):
    def test_success_returns_zero(self):
        _, result = self.execute(self.request("success"))

        self.assertTrue(result.succeeded)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.failure)
        self.assertIs(result.status, ExecutionStatus.FINISHED)

    def test_nonzero_exit_is_classified(self):
        _, result = self.execute(self.request("fail", exit_code=5))

        self.assertFalse(result.succeeded)
        self.assertEqual(result.exit_code, 5)
        self.assertIs(result.failure, ExecutorFailure.NONZERO_EXIT)

    def test_process_runs_in_the_requested_cwd(self):
        self.execute(self.request("success", write_file="made.txt"))

        self.assertTrue((self.cwd / "made.txt").exists())

    def test_missing_cwd_is_refused(self):
        request = ExecutorRequest(
            run_id="r", task_id="t", cwd=str(self.root / "nope"),
            argv=mock_argv(), timeout_seconds=5.0,
        )
        with self.assertRaises(ExecutorError) as caught:
            self.executor.spawn(request, self.logs / "x")

        self.assertEqual(caught.exception.category, "cwd_missing")

    def test_empty_argv_is_refused(self):
        request = ExecutorRequest(
            run_id="r", task_id="t", cwd=str(self.cwd), argv=(), timeout_seconds=5.0
        )
        with self.assertRaises(ExecutorError) as caught:
            self.executor.spawn(request, self.logs / "x")

        self.assertEqual(caught.exception.category, "empty_argv")

    def test_environment_is_not_inherited_wholesale(self):
        os.environ["ATLAS_TEST_LEAK"] = "leaked-value"
        self.addCleanup(os.environ.pop, "ATLAS_TEST_LEAK", None)

        request = ExecutorRequest(
            run_id="r", task_id="t", cwd=str(self.cwd),
            argv=(sys.executable, "-c",
                  "import os,sys; sys.stdout.write(os.environ.get('ATLAS_TEST_LEAK','absent'))"),
            timeout_seconds=20.0,
        )
        handle = self.executor.spawn(request, self.logs / "env")
        result = self.executor.wait(handle, request)

        self.assertEqual(read_log_tail(result.stdout.path).strip(), "absent")


class OutputCaptureTest(AdapterTestCase):
    def test_stdout_and_stderr_are_separated(self):
        _, result = self.execute(
            self.request("fail", stdout_text="to-out", stderr_text="to-err", exit_code=1)
        )

        self.assertIn("to-out", read_log_tail(result.stdout.path))
        self.assertIn("to-err", read_log_tail(result.stderr.path))
        self.assertNotIn("to-err", read_log_tail(result.stdout.path))

    def test_output_is_capped(self):
        _, result = self.execute(self.request("output", limit=2048, stdout_bytes=50_000))

        self.assertEqual(result.stdout.bytes_written, 2048)
        self.assertTrue(result.stdout.truncated)

    def test_small_output_is_not_marked_truncated(self):
        _, result = self.execute(self.request("success", stdout_text="tiny"))

        self.assertFalse(result.stdout.truncated)

    def test_invalid_utf8_does_not_crash(self):
        _, result = self.execute(self.request("binary"))

        raw = Path(result.stdout.path).read_bytes()
        self.assertIn(b"invalid utf-8", raw)
        # 안전 디코딩이 예외를 내지 않아야 합니다.
        self.assertIn("invalid utf-8", read_log_tail(result.stdout.path))

    def test_logs_are_written_under_the_given_directory(self):
        _, result = self.execute(self.request("success"), name="scoped")

        self.assertTrue(Path(result.stdout.path).is_relative_to(self.logs / "scoped"))


class TimeoutAndCancelTest(AdapterTestCase):
    def test_timeout_terminates_and_classifies(self):
        started = time.monotonic()
        handle, result = self.execute(self.request("sleep", timeout=2.0, sleep_seconds=60))

        self.assertIs(result.failure, ExecutorFailure.TIMEOUT)
        self.assertLess(time.monotonic() - started, 30.0)
        time.sleep(0.5)
        self.assertFalse(process_exists(handle.pid))

    def test_cancel_terminates_a_running_process(self):
        request = self.request("sleep", timeout=120.0, sleep_seconds=60)
        handle = self.executor.spawn(request, self.logs / "cancel")
        time.sleep(0.5)
        self.assertTrue(process_exists(handle.pid))

        state = self.executor.cancel(handle, 2.0)
        time.sleep(0.5)

        self.assertIn(state, (CancellationState.COMPLETED, CancellationState.FORCED))
        self.assertFalse(process_exists(handle.pid))

    def test_cancel_of_finished_process_is_idempotent(self):
        handle, _ = self.execute(self.request("success"))

        self.assertIs(self.executor.cancel(handle, 1.0), CancellationState.COMPLETED)

    def test_cancel_refuses_unverified_identity(self):
        """identity를 증명하지 못하면 종료하지 않습니다."""

        request = self.request("sleep", timeout=120.0, sleep_seconds=30)
        handle = self.executor.spawn(request, self.logs / "unverified")
        time.sleep(0.4)
        tampered = type(handle)(
            pid=handle.pid,
            identity=ProcessIdentity(handle.pid, handle.identity.method, "0", "x"),
            started_at=handle.started_at,
            process_group_id=handle.process_group_id,
        )
        try:
            with self.assertRaises(ExecutorError) as caught:
                self.executor.cancel(tampered, 1.0)
            self.assertEqual(caught.exception.category, "identity_unverified")
            self.assertTrue(process_exists(handle.pid))
        finally:
            self.executor.cancel(handle, 1.0)


class ProcessTreeTest(AdapterTestCase):
    def _child_pid(self, marker: Path) -> int:
        for _ in range(150):
            if marker.exists():
                try:
                    return int(marker.read_text(encoding="utf-8").strip())
                except ValueError:
                    pass
            time.sleep(0.1)
        self.fail("child process가 시작되지 않았습니다")

    def test_timeout_leaves_no_child_process(self):
        marker = self.cwd / "child.txt"
        request = self.request(
            "child", timeout=2.0, child_sleep_seconds=120, child_marker=str(marker)
        )
        handle = self.executor.spawn(request, self.logs / "tree")
        child = self._child_pid(marker)
        self.assertTrue(process_exists(child))

        self.executor.wait(handle, request)
        time.sleep(1.5)

        self.assertFalse(process_exists(handle.pid), "parent가 남았습니다")
        self.assertFalse(process_exists(child), "child가 남았습니다")

    def test_cancel_leaves_no_child_process(self):
        marker = self.cwd / "child.txt"
        request = self.request(
            "child", timeout=120.0, child_sleep_seconds=120, child_marker=str(marker)
        )
        handle = self.executor.spawn(request, self.logs / "tree2")
        child = self._child_pid(marker)

        self.executor.cancel(handle, 2.0)
        time.sleep(1.5)

        self.assertFalse(process_exists(child), "child가 남았습니다")


@unittest.skipUnless(GIT_AVAILABLE, "git 실행 파일이 없습니다")
class ExecutionServiceTestCase(unittest.TestCase):
    """workspace가 준비된 Run에서 executor를 돌립니다."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True)
        git("init", "-b", "main", cwd=self.repo)
        git("config", "user.email", "t@e.com", cwd=self.repo)
        git("config", "user.name", "T", cwd=self.repo)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git("add", "README.md", cwd=self.repo)
        git("commit", "-m", "base", cwd=self.repo)
        self.base = GitRunner(self.repo).resolve_revision("main")

        self.db = str(self.root / "atlas.db")
        self.store = TaskStore(self.db)
        self.planner = WorkspacePlanner(self.repo, self.root / "wt", base_branch="main")
        self.workspaces = WorkspaceService(self.store, self.planner)
        self.service = self._service(self.store)
        self.addCleanup(self._teardown)

        self.run = self.prepare()

    def _service(self, store):
        return ExecutionService(
            store,
            LocalProcessExecutor(),
            WorkspaceService(store, self.planner),
            self.root / "logs",
            RunConfig(heartbeat_interval_seconds=1.0, stale_after_seconds=60.0),
        )

    def _teardown(self):
        try:
            for row in self.store.active_executions():
                try:
                    self.service.cancel(row["run_id"], "teardown", 1.0)
                except Exception:  # noqa: BLE001
                    pass
            self.store.close()
        except Exception:  # noqa: BLE001 - 테스트가 이미 닫았을 수 있습니다.
            pass
        try:
            self._dir.cleanup()
        except (PermissionError, OSError):
            pass

    def prepare(self, number=42):
        issue = make_issue(number=number)
        key = build_idempotency_key(issue)
        self.store.register(
            validate_intake(issue, parse_issue_body(issue.body), key), key,
            repository=issue.repository, issue_number=issue.number, labels=issue.labels,
            approved=True, approval_signal="queue_label:atlas:queued",
        )
        task_id = f"ATLAS-{number:04d}"
        self.store.claim(WORKER, 900, task_id=task_id)
        run = self.store.start_run(task_id, WORKER)
        self.workspaces.create(run.run_id)
        return self.store.run(run.run_id)

    def execute(self, run=None, mode="success", timeout=30.0, **kwargs):
        target = run or self.run
        return self.service.run(
            target.run_id, WORKER, mock_argv(mode, **kwargs),
            timeout_seconds=timeout, environment=dict(ENV), grace_period_seconds=2.0,
        )


class ExecutionSuccessTest(ExecutionServiceTestCase):
    def test_successful_execution_is_persisted(self):
        outcome = self.execute(mode="success", write_file="made.txt", stdout_text="hi")

        row = self.store.execution(outcome.execution_id)
        self.assertTrue(outcome.result.succeeded)
        self.assertEqual(row["status"], ExecutionStatus.FINISHED.value)
        self.assertEqual(row["process_exit_code"], 0)
        self.assertIsNotNone(row["process_id"])
        self.assertIsNotNone(row["process_identity"])
        self.assertIsNotNone(row["process_started_at"])
        self.assertIsNotNone(row["process_finished_at"])

    def test_process_cwd_is_the_run_worktree(self):
        self.execute(mode="success", write_file="made.txt")

        self.assertTrue((Path(self.run.worktree_path) / "made.txt").exists())

    def test_main_worktree_is_not_polluted(self):
        self.execute(mode="success", write_file="made.txt")

        self.assertFalse((self.repo / "made.txt").exists())
        self.assertFalse(GitRunner(self.repo).is_dirty())
        self.assertEqual(GitRunner(self.repo).head_revision(), self.base)

    def test_other_run_worktree_is_not_polluted(self):
        other = self.prepare(77)
        self.execute(mode="success", write_file="made.txt")

        self.assertFalse((Path(other.worktree_path) / "made.txt").exists())

    def test_run_becomes_succeeded(self):
        outcome = self.execute()
        self.service.apply_to_run(self.run.run_id, outcome.result)

        self.assertIs(self.store.run(self.run.run_id).status, RunStatus.SUCCEEDED)

    def test_command_is_stored_redacted(self):
        secret = "super-secret-token-abcdef"
        self.service.run(
            self.run.run_id, WORKER,
            mock_argv("success") + (f"--stdout-text={secret}",),
            timeout_seconds=30.0, environment=dict(ENV), secret_values=(secret,),
        )
        row = self.store.executions(self.run.run_id)[0]

        self.assertNotIn(secret, row["command"])

    def test_lifecycle_events_are_recorded(self):
        self.execute()
        kinds = [row["kind"] for row in self.store.events()]

        self.assertIn("execution_reserved", kinds)
        self.assertIn("execution_running", kinds)
        self.assertIn("execution_finished", kinds)


class ExecutionFailureTest(ExecutionServiceTestCase):
    def test_nonzero_exit_marks_run_failed(self):
        outcome = self.execute(mode="fail", exit_code=9)
        self.service.apply_to_run(self.run.run_id, outcome.result)
        run = self.store.run(self.run.run_id)

        self.assertEqual(outcome.result.exit_code, 9)
        self.assertIs(run.status, RunStatus.FAILED)
        self.assertEqual(run.failure_category, "transient_executor")

    def test_timeout_marks_run_failed_with_timeout_category(self):
        outcome = self.execute(mode="sleep", timeout=2.0, sleep_seconds=60)
        self.service.apply_to_run(self.run.run_id, outcome.result)
        run = self.store.run(self.run.run_id)

        self.assertIs(outcome.result.failure, ExecutorFailure.TIMEOUT)
        self.assertIs(run.status, RunStatus.FAILED)
        self.assertEqual(run.failure_category, "timeout")

    def test_terminal_run_is_not_overwritten(self):
        outcome = self.execute()
        self.service.apply_to_run(self.run.run_id, outcome.result)
        first = self.store.run(self.run.run_id).finished_at

        self.service.apply_to_run(self.run.run_id, outcome.result)

        self.assertEqual(self.store.run(self.run.run_id).finished_at, first)


class SafetyGateTest(ExecutionServiceTestCase):
    def test_gate_passes_for_a_ready_run(self):
        gate = self.service.safety_gate(self.run.run_id, WORKER)

        self.assertTrue(gate.passed, gate.failed_checks)

    def test_revoked_approval_blocks_execution(self):
        self.store.revoke_approval("ATLAS-0042", "queue_label_absent")

        with self.assertRaises(SafetyGateFailed) as caught:
            self.execute()

        self.assertIn("task_approved", caught.exception.gate.failed_checks)
        self.assertEqual(len(self.store.executions(self.run.run_id)), 0)

    def test_released_claim_blocks_execution(self):
        claim = self.store.active_claim("ATLAS-0042")
        self.store.release(claim["claim_id"], "released")

        with self.assertRaises(SafetyGateFailed) as caught:
            self.execute()

        self.assertIn("claim_active", caught.exception.gate.failed_checks)

    def test_wrong_worker_blocks_execution(self):
        with self.assertRaises(SafetyGateFailed) as caught:
            self.service.run(
                self.run.run_id, "worker-intruder", mock_argv(), timeout_seconds=10.0,
                environment=dict(ENV),
            )

        self.assertIn("claim_owner_matches", caught.exception.gate.failed_checks)

    def test_stale_workspace_blocks_execution(self):
        shutil.rmtree(self.run.worktree_path)

        with self.assertRaises(SafetyGateFailed) as caught:
            self.execute()

        self.assertIn("workspace_valid", caught.exception.gate.failed_checks)

    def test_terminal_run_blocks_execution(self):
        self.store.finish_run(self.run.run_id, RunStatus.SUCCEEDED)

        with self.assertRaises(SafetyGateFailed) as caught:
            self.execute()

        self.assertIn("run_active", caught.exception.gate.failed_checks)

    def test_gate_failure_is_recorded(self):
        self.store.revoke_approval("ATLAS-0042", "x")
        with self.assertRaises(SafetyGateFailed):
            self.execute()

        kinds = [row["kind"] for row in self.store.events()]
        self.assertIn("execution_safety_gate_failed", kinds)


class CancellationTest(ExecutionServiceTestCase):
    def start_sleeper(self, run=None, seconds=60):
        target = run or self.run
        return self.service.start(
            target.run_id, WORKER, mock_argv("sleep", sleep_seconds=seconds),
            timeout_seconds=300.0, environment=dict(ENV), grace_period_seconds=2.0,
        )

    def test_explicit_cancel_terminates_the_process(self):
        _, _, handle = self.start_sleeper()
        time.sleep(0.5)

        outcome = self.service.cancel(self.run.run_id, "manual", 2.0)
        time.sleep(0.7)

        self.assertTrue(outcome["cancelled"])
        self.assertFalse(process_exists(handle.pid))

    def test_cancel_is_idempotent(self):
        self.start_sleeper()
        time.sleep(0.4)
        self.service.cancel(self.run.run_id, "first", 2.0)

        second = self.service.cancel(self.run.run_id, "second", 2.0)

        self.assertFalse(second["cancelled"])

    def test_cancel_records_state(self):
        execution_id, _, _ = self.start_sleeper()
        time.sleep(0.4)
        self.service.cancel(self.run.run_id, "manual", 2.0)

        row = self.store.execution(execution_id)
        self.assertIn(row["cancellation_state"], ("completed", "forced"))
        self.assertEqual(row["failure_category"], ExecutorFailure.CANCELLED.value)

    def test_approval_revocation_cancels_active_execution(self):
        _, _, handle = self.start_sleeper()
        time.sleep(0.5)
        self.store.revoke_approval("ATLAS-0042", "queue_label_absent")

        results = self.service.cancel_for_lost_authorization(WORKER)
        time.sleep(0.7)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["cancelled"])
        self.assertFalse(process_exists(handle.pid))

    def test_claim_loss_cancels_active_execution(self):
        _, _, handle = self.start_sleeper()
        time.sleep(0.5)
        claim = self.store.active_claim("ATLAS-0042")
        self.store.release(claim["claim_id"], "lost")

        results = self.service.cancel_for_lost_authorization(WORKER)
        time.sleep(0.7)

        self.assertTrue(results[0]["cancelled"])
        self.assertFalse(process_exists(handle.pid))

    def test_healthy_execution_is_not_cancelled(self):
        self.start_sleeper()
        time.sleep(0.4)

        self.assertEqual(self.service.cancel_for_lost_authorization(WORKER), [])


class IdempotencyTest(ExecutionServiceTestCase):
    def test_duplicate_start_is_refused(self):
        self.service.start(
            self.run.run_id, WORKER, mock_argv("sleep", sleep_seconds=30),
            timeout_seconds=120.0, environment=dict(ENV),
        )

        with self.assertRaises(ExecutionConflict) as caught:
            self.service.start(
                self.run.run_id, WORKER, mock_argv(), timeout_seconds=10.0,
                environment=dict(ENV),
            )

        self.assertEqual(caught.exception.category, "execution_already_active")
        self.assertEqual(len(self.store.executions(self.run.run_id)), 1)

    def test_concurrent_start_launches_only_one_process(self):
        outcomes: list[object] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def attempt():
            store = TaskStore(self.db, busy_timeout_seconds=15.0)
            service = self._service(store)
            try:
                barrier.wait(timeout=15)
                try:
                    result = service.start(
                        self.run.run_id, WORKER, mock_argv("sleep", sleep_seconds=10),
                        timeout_seconds=60.0, environment=dict(ENV),
                    )
                except Exception as error:  # noqa: BLE001
                    result = error
            finally:
                store.close()
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        started = [o for o in outcomes if not isinstance(o, Exception)]
        self.assertEqual(len(outcomes), 6)
        self.assertEqual(len(started), 1)
        self.assertEqual(len(self.store.executions(self.run.run_id)), 1)

    def test_sequential_executions_are_allowed_after_completion(self):
        self.execute()
        second = self.execute()

        self.assertTrue(second.result.succeeded)
        self.assertEqual(len(self.store.executions(self.run.run_id)), 2)


class HeartbeatTest(ExecutionServiceTestCase):
    def test_heartbeat_advances_while_the_process_runs(self):
        from atlas.store import from_iso

        before = self.store.run(self.run.run_id).heartbeat_at
        self.execute(mode="sleep", timeout=30.0, sleep_seconds=4)
        after = self.store.run(self.run.run_id).heartbeat_at

        self.assertNotEqual(after, before)
        self.assertGreater((from_iso(after) - from_iso(before)).total_seconds(), 1.0)

    def test_heartbeat_failures_are_not_ignored(self):
        """heartbeat 실패는 event로 남아야 합니다."""

        self.execute(mode="sleep", timeout=30.0, sleep_seconds=2)
        kinds = [row["kind"] for row in self.store.events()]

        # 정상 경로에서는 실패 event가 없어야 합니다.
        self.assertNotIn("execution_heartbeat_failed", kinds)


class ReconciliationTest(ExecutionServiceTestCase):
    def reconciler(self, store=None, service=None):
        target = store or self.store
        return RunReconciler(target, executions=service or self.service)

    def test_running_process_is_healthy(self):
        _, _, handle = self.service.start(
            self.run.run_id, WORKER, mock_argv("sleep", sleep_seconds=30),
            timeout_seconds=120.0, environment=dict(ENV),
        )
        time.sleep(0.5)

        findings = self.reconciler().reconcile_processes()
        mine = [f for f in findings if f["run_id"] == self.run.run_id]

        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["kind"], "execution_healthy")
        self.assertEqual(mine[0]["identity_verdict"], "match")

    def test_reidentification_survives_restart(self):
        self.service.start(
            self.run.run_id, WORKER, mock_argv("sleep", sleep_seconds=30),
            timeout_seconds=120.0, environment=dict(ENV),
        )
        time.sleep(0.5)
        self.store.close()

        reopened = TaskStore(self.db)
        self.addCleanup(reopened.close)
        self.store = reopened
        service = self._service(reopened)
        findings = self.reconciler(reopened, service).reconcile_processes()

        mine = [f for f in findings if f["run_id"] == self.run.run_id]
        self.assertEqual(mine[0]["kind"], "execution_healthy")

    def test_missing_process_is_recovery_required(self):
        execution_id, _, handle = self.service.start(
            self.run.run_id, WORKER, mock_argv("sleep", sleep_seconds=30),
            timeout_seconds=120.0, environment=dict(ENV),
        )
        time.sleep(0.4)
        self.service.cancel(self.run.run_id, "cleanup", 2.0)
        time.sleep(0.5)
        self.store._connection.execute(
            "UPDATE executions SET status='Running' WHERE execution_id=?", (execution_id,)
        )
        self.store._connection.commit()

        findings = self.reconciler().reconcile_processes()
        mine = [f for f in findings if f["execution_id"] == execution_id]

        self.assertEqual(mine[0]["problem"], "process_missing")
        self.assertEqual(mine[0]["kind"], "execution_recovery_required")

    def test_identity_mismatch_is_reported_and_not_terminated(self):
        execution_id, _, _ = self.service.start(
            self.run.run_id, WORKER, mock_argv("sleep", sleep_seconds=30),
            timeout_seconds=120.0, environment=dict(ENV),
        )
        time.sleep(0.4)
        self.store._connection.execute(
            "UPDATE executions SET process_identity=? WHERE execution_id=?",
            (json.dumps({"pid": os.getpid(), "method": capture(os.getpid(), "x").method,
                         "start_token": "0", "captured_at": "x"}), execution_id),
        )
        self.store._connection.commit()

        findings = self.reconciler().reconcile_processes()
        mine = [f for f in findings if f["execution_id"] == execution_id]

        self.assertEqual(mine[0]["problem"], "pid_identity_mismatch")
        self.assertFalse(mine[0]["may_terminate"])
        # 우리 자신을 죽이지 않았습니다.
        self.assertTrue(process_exists(os.getpid()))

    def test_crash_before_attach_is_recovery_required(self):
        execution_id = self.store.reserve_execution(
            self.run.run_id, task_id=self.run.task_id, executor_name="mock_local",
            executor_provider="local", worker_id=WORKER, cwd=self.run.worktree_path,
            command=["x"], timeout_seconds=10.0,
        )

        findings = self.reconciler().reconcile_processes()
        mine = [f for f in findings if f["execution_id"] == execution_id]

        self.assertEqual(mine[0]["problem"], "process_never_attached")

    def test_surviving_process_on_terminal_run_is_high_severity(self):
        execution_id, _, handle = self.service.start(
            self.run.run_id, WORKER, mock_argv("sleep", sleep_seconds=30),
            timeout_seconds=120.0, environment=dict(ENV),
        )
        time.sleep(0.5)
        # Run만 종료 상태로 만들고 process는 살려 둡니다.
        self.store._connection.execute(
            "UPDATE runs SET status='Succeeded' WHERE run_id=?", (self.run.run_id,)
        )
        self.store._connection.commit()

        findings = self.reconciler().reconcile_processes()
        mine = [f for f in findings if f["kind"] == "execution_surviving_terminal_run"]

        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["problem"], "process_outlived_run")
        self.assertTrue(process_exists(handle.pid), "자동으로 종료하면 안 됩니다")

    def test_reconcile_report_includes_process_findings(self):
        self.service.start(
            self.run.run_id, WORKER, mock_argv("sleep", sleep_seconds=20),
            timeout_seconds=60.0, environment=dict(ENV),
        )
        time.sleep(0.4)

        report = self.reconciler().reconcile()

        self.assertIn("process_findings", report.to_dict())
        self.assertTrue(report.process_findings)


class SchemaMigrationTest(ExecutionServiceTestCase):
    def test_v4_database_gains_executions_table(self):
        outcome = self.execute()
        self.store.close()

        connection = __import__("sqlite3").connect(self.db)
        connection.execute("DROP TABLE executions")
        connection.execute("ALTER TABLE events DROP COLUMN execution_id")
        connection.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")
        connection.commit()
        connection.close()

        migrated = TaskStore(self.db)
        self.addCleanup(migrated.close)
        self.store = migrated

        version = migrated._connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        self.assertEqual(version, "5")
        # Run과 workspace는 보존됩니다.
        self.assertIsNotNone(migrated.run(self.run.run_id))
        self.assertEqual(migrated.run(self.run.run_id).task_id, "ATLAS-0042")
        self.assertEqual(migrated.executions(self.run.run_id), [])


class ConfigTest(unittest.TestCase):
    def test_executor_defaults(self):
        config = ExecutorConfig()

        self.assertGreater(config.timeout_seconds, 0)
        self.assertGreater(config.max_output_bytes, 0)

    def test_non_positive_values_are_rejected(self):
        with self.assertRaises(ValueError):
            ExecutorConfig(timeout_seconds=0)
        with self.assertRaises(ValueError):
            ExecutorConfig(max_output_bytes=0)

    def test_logs_root_defaults_under_repository(self):
        config = WorkspaceConfig(repository_root="/tmp/repo")

        self.assertIn(".atlas", config.resolved_logs_root())

    def test_active_execution_statuses_are_declared(self):
        self.assertEqual(ACTIVE_EXECUTION_STATUSES, ("Cancelling", "Running", "Starting"))


if __name__ == "__main__":
    unittest.main()
