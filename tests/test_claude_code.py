"""Claude Code executor adapter 테스트.

실제 Claude CLI를 부르지 않습니다. adapter 배선은 가짜 실행 파일로 검증하고,
실제 CLI 검증 결과는 docs/verification-log.md에 있습니다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from atlas.claude_code import (
    CLAUDE_TO_RUN_CATEGORY,
    FORBIDDEN_TOOLS,
    SAFE_PERMISSION_MODES,
    SAFE_TOOLS,
    ClaudeConfigurationError,
    DEFAULT_PERMISSION_MODE,
    DEFAULT_TOOLS,
    ClaudeCodeConfig,
    ClaudeCodeError,
    ClaudeCodeExecutor,
    ClaudeFailure,
    build_argv,
    parse_cli_output,
    probe_version,
    resolve_executable,
)
from atlas.claude_prompt import STANDING_RULES, build_prompt
from atlas.config import WorkerConfig
from atlas.config import RunConfig
from atlas.execution_service import ExecutionService
from atlas.executor import ExecutorFailure, ExecutorRequest, StructuredCapture
from atlas.gitcmd import GitRunner
from atlas.implementation import ImplementationRunner
from atlas.intake import build_idempotency_key
from atlas.local_process import LocalProcessExecutor, read_log_tail
from atlas.parser import parse_issue_body
from atlas.redaction import redact
from atlas.reconciliation import RunReconciler
from atlas.schema import FAILURE_CATEGORIES, RunFailure, RunStatus
from atlas.store import SCHEMA_VERSION, ExecutionConflict, RunError, TaskStore, utcnow
from atlas.validation import validate_intake
from atlas.workspace import WorkspacePlanner
from atlas.workspace_service import WorkspaceService
from atlas.worktree_changes import (
    ImplementationOutcome,
    WorktreeState,
    capture_state,
    compare,
)
from tests.fixtures import make_issue

GIT_AVAILABLE = shutil.which("git") is not None
WORKER = "worker-a"
SRC = str(Path(__file__).resolve().parent.parent / "src")
REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, shell=False)


def write_fake_claude(directory: Path, name: str = "claude") -> Path:
    """`tests.fake_claude`를 실행하는 shim을 만듭니다.

    adapter는 실행 파일 경로만 받으므로, 플랫폼별로 실행 가능한 얇은 wrapper를
    둡니다. shim 안에서도 shell 확장에 사용자 데이터를 태우지 않습니다.
    """

    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path = directory / f"{name}.cmd"
        path.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" -m tests.fake_claude %*\r\n',
            encoding="utf-8",
        )
    else:
        path = directory / name
        path.write_text(
            "#!/bin/sh\n" f'exec "{sys.executable}" -m tests.fake_claude "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
    return path


class ExecutableResolutionTest(unittest.TestCase):
    """실행 파일을 cwd나 추측으로 정하지 않아야 합니다."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="atlas-claude-res-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_explicit_path_is_used(self):
        target = write_fake_claude(self.root / "bin")

        resolved = resolve_executable(ClaudeCodeConfig(executable=str(target)))

        self.assertEqual(Path(resolved), target)

    def test_missing_explicit_path_is_reported(self):
        config = ClaudeCodeConfig(executable=str(self.root / "nope"))

        with self.assertRaises(ClaudeCodeError) as caught:
            resolve_executable(config)

        self.assertEqual(caught.exception.failure, ClaudeFailure.EXECUTABLE_MISSING)

    def test_directory_is_not_accepted_as_executable(self):
        config = ClaudeCodeConfig(executable=str(self.root))

        with self.assertRaises(ClaudeCodeError) as caught:
            resolve_executable(config)

        self.assertEqual(caught.exception.failure, ClaudeFailure.EXECUTABLE_MISSING)

    def test_path_lookup_failure_is_reported(self):
        empty = str(self.root / "empty")

        with self.assertRaises(ClaudeCodeError) as caught:
            resolve_executable(ClaudeCodeConfig(), environ={"PATH": empty})

        self.assertEqual(caught.exception.failure, ClaudeFailure.EXECUTABLE_MISSING)


class VersionProbeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="atlas-claude-probe-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = REPO_ROOT + os.pathsep + SRC

    def shim(self, body_win: str, body_posix: str, name="claude") -> Path:
        if sys.platform == "win32":
            path = self.root / f"{name}.cmd"
            path.write_text("@echo off\r\n" + body_win + "\r\n", encoding="utf-8")
        else:
            path = self.root / name
            path.write_text("#!/bin/sh\n" + body_posix + "\n", encoding="utf-8")
            path.chmod(0o755)
        return path

    def test_probe_reads_the_version(self):
        target = write_fake_claude(self.root)

        info = probe_version(str(target), environment=self.env)

        self.assertIn("Claude Code", info.version)
        self.assertEqual(info.path, str(target))

    def test_nonzero_probe_is_classified(self):
        target = self.shim("exit 3", "exit 3")

        with self.assertRaises(ClaudeCodeError) as caught:
            probe_version(str(target), environment=self.env)

        self.assertEqual(caught.exception.failure, ClaudeFailure.VERSION_PROBE_FAILED)

    def test_other_cli_is_rejected(self):
        target = self.shim("echo some-other-tool 1.0", "echo some-other-tool 1.0")

        with self.assertRaises(ClaudeCodeError) as caught:
            probe_version(str(target), environment=self.env)

        self.assertEqual(caught.exception.failure, ClaudeFailure.UNSUPPORTED_CLI)

    def test_probe_has_its_own_timeout(self):
        target = self.shim(
            f'"{sys.executable}" -c "import time; time.sleep(30)"',
            f'exec "{sys.executable}" -c "import time; time.sleep(30)"',
        )

        with self.assertRaises(ClaudeCodeError) as caught:
            probe_version(str(target), timeout_seconds=2.0, environment=self.env)

        self.assertEqual(caught.exception.failure, ClaudeFailure.VERSION_PROBE_FAILED)


