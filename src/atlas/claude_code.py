"""Claude Code CLI를 ExecutorAdapter로 연결합니다.

ADR-003의 "primary automated executor는 self-hosted Claude Code"를 실제
구현으로 옮깁니다. provider 세부사항은 이 파일 안에만 둡니다. core contract
(`executor.py`)에는 Claude 옵션이 들어가지 않습니다.

process lifecycle은 새로 만들지 않고 `LocalProcessExecutor`를 composition
합니다. timeout, cancellation, process tree 종료, identity 확인, output
redaction, restart reconciliation이 모두 그대로 적용됩니다.

## 실측으로 확인한 CLI 동작

`docs/verification-log.md`에 기록한 실측 결과입니다.

- `-p/--print`가 비대화형 모드입니다. prompt는 argv 없이 stdin으로 넘길 수
  있습니다. Issue 본문은 사용자 입력이므로 argv에 넣지 않습니다.
- `--output-format json`이 구조화된 결과를 줍니다.
- `subtype`은 실패 신호가 아닙니다. 모델 오류에서도 `subtype=success`가
  나왔고 `is_error=true`가 실제 신호였습니다.
- `permission_denials`에 차단된 tool 호출이 남습니다.
- cwd 밖 파일 접근은 CLI가 자체적으로 거부합니다.
- `--tools`로 도구를 제한할 수 있습니다. Bash를 빼면 CLI가 git commit을
  실행할 수단이 없습니다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .executor import (
    CancellationState,
    ExecutorError,
    ExecutorFailure,
    ExecutorRequest,
    ExecutorResult,
    ProcessHandle,
    TerminationOutcome,
)
from .local_process import LocalProcessExecutor, read_log_tail
from .redaction import redact_line

EXECUTOR_NAME = "claude_code_local"
EXECUTOR_PROVIDER = "anthropic"

# 편집에 필요한 도구만 남깁니다. Bash를 주지 않으므로 CLI가 git commit이나
# push를 실행할 수단이 없습니다. 권한을 넓게 여는 대신 도구를 좁힙니다.
DEFAULT_TOOLS = "Read,Edit,Write,Glob,Grep"

# 파일 편집만 승인 없이 허용합니다. bypassPermissions와
# --dangerously-skip-permissions는 쓰지 않습니다.
DEFAULT_PERMISSION_MODE = "acceptEdits"

DEFAULT_VERSION_PROBE_TIMEOUT_SECONDS = 30.0

# version probe 출력이 이 문자열을 포함해야 Claude Code로 인정합니다.
VERSION_MARKER = "Claude Code"

# 실행 결과 JSON에서 요약으로 남길 필드. 응답 전체를 event에 넣지 않습니다.
SUMMARY_FIELDS = (
    "is_error",
    "subtype",
    "num_turns",
    "session_id",
    "stop_reason",
    "terminal_reason",
    "api_error_status",
    "total_cost_usd",
    "duration_ms",
)

# 요약에 담을 result 텍스트 길이 상한.
MAX_SUMMARY_CHARS = 400


class ClaudeFailure(str, Enum):
    """Claude 특유의 실패 분류.

    generic `ExecutorFailure`와 분리합니다. provider별 어휘를 core taxonomy에
    섞으면 다른 adapter가 쓸 수 없는 값이 생깁니다.
    """

    EXECUTABLE_MISSING = "claude_executable_missing"
    VERSION_PROBE_FAILED = "claude_version_probe_failed"
    UNSUPPORTED_CLI = "claude_unsupported_cli"
    AUTH_UNAVAILABLE = "claude_auth_unavailable"
    CLI_FAILED = "claude_cli_failed"
    TIMEOUT = "claude_timeout"
    NO_CHANGES = "claude_no_changes"
    POLICY_VIOLATION = "claude_policy_violation"
    OUTPUT_UNPARSEABLE = "claude_output_unparseable"


# provider category를 generic executor category로 옮깁니다. Run failure는
# 다시 generic category에서 FAILURE_TO_RUN_CATEGORY로 매핑됩니다.
CLAUDE_TO_EXECUTOR_FAILURE: dict[ClaudeFailure, ExecutorFailure] = {
    ClaudeFailure.EXECUTABLE_MISSING: ExecutorFailure.SPAWN_FAILED,
    ClaudeFailure.VERSION_PROBE_FAILED: ExecutorFailure.SPAWN_FAILED,
    ClaudeFailure.UNSUPPORTED_CLI: ExecutorFailure.SPAWN_FAILED,
    ClaudeFailure.AUTH_UNAVAILABLE: ExecutorFailure.UNKNOWN,
    ClaudeFailure.CLI_FAILED: ExecutorFailure.NONZERO_EXIT,
    ClaudeFailure.TIMEOUT: ExecutorFailure.TIMEOUT,
    ClaudeFailure.NO_CHANGES: ExecutorFailure.UNKNOWN,
    ClaudeFailure.POLICY_VIOLATION: ExecutorFailure.SAFETY_GATE,
    ClaudeFailure.OUTPUT_UNPARSEABLE: ExecutorFailure.UNKNOWN,
}

# docs/specs/task-state-machine.md의 Failure Taxonomy 어휘로만 옮깁니다.
CLAUDE_TO_RUN_CATEGORY: dict[ClaudeFailure, str] = {
    ClaudeFailure.EXECUTABLE_MISSING: "transient_executor",
    ClaudeFailure.VERSION_PROBE_FAILED: "transient_executor",
    ClaudeFailure.UNSUPPORTED_CLI: "transient_executor",
    ClaudeFailure.AUTH_UNAVAILABLE: "authentication",
    ClaudeFailure.CLI_FAILED: "transient_executor",
    ClaudeFailure.TIMEOUT: "timeout",
    ClaudeFailure.NO_CHANGES: "unknown",
    ClaudeFailure.POLICY_VIOLATION: "policy_violation",
    ClaudeFailure.OUTPUT_UNPARSEABLE: "unknown",
}

# CLI 출력에서 인증 문제로 볼 표지입니다. 원문을 저장하지 않고 분류만
# 남기기 위한 판정용입니다.
AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "authentication",
    "unauthorized",
    "invalid api key",
    "credit balance",
)


class ClaudeCodeError(ExecutorError):
    """Claude adapter 고유 실패. category는 ClaudeFailure 값입니다."""

    def __init__(self, failure: ClaudeFailure, message: str) -> None:
        super().__init__(failure.value, message)
        self.failure = failure


@dataclass(frozen=True)
class ClaudeCodeConfig:
    """Claude Code 실행 정책.

    provider별 설정이므로 core `ExecutorConfig`가 아니라 adapter 경계에 둡니다.
    """

    # 명시적 경로. 비어 있으면 PATH에서 찾습니다. cwd로 추측하지 않습니다.
    executable: str | None = None
    model: str | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    tools: str = DEFAULT_TOOLS
    version_probe_timeout_seconds: float = DEFAULT_VERSION_PROBE_TIMEOUT_SECONDS
    # 세션을 디스크에 남기지 않습니다. Run 사이에 대화가 이어지면 격리가
    # 깨집니다. Run 하나가 곧 대화 하나입니다.
    session_persistence: bool = False
    max_budget_usd: float | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ClaudeCodeConfig:
        env = environ if environ is not None else dict(os.environ)
        config = cls()
        if path := env.get("ATLAS_CLAUDE_EXECUTABLE", "").strip():
            config = replace(config, executable=path)
        if model := env.get("ATLAS_CLAUDE_MODEL", "").strip():
            config = replace(config, model=model)
        if mode := env.get("ATLAS_CLAUDE_PERMISSION_MODE", "").strip():
            config = replace(config, permission_mode=mode)
        if tools := env.get("ATLAS_CLAUDE_TOOLS", "").strip():
            config = replace(config, tools=tools)
        return config


@dataclass(frozen=True)
class ExecutableInfo:
    """resolve된 실행 파일과 version probe 결과."""

    path: str
    version: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "version": self.version}


def resolve_executable(
    config: ClaudeCodeConfig | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    """Claude 실행 파일 경로를 확정합니다.

    cwd에서 찾지 않습니다. 명시적 설정이 우선이고, 없으면 PATH에서만 찾습니다.
    """

    config = config or ClaudeCodeConfig()
    explicit = (config.executable or "").strip()
    if explicit:
        candidate = Path(explicit)
        if not candidate.exists():
            raise ClaudeCodeError(
                ClaudeFailure.EXECUTABLE_MISSING,
                f"지정한 claude 실행 파일이 없습니다: {candidate}",
            )
        if not candidate.is_file():
            raise ClaudeCodeError(
                ClaudeFailure.EXECUTABLE_MISSING,
                f"claude 실행 파일이 일반 파일이 아닙니다: {candidate}",
            )
        return str(candidate)

    path_value = (environ or {}).get("PATH") if environ else None
    found = shutil.which("claude", path=path_value)
    if not found:
        raise ClaudeCodeError(
            ClaudeFailure.EXECUTABLE_MISSING,
            "PATH에서 claude 실행 파일을 찾지 못했습니다. "
            "ATLAS_CLAUDE_EXECUTABLE로 경로를 지정하세요.",
        )
    return found


def probe_version(
    executable: str,
    timeout_seconds: float = DEFAULT_VERSION_PROBE_TIMEOUT_SECONDS,
    environment: dict[str, str] | None = None,
) -> ExecutableInfo:
    """`claude --version`으로 실행 가능 여부와 종류를 확인합니다.

    probe 자체에도 timeout을 겁니다. 응답 없는 CLI가 worker를 잡아 두면
    안 됩니다.
    """

    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        raise ClaudeCodeError(
            ClaudeFailure.VERSION_PROBE_FAILED,
            f"claude --version이 {timeout_seconds}초 안에 끝나지 않았습니다.",
        ) from None
    except OSError as error:
        raise ClaudeCodeError(
            ClaudeFailure.EXECUTABLE_MISSING,
            f"claude를 실행하지 못했습니다: {type(error).__name__}",
        ) from None

    if completed.returncode != 0:
        raise ClaudeCodeError(
            ClaudeFailure.VERSION_PROBE_FAILED,
            f"claude --version이 exit {completed.returncode}로 끝났습니다: "
            f"{redact_line(completed.stderr or completed.stdout, limit=120)}",
        )

    lines = (completed.stdout or "").strip().splitlines()
    version = lines[0].strip() if lines else ""
    if VERSION_MARKER not in version:
        raise ClaudeCodeError(
            ClaudeFailure.UNSUPPORTED_CLI,
            f"Claude Code CLI가 아닌 것 같습니다: {redact_line(version, limit=120)}",
        )
    return ExecutableInfo(path=executable, version=version)


def build_argv(executable: str, config: ClaudeCodeConfig | None = None) -> tuple[str, ...]:
    """비대화형 실행 argv를 만듭니다.

    argv에는 사용자 유래 데이터를 넣지 않습니다. prompt는 stdin으로
    전달합니다. Windows에서 claude가 `.CMD` wrapper로 해석되면 cmd.exe가
    argument를 다시 파싱하므로, argv에 Issue 본문이 있으면 metacharacter
    해석에 노출됩니다. 여기에는 고정 flag만 들어갑니다.
    """

    config = config or ClaudeCodeConfig()
    argv: list[str] = [
        executable,
        # 비대화형. TTY를 요구하지 않고 응답 후 종료합니다.
        "--print",
        "--output-format",
        "json",
        # 편집만 자동 승인합니다. 전체 우회가 아닙니다.
        "--permission-mode",
        config.permission_mode,
    ]
    if config.tools:
        argv += ["--tools", config.tools]
    if not config.session_persistence:
        argv.append("--no-session-persistence")
    if config.model:
        argv += ["--model", config.model]
    if config.max_budget_usd:
        argv += ["--max-budget-usd", str(config.max_budget_usd)]
    return tuple(argv)


@dataclass(frozen=True)
class ClaudeOutcome:
    """CLI 결과 JSON에서 뽑은 요약.

    응답 전체를 담지 않습니다. 전문은 log artifact에 남기고 여기에는 분류와
    짧은 요약만 둡니다.
    """

    parsed: bool
    is_error: bool = False
    summary: dict[str, Any] = field(default_factory=dict)
    result_text: str = ""
    permission_denials: int = 0
    failure: ClaudeFailure | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "parsed": self.parsed,
            "is_error": self.is_error,
            "permission_denials": self.permission_denials,
            "failure": self.failure.value if self.failure else None,
            "result_summary": self.result_text,
            **self.summary,
        }


def parse_cli_output(text: str, secrets: tuple[str, ...] = ()) -> ClaudeOutcome:
    """`--output-format json` 출력을 해석합니다.

    실측 결과 `subtype`은 실패 신호가 아닙니다. 모델 오류에서도
    `subtype=success`가 나왔고 `is_error`가 실제 신호였습니다.
    """

    stripped = (text or "").strip()
    if not stripped:
        return ClaudeOutcome(parsed=False, failure=ClaudeFailure.OUTPUT_UNPARSEABLE)
    try:
        payload = json.loads(stripped)
    except (ValueError, TypeError):
        return ClaudeOutcome(parsed=False, failure=ClaudeFailure.OUTPUT_UNPARSEABLE)
    if not isinstance(payload, dict):
        return ClaudeOutcome(parsed=False, failure=ClaudeFailure.OUTPUT_UNPARSEABLE)

    summary = {key: payload.get(key) for key in SUMMARY_FIELDS if key in payload}
    denials = payload.get("permission_denials")
    is_error = bool(payload.get("is_error"))
    result_text = redact_line(
        str(payload.get("result") or ""), limit=MAX_SUMMARY_CHARS, secrets=secrets
    )

    failure: ClaudeFailure | None = None
    if is_error:
        haystack = f"{result_text} {payload.get('terminal_reason') or ''}".lower()
        failure = (
            ClaudeFailure.AUTH_UNAVAILABLE
            if any(marker in haystack for marker in AUTH_MARKERS)
            else ClaudeFailure.CLI_FAILED
        )

    return ClaudeOutcome(
        parsed=True,
        is_error=is_error,
        summary=summary,
        result_text=result_text,
        permission_denials=len(denials) if isinstance(denials, list) else 0,
        failure=failure,
    )


def classify_failure(
    result: ExecutorResult, outcome: ClaudeOutcome, stderr_tail: str = ""
) -> ClaudeFailure | None:
    """process 결과와 CLI 출력을 합쳐 provider category를 정합니다."""

    if result.failure is ExecutorFailure.TIMEOUT:
        return ClaudeFailure.TIMEOUT
    if result.failure is ExecutorFailure.CANCELLED:
        return None
    if outcome.failure is not None:
        return outcome.failure
    if result.exit_code not in (0, None):
        haystack = (stderr_tail or "").lower()
        if any(marker in haystack for marker in AUTH_MARKERS):
            return ClaudeFailure.AUTH_UNAVAILABLE
        return ClaudeFailure.CLI_FAILED
    if not outcome.parsed:
        return ClaudeFailure.OUTPUT_UNPARSEABLE
    return None


class ClaudeCodeExecutor:
    """Claude Code CLI를 실행하는 ExecutorAdapter.

    process 관리는 `LocalProcessExecutor`에 위임합니다. Claude 전용 process
    manager를 만들지 않습니다. reconciliation, identity 확인, cancel 경로가
    갈라지면 안 됩니다.
    """

    def __init__(
        self,
        config: ClaudeCodeConfig | None = None,
        runtime: LocalProcessExecutor | None = None,
    ) -> None:
        self._config = config or ClaudeCodeConfig()
        self._runtime = runtime or LocalProcessExecutor(
            name=EXECUTOR_NAME, provider=EXECUTOR_PROVIDER
        )
        self._executable: ExecutableInfo | None = None

    @property
    def name(self) -> str:
        return EXECUTOR_NAME

    @property
    def provider(self) -> str:
        return EXECUTOR_PROVIDER

    @property
    def config(self) -> ClaudeCodeConfig:
        return self._config

    def preflight(self, environment: dict[str, str] | None = None) -> ExecutableInfo:
        """실행 파일을 찾고 version probe까지 마칩니다. 결과를 캐시합니다."""

        if self._executable is None:
            path = resolve_executable(self._config)
            self._executable = probe_version(
                path,
                timeout_seconds=self._config.version_probe_timeout_seconds,
                environment=environment,
            )
        return self._executable

    def build_request(
        self,
        base: ExecutorRequest,
        prompt: str,
        environment: dict[str, str] | None = None,
    ) -> ExecutorRequest:
        """provider-neutral 요청에 Claude argv와 prompt를 채웁니다."""

        info = self.preflight(environment)
        return replace(base, argv=build_argv(info.path, self._config), stdin_data=prompt)

    # -- ExecutorAdapter -------------------------------------------------

    def spawn(self, request: ExecutorRequest, log_dir: Path) -> ProcessHandle:
        if not request.stdin_data:
            # prompt 없이 실행하면 CLI가 stdin을 기다리거나 빈 turn을 돕니다.
            raise ClaudeCodeError(
                ClaudeFailure.CLI_FAILED, "prompt가 비어 있습니다. stdin_data가 필요합니다."
            )
        return self._runtime.spawn(request, log_dir)

    def wait(self, handle: ProcessHandle, request: ExecutorRequest) -> ExecutorResult:
        return self._runtime.wait(handle, request)

    def cancel(
        self, handle: ProcessHandle, grace_period_seconds: float
    ) -> CancellationState:
        return self._runtime.cancel(handle, grace_period_seconds)

    def terminate(
        self, handle: ProcessHandle, grace_period_seconds: float
    ) -> tuple[CancellationState, TerminationOutcome, dict[str, Any]]:
        return self._runtime.terminate(handle, grace_period_seconds)

    # -- 결과 해석 -------------------------------------------------------

    def interpret(self, result: ExecutorResult, request: ExecutorRequest) -> ClaudeOutcome:
        """stdout log에서 CLI 결과 JSON을 읽어 요약합니다.

        log artifact는 이미 redaction을 거쳤습니다. 여기서 다시 읽어도 raw
        secret이 나오지 않습니다.
        """

        if result.stdout is None:
            return ClaudeOutcome(parsed=False, failure=ClaudeFailure.OUTPUT_UNPARSEABLE)
        # JSON 한 덩어리라 뒤에서 조금만 읽으면 잘립니다. 상한까지 읽습니다.
        text = read_log_tail(result.stdout.path, max_bytes=request.max_output_bytes)
        return parse_cli_output(text, secrets=request.secret_values)
