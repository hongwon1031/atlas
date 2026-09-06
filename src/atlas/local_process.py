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

import codecs
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .executor import (
    StructuredCapture,
    CancellationState,
    ExecutionStatus,
    ExecutorError,
    ExecutorFailure,
    ExecutorRequest,
    ExecutorResult,
    OutputCapture,
    ProcessHandle,
    TerminationOutcome,
)
from .process_identity import IdentityVerdict, ProcessIdentity, capture, verify
from .redaction import (
    overlap_window,
    redact,
    redaction_spans,
    safe_split_index,
)

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

# redaction pattern은 줄 단위입니다. secret에는 개행이 없으므로 완성된 줄만
# 처리하면 chunk 경계에 걸친 token도 안전하게 지울 수 있습니다. 개행 없이
# 계속 쏟아내는 process를 대비해 보류할 수 있는 최대 길이를 둡니다.
MAX_PENDING_CHARS = 65_536

# 경계를 가로지르는 secret 때문에 flush를 미룰 수 있는 한도입니다. 버퍼 전체가
# 하나의 secret 후보인 병적인 출력에서도 메모리가 무한히 늘지 않게 막습니다.
MAX_RETAINED_CHARS = MAX_PENDING_CHARS * 2


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
    secrets: tuple[str, ...] = ()
    written: int = 0
    truncated: bool = False


class _RedactingSink:
    """executor 출력을 redaction하면서 파일로 흘립니다.

    log artifact 자체가 redacted 상태여야 합니다. event만 지우면 secret이 디스크에
    평문으로 남습니다.

    설계상 유의점입니다.

    - **streaming**: 전체 출력을 메모리에 모으지 않습니다. 완성된 줄만 처리하고
      나머지는 보류합니다.
    - **chunk 경계**: secret이 여러 chunk에 나뉘어 도착해도 줄이 완성될 때까지
      기다렸다가 redaction하므로 잘린 채로 기록되지 않습니다.
    - **flush 경계**: 개행 없이 한도에 도달해 강제로 내보낼 때도 secret을 반으로
      자르지 않습니다. 꼬리 일부를 남겨 다음 회차와 함께 다시 검사하고, 완결된
      secret이 경계에 걸치면 그 시작점까지 물러섭니다.
    - **invalid UTF-8**: incremental decoder를 `errors="replace"`로 씁니다.
      multi-byte 문자가 chunk 경계에 걸려도 깨지지 않고, 잘못된 byte는 대체
      문자가 됩니다.
    - **binary 출력**: log는 텍스트로 취급합니다. binary 출력은 대체 문자로
      바뀌므로 log artifact는 byte 단위로 원본과 같지 않습니다. secret이 평문으로
      남지 않게 하는 것이 우선입니다.
    - **크기 제한**: redaction을 마친 byte 기준으로 셉니다.
    """

    def __init__(self, capture: _Capture) -> None:
        self._capture = capture
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._overlap = overlap_window(capture.secrets)
        self._suppressing = False
        self._sink = open(capture.path, "wb")

    def feed(self, chunk: bytes) -> None:
        text = self._decoder.decode(chunk)
        if self._suppressing:
            # 직전에 내보낸 구간이 버퍼 끝까지 이어졌습니다. 같은 secret이
            # 계속되는 중이므로, 구간이 끝나는 줄바꿈까지 버립니다. 여기서
            # 흘려보내면 token의 뒷부분이 raw로 남습니다.
            _, separator, rest = text.partition(chr(10))
            if not separator:
                return
            self._suppressing = False
            text = chr(10) + rest
        self._pending += text
        if "\n" in self._pending:
            head, _, self._pending = self._pending.rpartition("\n")
            self._write(head + "\n")
        elif len(self._pending) >= MAX_PENDING_CHARS:
            self._flush_forced()

    def _flush_forced(self) -> None:
        """개행 없이 계속 출력하는 process를 위한 강제 flush입니다.

        무한정 보류할 수는 없지만, 통째로 내보내면 secret이 경계에 걸쳐 앞
        조각이 raw로 기록됩니다. 꼬리를 남겨 다음 회차로 넘깁니다.
        """

        text = self._pending
        keep = self._overlap
        if len(text) <= keep:
            return

        cut = safe_split_index(text, len(text) - keep, self._capture.secrets)
        if cut <= 0:
            if len(text) < MAX_RETAINED_CHARS:
                # 버퍼 전체가 아직 판정 중인 secret 후보입니다. 조금 더 봅니다.
                return
            # 버퍼 전체가 하나의 구간입니다. redaction하면 통째로 사라지므로
            # 내보내도 raw 값이 남지 않습니다.
            self._write(text)
            self._pending = ""
            # 구간이 버퍼 끝까지 이어졌다면 secret이 아직 안 끝났습니다.
            self._suppressing = any(
                end >= len(text)
                for _, end in redaction_spans(text, self._capture.secrets)
            )
            return

        self._write(text[:cut])
        self._pending = text[cut:]

    def close(self) -> None:
        try:
            self._pending += self._decoder.decode(b"", final=True)
            if self._pending:
                self._write(self._pending)
                self._pending = ""
        finally:
            try:
                self._sink.close()
            except (OSError, ValueError):
                pass

    def _write(self, text: str) -> None:
        if not text:
            return
        data = redact(text, self._capture.secrets).encode("utf-8", errors="replace")
        capture = self._capture
        if capture.written >= capture.limit:
            capture.truncated = True
            return
        room = capture.limit - capture.written
        self._sink.write(data[:room])
        capture.written += min(len(data), room)
        if len(data) > room:
            capture.truncated = True
        self._sink.flush()


