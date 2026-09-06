"""Local subprocess executor adapter.

`ExecutorAdapter`를 OS process로 구현합니다. provider-neutral이며 어떤 argv든
실행할 수 있습니다. mock executor도 실제 executor도 같은 경로를 씁니다.

docs/security-governance.md 제약을 지킵니다.

- `shell=True`를 쓰지 않고 argv list로만 실행합니다.
- 환경을 상속하지 않고 allowlist로 구성합니다.
- 모든 실행에 timeout이 있습니다.
- stdout/stderr를 메모리에 무한정 쌓지 않고 크기를 제한해 파일로 흘립니다.
- 종료 시 child process까지 정리합니다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .executor import (
    CancellationState,
    ExecutionStatus,
    ExecutorError,
    ExecutorFailure,
    ExecutorRequest,
    ExecutorResult,
    OutputCapture,
    ProcessHandle,
)
from .process_identity import IdentityVerdict, ProcessIdentity, capture, verify

# 상속을 허용하는 OS 기본 환경변수. 이 목록 밖의 값은 child에 전달하지 않습니다.
# Python 인터프리터와 DLL/라이브러리 탐색에 필요한 최소 집합입니다.
POSIX_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR")
WINDOWS_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "PATHEXT",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
)

_TASKKILL_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.05


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_log_tail(path: str | Path, max_bytes: int = 4096) -> str:
    """log 파일의 끝부분을 안전하게 읽습니다.

    executor 출력에는 유효하지 않은 UTF-8이 섞일 수 있으므로 디코딩 오류로
    죽지 않게 `errors="replace"`를 씁니다. redaction은 호출자가 적용합니다.
    """

    target = Path(path)
    try:
        size = target.stat().st_size
        with open(target, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            raw = handle.read(max_bytes)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def base_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """allowlist에 있는 OS 기본 변수만 남깁니다."""

    source = environ if environ is not None else dict(os.environ)
    names = WINDOWS_ENV_ALLOWLIST if sys.platform == "win32" else POSIX_ENV_ALLOWLIST
    return {name: source[name] for name in names if name in source and source[name]}


@dataclass
class _Capture:
    path: Path
    limit: int
    written: int = 0
    truncated: bool = False


def _pump(stream, capture: _Capture) -> None:
    """pipe를 파일로 흘립니다.

    한도를 넘으면 더 쓰지 않지만 읽기는 계속합니다. 읽기를 멈추면 pipe가 차서
    child가 블록되기 때문입니다. 메모리에 전체를 쌓지 않습니다.
    """

    try:
        with open(capture.path, "wb") as sink:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                if capture.written < capture.limit:
                    room = capture.limit - capture.written
                    sink.write(chunk[:room])
                    capture.written += min(len(chunk), room)
                    if len(chunk) > room:
                        capture.truncated = True
                else:
                    capture.truncated = True
                sink.flush()
    except (OSError, ValueError):
        # stream이 닫혔거나 디스크 오류입니다. 수집만 중단합니다.
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


class LocalProcessExecutor:
    """OS process로 실행하는 provider-neutral adapter."""

    def __init__(self, name: str = "mock_local", provider: str = "local") -> None:
        self._name = name
        self._provider = provider
        self._processes: dict[int, subprocess.Popen] = {}
        self._captures: dict[int, tuple[_Capture, _Capture, list[threading.Thread]]] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def provider(self) -> str:
        return self._provider

    # -- lifecycle -------------------------------------------------------

    def spawn(self, request: ExecutorRequest, log_dir: Path) -> ProcessHandle:
        cwd = Path(request.cwd)
        if not cwd.is_dir():
            raise ExecutorError("cwd_missing", f"실행 디렉터리가 없습니다: {cwd}")
        if not request.argv:
            raise ExecutorError("empty_argv", "실행할 command가 없습니다.")

        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_capture = _Capture(log_dir / "stdout.log", request.max_output_bytes)
        stderr_capture = _Capture(log_dir / "stderr.log", request.max_output_bytes)

        environment = base_environment()
        environment.update(request.environment)

        popen_kwargs: dict[str, object] = {
            "cwd": str(cwd),
            "env": environment,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            # shell을 쓰지 않습니다. argv list로만 실행합니다.
            "shell": False,
            "close_fds": True,
        }
        group_id: int | None = None
        if sys.platform == "win32":
            # child까지 함께 다루기 위해 새 process group을 만듭니다.
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # 새 session을 만들어 process group 단위로 종료할 수 있게 합니다.
            popen_kwargs["start_new_session"] = True

        try:
            process = subprocess.Popen(list(request.argv), **popen_kwargs)
        except (OSError, ValueError) as error:
            raise ExecutorError("spawn_failed", f"process를 시작하지 못했습니다: {type(error).__name__}") from None

        if sys.platform != "win32":
            group_id = process.pid

        threads = [
            threading.Thread(target=_pump, args=(process.stdout, stdout_capture), daemon=True),
            threading.Thread(target=_pump, args=(process.stderr, stderr_capture), daemon=True),
        ]
        for thread in threads:
            thread.start()

        started_at = _utcnow_iso()
        identity = capture(process.pid, started_at)
        self._processes[process.pid] = process
        self._captures[process.pid] = (stdout_capture, stderr_capture, threads)

        return ProcessHandle(
            pid=process.pid,
            identity=identity,
            started_at=started_at,
            process_group_id=group_id,
        )

    def wait(self, handle: ProcessHandle, request: ExecutorRequest) -> ExecutorResult:
        process = self._processes.get(handle.pid)
        if process is None:
            raise ExecutorError("process_not_tracked", f"이 adapter가 시작한 process가 아닙니다: {handle.pid}")

        timed_out = False
        try:
            process.wait(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                self.cancel(handle, request.grace_period_seconds)
            except ExecutorError:
                # identity를 확인하지 못하면 종료하지 않습니다. 남은 process는
                # reconciliation이 판단합니다.
                pass
            try:
                process.wait(timeout=request.grace_period_seconds + 5.0)
            except subprocess.TimeoutExpired:
                pass

        stdout, stderr = self._finish_capture(handle.pid)
        finished_at = _utcnow_iso()
        exit_code = process.returncode

        if timed_out:
            return ExecutorResult(
                run_id=request.run_id,
                executor_name=self._name,
                status=ExecutionStatus.FINISHED,
                exit_code=exit_code,
                started_at=handle.started_at,
                finished_at=finished_at,
                stdout=stdout,
                stderr=stderr,
                failure=ExecutorFailure.TIMEOUT,
                detail=f"{request.timeout_seconds}초 안에 끝나지 않아 종료했습니다.",
                cancellation_state=CancellationState.FORCED,
            )

        failure = None if exit_code == 0 else ExecutorFailure.NONZERO_EXIT
        return ExecutorResult(
            run_id=request.run_id,
            executor_name=self._name,
            status=ExecutionStatus.FINISHED,
            exit_code=exit_code,
            started_at=handle.started_at,
            finished_at=finished_at,
            stdout=stdout,
            stderr=stderr,
            failure=failure,
            detail="" if failure is None else f"exit code {exit_code}",
        )

    def cancel(self, handle: ProcessHandle, grace_period_seconds: float = 5.0) -> CancellationState:
        """graceful 종료 후 grace period가 지나면 process tree를 강제 종료합니다.

        **identity가 일치할 때만 종료합니다.** PID만 보고 종료하면 PID를 재사용한
        다른 process를 죽일 수 있습니다.

        parent가 먼저 죽으면 Windows에서는 child를 tree로 추적할 수 없습니다.
        그래서 graceful 단계는 parent를 즉시 죽이지 않고 process group 전체에
        신호를 보냅니다. 강제 단계는 parent가 살아 있는 동안 tree를 정리합니다.
        """

        verdict = verify(handle.identity)
        if verdict is IdentityVerdict.PROCESS_ABSENT:
            # parent는 이미 없지만 child가 남아 있을 수 있습니다.
            self._terminate_tree(handle, force=True)
            return CancellationState.COMPLETED
        if not verdict.may_terminate:
            raise ExecutorError(
                "identity_unverified",
                f"process identity를 확인하지 못해 종료하지 않습니다: {verdict.value}",
            )

        self._terminate_tree(handle, force=False)
        deadline = time.monotonic() + max(0.0, grace_period_seconds)
        graceful = False
        while time.monotonic() < deadline:
            if verify(handle.identity) is IdentityVerdict.PROCESS_ABSENT:
                graceful = True
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        # graceful로 끝났더라도 tree 정리를 한 번 더 수행합니다. parent가 죽어도
        # child가 남을 수 있고, 남은 child는 Run 경계를 벗어난 side effect입니다.
        self._terminate_tree(handle, force=True)
        return CancellationState.COMPLETED if graceful else CancellationState.FORCED

    # -- 내부 -----------------------------------------------------------

    def _finish_capture(self, pid: int) -> tuple[OutputCapture | None, OutputCapture | None]:
        entry = self._captures.pop(pid, None)
        self._processes.pop(pid, None)
        if entry is None:
            return None, None
        stdout_capture, stderr_capture, threads = entry
        for thread in threads:
            thread.join(timeout=10.0)
        return (
            OutputCapture(
                str(stdout_capture.path), stdout_capture.written, stdout_capture.truncated
            ),
            OutputCapture(
                str(stderr_capture.path), stderr_capture.written, stderr_capture.truncated
            ),
        )

    def _terminate_tree(self, handle: ProcessHandle, force: bool) -> None:
        """process와 그 child까지 종료합니다.

        POSIX는 새 session을 만들었으므로 process group에 신호를 보냅니다.
        Windows는 process group에 CTRL_BREAK을 보내고, 강제 단계에서는
        `taskkill /T`로 tree를 정리합니다.
        """

        if sys.platform == "win32":
            self._terminate_tree_windows(handle, force)
            return

        import signal

        group = handle.process_group_id or handle.pid
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(group, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.kill(handle.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _terminate_tree_windows(self, handle: ProcessHandle, force: bool) -> None:
        """Windows에서 Run 단위 process tree를 종료합니다.

        graceful 단계는 `CTRL_BREAK_EVENT`를 process group에 보냅니다. spawn할 때
        `CREATE_NEW_PROCESS_GROUP`을 줬으므로 같은 group의 child도 함께 받습니다.
        parent를 바로 `TerminateProcess`하면 child가 고아가 되고 `taskkill /T`가
        tree를 추적하지 못합니다.

        강제 단계는 `taskkill /F /T`로 tree를 정리합니다. parent가 아직 살아 있을
        때 호출해야 child까지 닿습니다.
        """

        process = self._processes.get(handle.pid)
        if not force:
            import signal

            try:
                os.kill(handle.pid, signal.CTRL_BREAK_EVENT)
                return
            except (OSError, AttributeError, ValueError):
                pass
            try:
                if process is not None:
                    process.terminate()
            except OSError:
                pass
            return

        # `taskkill /T`가 child까지 정리합니다. shell 없이 argv로 호출합니다.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(handle.pid)],
                capture_output=True,
                timeout=_TASKKILL_TIMEOUT_SECONDS,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            if process is not None:
                process.kill()
        except OSError:
            pass


def terminate_foreign(identity: ProcessIdentity, force: bool = True) -> bool:
    """다른 process(예: 재시작 전에 남은 executor)를 종료합니다.

    identity가 일치할 때만 종료합니다. 일치하지 않으면 아무것도 하지 않고
    `False`를 돌려줍니다. 이 함수가 "증명하지 못한 process는 죽이지 않는다"
    규칙의 유일한 관문입니다.
    """

    if verify(identity) is not IdentityVerdict.MATCH:
        return False
    handle = ProcessHandle(pid=identity.pid, identity=identity, started_at=identity.captured_at)
    executor = LocalProcessExecutor()
    executor._terminate_tree(handle, force=force)
    return True