class ArgvConstructionTest(unittest.TestCase):
    """argv에는 고정 flag만 들어가야 합니다."""

    def test_non_interactive_flags_are_present(self):
        argv = build_argv("/bin/claude")

        self.assertEqual(argv[0], "/bin/claude")
        self.assertIn("--print", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--no-session-persistence", argv)

    def test_permissions_are_not_opened_wide(self):
        argv = build_argv("/bin/claude")

        self.assertIn("--permission-mode", argv)
        self.assertIn(DEFAULT_PERMISSION_MODE, argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--allow-dangerously-skip-permissions", argv)

    def test_tools_are_restricted_and_exclude_shell(self):
        argv = build_argv("/bin/claude")

        self.assertIn("--tools", argv)
        tools = argv[argv.index("--tools") + 1]
        self.assertEqual(tools, DEFAULT_TOOLS)
        self.assertNotIn("Bash", tools)

    def test_model_is_optional(self):
        self.assertNotIn("--model", build_argv("/bin/claude"))

        argv = build_argv("/bin/claude", ClaudeCodeConfig(model="sonnet"))

        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_argv_carries_no_prompt_or_user_text(self):
        argv = build_argv("/bin/claude")

        joined = " ".join(argv)
        for marker in ("Objective", "Acceptance", "Atlas Task", "\n"):
            self.assertNotIn(marker, joined)

    def test_argv_has_no_shell_metacharacters(self):
        for token in build_argv("/bin/claude"):
            self.assertNotIn("&", token)
            self.assertNotIn("|", token)
            self.assertNotIn(";", token)
            self.assertNotIn("`", token)


class PromptConstructionTest(unittest.TestCase):
    TASK = {
        "task_id": "ATLAS-0042",
        "repository": "owner/repo",
        "objective": "README.md에 한 줄 추가한다",
        "acceptance_criteria": [{"id": "AC-1", "description": "그 줄이 존재한다"}],
        "constraints": ["다른 파일을 바꾸지 않는다"],
        "allowed_scope": {"paths": ["README.md"]},
        "forbidden_scope": {"paths": [".github/**"]},
    }

    def prompt(self, **overrides):
        task = {**self.TASK, **overrides}
        return build_prompt(task, "run-77", branch="atlas/ATLAS-0042/run77")

    def test_task_contract_is_included(self):
        text = self.prompt()

        self.assertIn("ATLAS-0042", text)
        self.assertIn("run-77", text)
        self.assertIn("owner/repo", text)
        self.assertIn("atlas/ATLAS-0042/run77", text)
        self.assertIn("README.md에 한 줄 추가한다", text)
        self.assertIn("그 줄이 존재한다", text)
        self.assertIn("다른 파일을 바꾸지 않는다", text)

    def test_scopes_are_included(self):
        text = self.prompt()

        self.assertIn("Allowed Scope", text)
        self.assertIn("Forbidden Scope", text)
        self.assertIn(".github/**", text)

    def test_standing_rules_are_always_present(self):
        text = self.prompt(objective="", acceptance_criteria=[], constraints=[])

        for rule in STANDING_RULES:
            self.assertIn(rule, text)

    def test_commit_and_boundary_rules_are_stated(self):
        text = self.prompt()

        self.assertIn("git commit", text)
        self.assertIn("main/master", text)
        self.assertIn("작업 디렉터리 밖", text)

    def test_prompt_is_deterministic(self):
        self.assertEqual(self.prompt(), self.prompt())

    def test_long_objective_is_clipped(self):
        text = self.prompt(objective="가" * 20_000)

        self.assertLess(len(text), 20_000)
        self.assertIn("생략됨", text)

    def test_missing_fields_do_not_crash(self):
        text = build_prompt({}, "run-1")

        self.assertIn("Atlas Task", text)
        self.assertIn("명시되지 않음", text)


class CliOutputParsingTest(unittest.TestCase):
    """`subtype`이 아니라 `is_error`가 실패 신호입니다."""

    def test_success_payload(self):
        outcome = parse_cli_output(
            json.dumps({"is_error": False, "subtype": "success", "result": "했습니다", "num_turns": 3})
        )

        self.assertTrue(outcome.parsed)
        self.assertFalse(outcome.is_error)
        self.assertIsNone(outcome.failure)
        self.assertEqual(outcome.summary["num_turns"], 3)

    def test_error_payload_with_success_subtype_is_a_failure(self):
        outcome = parse_cli_output(
            json.dumps(
                {"is_error": True, "subtype": "success", "result": "model not found",
                 "terminal_reason": "api_error"}
            )
        )

        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.failure, ClaudeFailure.CLI_FAILED)

    def test_auth_error_is_separated(self):
        outcome = parse_cli_output(
            json.dumps({"is_error": True, "result": "You are not logged in. Please run /login"})
        )

        self.assertEqual(outcome.failure, ClaudeFailure.AUTH_UNAVAILABLE)

    def test_unparseable_output_is_classified(self):
        for text in ("", "not json", json.dumps([1, 2, 3])):
            with self.subTest(text=text):
                outcome = parse_cli_output(text)

                self.assertFalse(outcome.parsed)
                self.assertEqual(outcome.failure, ClaudeFailure.OUTPUT_UNPARSEABLE)

    def test_permission_denials_are_counted(self):
        outcome = parse_cli_output(
            json.dumps({"is_error": False, "permission_denials": [{"tool_name": "Read"}]})
        )

        self.assertEqual(outcome.permission_denials, 1)

    def test_result_summary_is_redacted_and_bounded(self):
        secret = "ghp_" + "Z" * 30
        outcome = parse_cli_output(
            json.dumps({"is_error": False, "result": f"token {secret} " + "긴 " * 500}),
            secrets=(secret,),
        )

        self.assertNotIn(secret, outcome.result_text)
        self.assertNotIn(secret, json.dumps(outcome.to_dict(), ensure_ascii=False))
        self.assertLessEqual(len(outcome.result_text), 400)


class FailureTaxonomyTest(unittest.TestCase):
    def test_every_claude_category_maps_to_a_known_run_category(self):
        for failure in ClaudeFailure:
            with self.subTest(failure=failure):
                self.assertIn(failure, CLAUDE_TO_RUN_CATEGORY)
                self.assertIn(CLAUDE_TO_RUN_CATEGORY[failure], FAILURE_CATEGORIES)

    def test_provider_categories_stay_out_of_the_generic_enum(self):
        generic = {member.value for member in ExecutorFailure}

        for failure in ClaudeFailure:
            self.assertNotIn(failure.value, generic)


class ConfigSelectionTest(unittest.TestCase):
    def test_executor_kind_comes_from_env(self):
        self.assertEqual(WorkerConfig.from_env({}).executor.kind, "mock")
        self.assertEqual(
            WorkerConfig.from_env({"ATLAS_EXECUTOR": "claude"}).executor.kind, "claude"
        )

    def test_unknown_executor_is_rejected(self):
        with self.assertRaises(ValueError):
            WorkerConfig.from_env({"ATLAS_EXECUTOR": "codex"})

    def test_claude_options_come_from_env(self):
        config = ClaudeCodeConfig.from_env(
            {"ATLAS_CLAUDE_EXECUTABLE": "/x/claude", "ATLAS_CLAUDE_MODEL": "opus"}
        )

        self.assertEqual(config.executable, "/x/claude")
        self.assertEqual(config.model, "opus")

    def test_claude_options_are_not_on_the_core_executor_config(self):
        fields = WorkerConfig().executor.__dataclass_fields__

        for name in fields:
            self.assertNotIn("claude", name)


class ConfigHardeningTest(unittest.TestCase):
    """환경변수로 안전 경계를 우회할 수 없어야 합니다."""

    def test_default_config_is_unchanged(self):
        config = ClaudeCodeConfig()

        self.assertEqual(config.permission_mode, DEFAULT_PERMISSION_MODE)
        self.assertEqual(config.tools, DEFAULT_TOOLS)

    def test_bypass_permissions_is_rejected(self):
        for mode in ("bypassPermissions", "bypasspermissions", "dontAsk", "auto"):
            with self.subTest(mode=mode):
                with self.assertRaises(ClaudeConfigurationError):
                    ClaudeCodeConfig.from_env({"ATLAS_CLAUDE_PERMISSION_MODE": mode})

    def test_dangerous_sounding_modes_are_rejected(self):
        with self.assertRaises(ClaudeConfigurationError):
            ClaudeCodeConfig(permission_mode="dangerously-skip")

    def test_unknown_permission_mode_is_rejected(self):
        for mode in ("plan", "manual", "", "   ", "acceptedits"):
            with self.subTest(mode=mode):
                with self.assertRaises(ClaudeConfigurationError):
                    ClaudeCodeConfig(permission_mode=mode)

    def test_safe_permission_mode_is_accepted(self):
        for mode in SAFE_PERMISSION_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(ClaudeCodeConfig(permission_mode=mode).permission_mode, mode)

    def test_shell_capable_tools_are_rejected(self):
        for tool in sorted(FORBIDDEN_TOOLS):
            with self.subTest(tool=tool):
                with self.assertRaises(ClaudeConfigurationError):
                    ClaudeCodeConfig.from_env({"ATLAS_CLAUDE_TOOLS": f"Read,Edit,{tool}"})

    def test_unknown_tool_fails_closed(self):
        for tools in ("Read,SomethingNew", "Read Bash", "bash", "", "   "):
            with self.subTest(tools=tools):
                with self.assertRaises(ClaudeConfigurationError):
                    ClaudeCodeConfig(tools=tools)

    def test_safe_subset_is_accepted(self):
        config = ClaudeCodeConfig.from_env({"ATLAS_CLAUDE_TOOLS": "Read,Grep"})

        self.assertEqual(config.tool_names, ("Read", "Grep"))
        self.assertTrue(set(config.tool_names) <= SAFE_TOOLS)

    def test_trailing_separators_are_ignored(self):
        self.assertEqual(ClaudeCodeConfig(tools="Read,,Grep,").tool_names, ("Read", "Grep"))

    def test_duplicates_are_collapsed(self):
        self.assertEqual(ClaudeCodeConfig(tools="Read,Read,Grep").tool_names, ("Read", "Grep"))

    def test_invalid_config_fails_at_preflight(self):
        adapter = ClaudeCodeExecutor(ClaudeCodeConfig())
        # 검증을 우회해 만든 설정이라도 preflight에서 막아야 합니다.
        object.__setattr__(adapter.config, "tools", "Read,Bash")

        with self.assertRaises(ClaudeConfigurationError):
            adapter.preflight()

    def test_normalized_policy_has_no_raw_config(self):
        policy = ClaudeCodeConfig(executable="/secret/path/claude", model="opus").normalized()

        self.assertEqual(policy["permission_mode"], DEFAULT_PERMISSION_MODE)
        self.assertEqual(policy["tools"], list(ClaudeCodeConfig().tool_names))
        self.assertNotIn("executable", policy)
        self.assertNotIn("/secret/path", json.dumps(policy))

    def test_argv_never_contains_forbidden_values(self):
        argv = " ".join(build_argv("/bin/claude"))

        for tool in FORBIDDEN_TOOLS:
            self.assertNotIn(tool, argv)
        self.assertNotIn("bypassPermissions", argv)


class StructuredCaptureTest(unittest.TestCase):
    """파싱 대상과 저장 대상을 분리해야 합니다."""

    PAYLOAD = {
        "is_error": False,
        "subtype": "success",
        "result": "Changed Authorization: Bearer abc123def456 safely",
    }

    def test_text_redaction_breaks_single_line_json(self):
        """이 테스트가 문제의 근거입니다. redaction 후 JSON이 깨집니다."""

        raw = json.dumps(self.PAYLOAD, ensure_ascii=False)

        with self.assertRaises(ValueError):
            json.loads(redact(raw))

    def test_capture_returns_raw_text_and_clears(self):
        capture = StructuredCapture()
        capture.feed(b'{"a": ')
        capture.feed(b'1}')

        self.assertEqual(capture.take(), '{"a": 1}')
        # 한 번 읽으면 비워집니다. raw 값을 오래 들고 있지 않습니다.
        self.assertEqual(capture.take(), "")

    def test_capture_is_bounded_and_fails_closed(self):
        capture = StructuredCapture(limit=32)
        capture.feed(b"x" * 100)

        self.assertTrue(capture.overflowed)
        self.assertEqual(capture.take(), "")

    def test_capture_never_exposes_content_in_to_dict(self):
        capture = StructuredCapture()
        capture.feed(b'{"result": "ghp_secretvalue1234567890"}')

        self.assertNotIn("ghp_", json.dumps(capture.to_dict()))

    def test_parsing_survives_authorization_header_in_result(self):
        raw = json.dumps(self.PAYLOAD, ensure_ascii=False)

        outcome = parse_cli_output(raw)

        self.assertTrue(outcome.parsed)
        self.assertFalse(outcome.is_error)
        # 요약은 여전히 redaction을 거칩니다.
        self.assertNotIn("abc123def456", outcome.result_text)


class ChangeDetectionTest(unittest.TestCase):
    """git 없이 판정 로직만 검증합니다."""

    def state(self, head="a" * 40, branch="atlas/t/r", entries=()):
        return WorktreeState(head=head, branch=branch, entries=tuple(entries))

    def test_no_changes_is_its_own_outcome(self):
        report = compare(self.state(), self.state())

        self.assertIs(report.outcome, ImplementationOutcome.NO_CHANGES)
        self.assertFalse(report.has_changes)

    def test_modified_file_is_detected(self):
        report = compare(self.state(), self.state(entries=(" M README.md",)))

        self.assertIs(report.outcome, ImplementationOutcome.CHANGES_APPLIED)
        self.assertEqual(report.changed_files, ("README.md",))

    def test_untracked_file_is_detected(self):
        report = compare(self.state(), self.state(entries=("?? new/file.py",)))

        self.assertEqual(report.changed_files, ("new/file.py",))

    def test_rename_reports_both_paths(self):
        report = compare(self.state(), self.state(entries=("R  old.py -> new.py",)))

        self.assertEqual(set(report.changed_files), {"old.py", "new.py"})

    def test_commit_is_detected_as_a_violation(self):
        report = compare(self.state(head="a" * 40), self.state(head="b" * 40))

        self.assertTrue(report.committed)
        self.assertIn("unexpected_commit", report.violations)
        self.assertIs(report.outcome, ImplementationOutcome.POLICY_VIOLATION)

    def test_branch_switch_is_detected(self):
        report = compare(self.state(), self.state(branch="main"))

        self.assertIn("branch_switched", report.violations)

    def test_git_internal_change_is_detected(self):
        report = compare(self.state(), self.state(entries=("?? .git/hooks/pre-commit",)))

        self.assertIn("git_internals_modified", report.violations)

    def test_forbidden_path_is_detected(self):
        task = {"forbidden_scope": {"paths": [".github/**"]}}

        report = compare(self.state(), self.state(entries=(" M .github/workflows/ci.yml",)), task)

        self.assertIn("forbidden_path_changed", report.violations)
        self.assertEqual(report.forbidden_paths, (".github/workflows/ci.yml",))

    def test_out_of_scope_path_is_detected(self):
        task = {"allowed_scope": {"paths": ["src/"]}}

        report = compare(self.state(), self.state(entries=(" M docs/other.md",)), task)

        self.assertIn("out_of_scope_path_changed", report.violations)

    def test_in_scope_path_is_allowed(self):
        task = {"allowed_scope": {"paths": ["src/**"]}}

        report = compare(self.state(), self.state(entries=(" M src/atlas/x.py",)), task)

        self.assertIs(report.outcome, ImplementationOutcome.CHANGES_APPLIED)
        self.assertEqual(report.out_of_scope_paths, ())

    def test_empty_allowed_scope_does_not_invent_violations(self):
        report = compare(self.state(), self.state(entries=(" M anywhere.py",)), {})

        self.assertIs(report.outcome, ImplementationOutcome.CHANGES_APPLIED)

    def test_failed_process_leaves_the_outcome_unknown(self):
        report = compare(
            self.state(), self.state(entries=(" M x.py",)), process_succeeded=False
        )

        self.assertIs(report.outcome, ImplementationOutcome.UNKNOWN)


@unittest.skipUnless(GIT_AVAILABLE, "git 실행 파일이 없습니다")
class SchemaMigrationTest(unittest.TestCase):
    """v5 database의 active Run 인덱스는 AwaitingValidation을 모릅니다."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="atlas-claude-migrate-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.db = str(self.root / "atlas.db")

    def test_v5_index_is_recreated(self):
        store = TaskStore(self.db)
        connection = store._connection
        # v5 정의로 되돌립니다.
        connection.execute("DROP INDEX idx_runs_active")
        connection.execute(
            "CREATE UNIQUE INDEX idx_runs_active ON runs(task_id) "
            "WHERE status IN ('Pending', 'Running')"
        )
        connection.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")
        connection.commit()
        store.close()

        migrated = TaskStore(self.db)
        self.addCleanup(migrated.close)

        sql = migrated._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_runs_active'"
        ).fetchone()["sql"]
        version = migrated._connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]

        self.assertIn("AwaitingValidation", sql)
        self.assertEqual(version, SCHEMA_VERSION)

    def test_recreation_is_idempotent(self):
        store = TaskStore(self.db)
        first = store._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_runs_active'"
        ).fetchone()["sql"]
        store.close()

        again = TaskStore(self.db)
        self.addCleanup(again.close)
        second = again._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_runs_active'"
        ).fetchone()["sql"]

        self.assertEqual(first, second)


class ClaudeIntegrationTestCase(unittest.TestCase):
    """가짜 Claude 실행 파일로 adapter 전체 경로를 확인합니다."""

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

        self.fake = write_fake_claude(self.root / "bin")
        self.db = str(self.root / "atlas.db")
        self.store = TaskStore(self.db)
        self.planner = WorkspacePlanner(self.repo, self.root / "wt", base_branch="main")
        self.workspaces = WorkspaceService(self.store, self.planner)
        self.service = ExecutionService(
            self.store,
            ClaudeCodeExecutor(ClaudeCodeConfig(executable=str(self.fake))),
            self.workspaces,
            self.root / "logs",
            RunConfig(heartbeat_interval_seconds=1.0, stale_after_seconds=60.0),
        )
        self.adapter = ClaudeCodeExecutor(ClaudeCodeConfig(executable=str(self.fake)))
        self.runner = ImplementationRunner(self.store, self.service, self.adapter)
        self.addCleanup(self._teardown)
        self.run = self.prepare()

    def _teardown(self):
        try:
            for row in self.store.active_executions():
                try:
                    self.service.cancel(row["run_id"], "teardown", 1.0)
                except Exception:  # noqa: BLE001
                    pass
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
        self.store.claim(WORKER, 900, task_id=task_id)
        run = self.store.start_run(task_id, WORKER)
        self.workspaces.create(run.run_id)
        return self.store.run(run.run_id)

    def env(self, **extra):
        base = {
            "PYTHONPATH": REPO_ROOT + os.pathsep + SRC,
            "FAKE_CLAUDE_MODE": "success",
        }
        base.update({k: str(v) for k, v in extra.items()})
        return base

    def implement(self, run=None, timeout=60.0, **env):
        target = run or self.run
        return self.runner.run(
            target.run_id, WORKER, timeout_seconds=timeout, environment=self.env(**env)
        )


class ClaudeInvocationTest(ClaudeIntegrationTestCase):
    def test_prompt_reaches_the_cli_through_stdin(self):
        echo = self.root / "prompt.txt"

        self.implement(FAKE_CLAUDE_ECHO=str(echo), FAKE_CLAUDE_WRITE="docs/note.md")

        text = echo.read_text(encoding="utf-8")
        self.assertIn("Atlas Task", text)
        self.assertIn(self.run.run_id, text)
        self.assertIn("git commit", text)

    def test_argv_carries_flags_only(self):
        record = self.root / "argv.json"

        self.implement(FAKE_CLAUDE_ARGV=str(record), FAKE_CLAUDE_WRITE="docs/note.md")

        argv = json.loads(record.read_text(encoding="utf-8"))
        self.assertIn("--print", argv)
        self.assertIn("--permission-mode", argv)
        self.assertNotIn("Atlas Task", " ".join(argv))

    def test_cwd_is_the_run_worktree(self):
        report = self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertTrue((Path(self.run.worktree_path) / "docs" / "note.md").exists())
        self.assertTrue(report.implemented)

    def test_files_are_modified_inside_the_worktree(self):
        self.implement(FAKE_CLAUDE_WRITE="docs/a.md:src/atlas/b.py")

        worktree = Path(self.run.worktree_path)
        self.assertTrue((worktree / "docs" / "a.md").exists())
        self.assertTrue((worktree / "src" / "atlas" / "b.py").exists())

    def test_stdout_and_stderr_are_captured(self):
        report = self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertIsNotNone(report.result.stdout)
        self.assertIsNotNone(report.result.stderr)
        self.assertIn("result", read_log_tail(report.result.stdout.path))

    def test_main_worktree_is_untouched(self):
        before_head = GitRunner(self.repo).head_revision()

        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((self.repo / "docs" / "note.md").exists())
        self.assertEqual(GitRunner(self.repo).head_revision(), before_head)
        self.assertFalse(GitRunner(self.repo).is_dirty())

    def test_other_worktrees_are_untouched(self):
        other = self.prepare(number=77)

        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertFalse((Path(other.worktree_path) / "docs" / "note.md").exists())

    def test_branch_stays_on_the_atlas_branch(self):
        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertEqual(
            GitRunner(self.run.worktree_path).current_branch(), self.run.branch
        )

    def test_duplicate_start_is_still_rejected(self):
        request = ExecutorRequest(
            run_id=self.run.run_id,
            task_id=self.run.task_id,
            cwd=self.run.worktree_path,
            argv=(),
            timeout_seconds=60.0,
        )
        prepared = self.adapter.build_request(request, "prompt", environment=self.env())
        self.service.start(
            self.run.run_id,
            WORKER,
            prepared.argv,
            stdin_data="prompt",
            environment=self.env(FAKE_CLAUDE_MODE="sleep", FAKE_CLAUDE_SLEEP=30),
            timeout_seconds=60.0,
        )

        with self.assertRaises(ExecutionConflict):
            self.service.start(
                self.run.run_id, WORKER, prepared.argv, stdin_data="prompt",
                environment=self.env(), timeout_seconds=60.0,
            )

        self.service.cancel(self.run.run_id, "정리", 2.0)


class ImplementationResultTest(ClaudeIntegrationTestCase):
    def test_changes_applied_does_not_finish_the_run(self):
        report = self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertIs(report.outcome, ImplementationOutcome.CHANGES_APPLIED)
        # validation이 아직 없으므로 Run을 성공으로 확정하지 않습니다.
        run = self.store.run(self.run.run_id)
        self.assertIs(run.status, RunStatus.AWAITING_VALIDATION)
        self.assertFalse(run.status.is_terminal)

    def test_no_op_is_not_reported_as_success(self):
        report = self.implement(FAKE_CLAUDE_MODE="nochange")

        self.assertIs(report.outcome, ImplementationOutcome.NO_CHANGES)
        self.assertFalse(report.implemented)
        self.assertEqual(report.failure, ClaudeFailure.NO_CHANGES)
        self.assertTrue(report.process_succeeded)
        self.assertIs(self.store.run(self.run.run_id).status, RunStatus.FAILED)

    def test_nonzero_exit_is_a_cli_failure(self):
        report = self.implement(FAKE_CLAUDE_MODE="fail")

        self.assertFalse(report.process_succeeded)
        self.assertIs(report.outcome, ImplementationOutcome.UNKNOWN)
        self.assertEqual(report.failure, ClaudeFailure.CLI_FAILED)
        self.assertIs(self.store.run(self.run.run_id).status, RunStatus.FAILED)

    def test_unparseable_output_is_classified(self):
        report = self.implement(FAKE_CLAUDE_MODE="badjson", FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertEqual(report.claude.failure, ClaudeFailure.OUTPUT_UNPARSEABLE)

    def test_timeout_is_classified_as_claude_timeout(self):
        report = self.implement(
            timeout=3.0, FAKE_CLAUDE_MODE="sleep", FAKE_CLAUDE_SLEEP=60
        )

        self.assertEqual(report.failure, ClaudeFailure.TIMEOUT)
        self.assertIs(self.store.run(self.run.run_id).status, RunStatus.FAILED)
        self.assertEqual(self.store.run(self.run.run_id).failure_category, "timeout")

    def test_changed_path_outside_allowed_scope_is_detected(self):
        """fixture Task의 allowed_scope는 docs/**, src/** 입니다."""

        report = self.implement(FAKE_CLAUDE_WRITE="unexpected.txt")

        self.assertIs(report.outcome, ImplementationOutcome.POLICY_VIOLATION)
        self.assertIn("out_of_scope_path_changed", report.changes.violations)
        self.assertEqual(report.changes.out_of_scope_paths, ("unexpected.txt",))
        self.assertEqual(report.failure, ClaudeFailure.POLICY_VIOLATION)
        # 탐지만 하고 되돌리지 않습니다.
        self.assertTrue((Path(self.run.worktree_path) / "unexpected.txt").exists())

    def test_forbidden_path_change_is_detected(self):
        report = self.implement(FAKE_CLAUDE_WRITE="secrets/leak.txt")

        self.assertIn("forbidden_path_changed", report.changes.violations)

    def test_unexpected_commit_is_detected(self):
        report = self.implement(FAKE_CLAUDE_MODE="commit", FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertIs(report.outcome, ImplementationOutcome.POLICY_VIOLATION)
        self.assertIn("unexpected_commit", report.changes.violations)
        self.assertEqual(
            self.store.run(self.run.run_id).failure_category, "policy_violation"
        )

    def test_result_is_recorded_as_an_event_without_the_full_response(self):
        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        rows = [r for r in self.store.events() if r["kind"] == "implementation_completed"]
        self.assertEqual(len(rows), 1)
        detail = rows[0]["detail"]
        self.assertIn("implementation_outcome", detail)
        self.assertIn("changes", detail)
        self.assertLess(len(detail), 8000)

    def test_no_raw_secret_is_persisted(self):
        secret = "ghp_" + "Q" * 30
        report = self.runner.run(
            self.run.run_id,
            WORKER,
            timeout_seconds=60.0,
            environment=self.env(FAKE_CLAUDE_WRITE="docs/note.md", FAKE_CLAUDE_TEXT=secret),
            secret_values=(secret,),
        )

        blob = json.dumps([dict(r) for r in self.store.events()], ensure_ascii=False)
        blob += Path(self.db).read_bytes().decode("latin-1")
        blob += read_log_tail(report.result.stdout.path, max_bytes=1_000_000)
        self.assertNotIn(secret, blob)


class StructuredParsingIntegrationTest(ClaudeIntegrationTestCase):
    """persisted log는 redacted, 파싱은 원문에서."""

    SECRET = "ghp_" + "K" * 32

    def implement_with_result(self, text, secrets=()):
        return self.runner.run(
            self.run.run_id,
            WORKER,
            timeout_seconds=60.0,
            secret_values=tuple(secrets),
            environment=self.env(
                FAKE_CLAUDE_WRITE="docs/note.md", FAKE_CLAUDE_RESULT=text
            ),
        )

    def persisted(self, report):
        return Path(report.result.stdout.path).read_text(encoding="utf-8", errors="replace")

    def test_authorization_header_in_result_still_parses(self):
        report = self.implement_with_result("Changed Authorization: Bearer abc123def456 safely")

        self.assertTrue(report.claude.parsed, "redacted log를 파싱하면 여기서 깨집니다")
        self.assertIsNone(report.claude.failure)
        self.assertIs(report.outcome, ImplementationOutcome.CHANGES_APPLIED)
        self.assertNotIn("abc123def456", self.persisted(report))

    def test_provider_token_in_result_still_parses(self):
        report = self.implement_with_result("wrote " + self.SECRET + " to config")

        self.assertTrue(report.claude.parsed)
        self.assertNotIn(self.SECRET, self.persisted(report))
        self.assertNotIn(self.SECRET, report.claude.result_text)

    def test_url_credential_in_result_still_parses(self):
        report = self.implement_with_result(
            "cloned https://user:tokenvalue123@example.com/x.git"
        )

        self.assertTrue(report.claude.parsed)
        self.assertNotIn("tokenvalue123", self.persisted(report))

    def test_injected_secret_is_not_persisted_anywhere(self):
        secret = "inject3d-" + ("V" * 24)
        report = self.implement_with_result("used " + secret + " once", secrets=(secret,))

        self.assertTrue(report.claude.parsed)
        blob = self.persisted(report)
        blob += json.dumps([dict(r) for r in self.store.events()], ensure_ascii=False)
        blob += Path(self.db).read_bytes().decode("latin-1")
        self.assertNotIn(secret, blob)

    def test_malformed_json_is_unparseable(self):
        report = self.implement(FAKE_CLAUDE_MODE="badjson", FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertFalse(report.claude.parsed)
        self.assertEqual(report.claude.failure, ClaudeFailure.OUTPUT_UNPARSEABLE)

    def test_oversized_json_fails_closed(self):
        adapter = ClaudeCodeExecutor(
            ClaudeCodeConfig(executable=str(self.fake), structured_output_bytes=4096)
        )
        runner = ImplementationRunner(self.store, self.service, adapter)

        report = runner.run(
            self.run.run_id,
            WORKER,
            timeout_seconds=60.0,
            environment=self.env(FAKE_CLAUDE_MODE="huge", FAKE_CLAUDE_HUGE_BYTES=200000),
        )

        self.assertFalse(report.claude.parsed)
        self.assertEqual(report.claude.failure, ClaudeFailure.OUTPUT_TOO_LARGE)

    def test_effective_policy_is_recorded_normalized(self):
        self.implement_with_result("done")

        rows = [r for r in self.store.events() if r["kind"] == "implementation_completed"]
        detail = json.loads(rows[0]["detail"])
        self.assertEqual(detail["effective_policy"]["permission_mode"], DEFAULT_PERMISSION_MODE)
        self.assertNotIn("Bash", detail["effective_policy"]["tools"])
        self.assertNotIn(str(self.fake), rows[0]["detail"])


class AwaitingValidationTest(ClaudeIntegrationTestCase):
    """구현 완료는 terminal도 아니고 stale도 아닙니다."""

    def test_changes_applied_moves_to_awaiting_validation(self):
        report = self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertIs(report.outcome, ImplementationOutcome.CHANGES_APPLIED)
        run = self.store.run(self.run.run_id)
        self.assertIs(run.status, RunStatus.AWAITING_VALIDATION)
        self.assertFalse(run.status.is_terminal)
        self.assertTrue(run.status.is_active)
        self.assertFalse(run.status.expects_heartbeat)

    def test_it_is_not_orphaned_after_the_stale_threshold(self):
        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")
        later = utcnow() + timedelta(seconds=10000)

        reconciler = RunReconciler(
            self.store, RunConfig(heartbeat_interval_seconds=1.0, stale_after_seconds=60.0)
        )
        reconciler.reconcile(now=later)

        self.assertIs(self.store.run(self.run.run_id).status, RunStatus.AWAITING_VALIDATION)

    def test_orphan_if_stale_refuses_the_state(self):
        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")
        run = self.store.run(self.run.run_id)

        orphaned = self.store.orphan_if_stale(
            run.run_id,
            observed_heartbeat_at=run.heartbeat_at,
            stale_after_seconds=0.0,
            failure=RunFailure("worker_lost", "강제 시도"),
            evidence={},
            now=utcnow() + timedelta(seconds=10000),
        )

        self.assertIsNone(orphaned)
        self.assertIs(self.store.run(run.run_id).status, RunStatus.AWAITING_VALIDATION)

    def test_no_active_execution_remains(self):
        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertIsNone(self.store.active_execution(self.run.run_id))
        self.assertEqual(self.store.active_executions(), [])

    def test_workspace_is_kept(self):
        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        run = self.store.run(self.run.run_id)
        self.assertTrue(Path(run.worktree_path).exists())
        self.assertEqual(run.workspace_status.value, "ready")
        self.assertEqual(GitRunner(run.worktree_path).current_branch(), run.branch)

    def test_it_still_holds_the_task_run_slot(self):
        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        with self.assertRaises(RunError):
            self.store.start_run(self.run.task_id, WORKER)

    def test_transition_is_idempotent(self):
        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        again = self.store.await_validation(self.run.run_id)

        self.assertIs(again.status, RunStatus.AWAITING_VALIDATION)

    def test_it_can_still_be_finished_later(self):
        """다음 validation slice가 여기서 이어받습니다."""

        self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        finished = self.store.finish_run(self.run.run_id, RunStatus.SUCCEEDED)

        self.assertIs(finished.status, RunStatus.SUCCEEDED)

    def test_failures_do_not_use_the_new_state(self):
        report = self.implement(FAKE_CLAUDE_MODE="nochange")

        self.assertIs(report.outcome, ImplementationOutcome.NO_CHANGES)
        self.assertIs(self.store.run(self.run.run_id).status, RunStatus.FAILED)


class WorktreeStateCaptureTest(ClaudeIntegrationTestCase):
    def test_state_is_captured_before_and_after(self):
        report = self.implement(FAKE_CLAUDE_WRITE="docs/note.md")

        self.assertIsNotNone(report.changes.before)
        self.assertIsNotNone(report.changes.after)
        self.assertEqual(report.changes.before.branch, self.run.branch)
        self.assertFalse(report.changes.head_changed)
        self.assertEqual(report.changes.changed_files, ("docs/note.md",))

    def test_capture_state_reads_head_branch_and_status(self):
        worktree = Path(self.run.worktree_path)
        (worktree / "dirty.txt").write_text("x\n", encoding="utf-8")

        state = capture_state(worktree)

        self.assertEqual(state.branch, self.run.branch)
        self.assertEqual(len(state.head), 40)
        self.assertTrue(any("dirty.txt" in entry for entry in state.entries))


if __name__ == "__main__":
    unittest.main()