def _write_stdin(stream, text: str) -> None:
    """prompt를 stdin으로 흘려보내고 닫습니다.

    child가 입력을 다 읽지 않고 끝날 수 있으므로 broken pipe를 정상 상황으로
    다룹니다. 여기서 예외가 나면 실행 자체가 실패한 것처럼 보입니다.
    """

    try:
        stream.write(text.encode("utf-8"))
        stream.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _pump(stream, capture: _Capture, transient: StructuredCapture | None = None) -> None:
    """pipe를 redaction하며 파일로 흘립니다.

    한도를 넘어도 읽기는 계속합니다. 읽기를 멈추면 pipe가 차서 child가 블록되기
    때문입니다. 메모리에 전체 출력을 쌓지 않습니다.

    `transient`가 있으면 **redaction 이전 원문**을 상한 있는 메모리 버퍼에도
    복사합니다. 구조화된 출력을 파싱하려는 호출자를 위한 것으로, 이 버퍼는
    디스크나 DB에 저장되지 않고 한 번 읽히면 버려집니다. 파일에 쓰는 경로는
    영향을 받지 않습니다. 저장본은 그대로 redaction을 거칩니다.
    """

    sink = None
    try:
        sink = _RedactingSink(capture)
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            if transient is not None:
                transient.feed(chunk)
            sink.feed(chunk)
    except (OSError, ValueError):
        # stream이 닫혔거나 디스크 오류입니다. 수집만 중단합니다.
        pass
    finally:
        if sink is not None:
            try:
                sink.close()
            except (OSError, ValueError):
                pass
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
        stdout_capture = _Capture(
            log_dir / "stdout.log", request.max_output_bytes, request.secret_values
        )
        stderr_capture = _Capture(
            log_dir / "stderr.log", request.max_output_bytes, request.secret_values
        )

        environment = base_environment()
        environment.update(request.environment)

        popen_kwargs: dict[str, object] = {
            "cwd": str(cwd),
            "env": environment,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.PIPE if request.stdin_data else subprocess.DEVNULL,
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

        if request.stdin_data:
            # child가 다 읽기 전에 write가 막힐 수 있으므로 별도 thread에서
            # 흘려보내고 끝나면 닫습니다. 닫아야 child가 입력 끝을 압니다.
            threading.Thread(
                target=_write_stdin, args=(process.stdin, request.stdin_data), daemon=True
            ).start()

        threads = [
            threading.Thread(
            target=_pump,
            args=(process.stdout, stdout_capture, request.structured_capture),
            daemon=True,
        ),
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
        cancel_state = CancellationState.NONE
        cancel_error: str | None = None
        try:
            process.wait(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                cancel_state = self.cancel(handle, request.grace_period_seconds)
            except ExecutorError as error:
                # identity를 확인하지 못하면 종료하지 않습니다. 이 경우 process가
                # 아직 살아 있을 수 있으므로 정상 종료로 확정하면 안 됩니다.
                cancel_error = error.category
                cancel_state = CancellationState.UNCONFIRMED
            try:
                process.wait(timeout=request.grace_period_seconds + 5.0)
            except subprocess.TimeoutExpired:
                pass

        stdout, stderr = self._finish_capture(handle.pid)
        finished_at = _utcnow_iso()
        exit_code = process.returncode

        if timed_out:
            # 요청만으로 끝났다고 보지 않고 실제로 사라졌는지 확인합니다.
            verdict = verify(handle.identity)
            confirmed = verdict is IdentityVerdict.PROCESS_ABSENT
            evidence = {
                "identity_verdict": verdict.value,
                "cancel_error": cancel_error,
                "pid": handle.pid,
            }
            return ExecutorResult(
                run_id=request.run_id,
                executor_name=self._name,
                status=ExecutionStatus.FINISHED if confirmed else ExecutionStatus.CANCELLING,
                exit_code=exit_code,
                started_at=handle.started_at,
                finished_at=finished_at if confirmed else None,
                stdout=stdout,
                stderr=stderr,
                failure=ExecutorFailure.TIMEOUT,
                detail=f"{request.timeout_seconds}초 안에 끝나지 않아 종료했습니다."
                if confirmed
                else f"{request.timeout_seconds}초 timeout 후 종료를 확인하지 못했습니다.",
                cancellation_state=cancel_state
                if confirmed
                else CancellationState.UNCONFIRMED,
                termination=TerminationOutcome.CONFIRMED
                if confirmed
                else TerminationOutcome.UNVERIFIED,
                termination_evidence=evidence,
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

    def terminate(
        self, handle: ProcessHandle, grace_period_seconds: float = 5.0
    ) -> tuple[CancellationState, TerminationOutcome, dict[str, Any]]:
        """종료를 시도하고 **확인 결과까지** 돌려줍니다.

        `cancel()`은 계약 호환을 위해 상태만 돌려주지만, 호출자가 종료 확인 여부를
        알아야 할 때는 이 함수를 씁니다.
        """

        try:
            state = self.cancel(handle, grace_period_seconds)
        except ExecutorError as error:
            return (
                CancellationState.UNCONFIRMED,
                TerminationOutcome.UNVERIFIED,
                {"identity_verdict": verify(handle.identity).value, "cancel_error": error.category},
            )

        verdict = verify(handle.identity)
        if verdict is IdentityVerdict.PROCESS_ABSENT:
            return state, TerminationOutcome.CONFIRMED, {"identity_verdict": verdict.value}
        return (
            CancellationState.UNCONFIRMED,
            TerminationOutcome.UNVERIFIED,
            {"identity_verdict": verdict.value, "cancel_error": None},
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
