"""Process identity.

docs/specs/execution-runtime.md의 Restart and Recovery는 "기록된 PID의
identity, start time, Run marker를 확인해 PID 재사용을 구분한다"를 요구합니다.

PID만으로는 부족합니다. process가 죽고 OS가 같은 번호를 재사용하면 전혀 다른
process를 우리 것으로 오인해 죽일 수 있습니다. 그래서 PID와 함께 process 시작
시각을 identity evidence로 저장하고 확인합니다.

시작 시각을 얻는 방법은 플랫폼마다 다르고 어디서나 가능하지도 않습니다. 그래서
확인 결과를 "일치 / 불일치 / process 없음 / **확인 불가**"로 명시적으로
모델링합니다. 확인 불가를 일치로 취급하지 않습니다.

runtime dependency를 늘리지 않으려고 표준 라이브러리만 씁니다.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any

# 시작 시각을 읽는 방법.
METHOD_PROC_STAT = "linux_proc_stat"
METHOD_WIN32 = "win32_get_process_times"
METHOD_PS = "posix_ps_lstart"
METHOD_UNAVAILABLE = "unavailable"

_PS_TIMEOUT_SECONDS = 5.0


class IdentityVerdict(str, Enum):
    """저장된 identity와 현재 process를 비교한 결과."""

    MATCH = "match"
    MISMATCH = "mismatch"
    PROCESS_ABSENT = "process_absent"
    # 시작 시각을 얻을 수 없어 같은 process인지 증명할 수 없습니다.
    # 절대 kill 근거로 쓰지 않습니다.
    UNVERIFIABLE = "unverifiable"

    @property
    def may_terminate(self) -> bool:
        """이 판정만으로 process를 종료해도 되는지."""

        return self is IdentityVerdict.MATCH


@dataclass(frozen=True)
class ProcessIdentity:
    """process를 PID 재사용과 구분하기 위한 증거."""

    pid: int
    method: str
    start_token: str | None
    captured_at: str

    @property
    def verifiable(self) -> bool:
        return self.method != METHOD_UNAVAILABLE and bool(self.start_token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "method": self.method,
            "start_token": self.start_token,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProcessIdentity":
        return cls(
            pid=int(payload["pid"]),
            method=payload.get("method", METHOD_UNAVAILABLE),
            start_token=payload.get("start_token"),
            captured_at=payload.get("captured_at", ""),
        )


def process_exists(pid: int) -> bool:
    """PID가 살아 있는지 확인합니다. 존재가 곧 동일 process는 아닙니다.

    종료했지만 아직 정리되지 않은 process(Windows의 signaled handle, POSIX의
    zombie)를 살아 있다고 보면 안 됩니다. 그러면 이미 죽은 Run을 healthy로
    오판합니다.
    """

    if pid <= 0:
        return False
    if sys.platform == "win32":
        info = _win32_process_info(pid)
        return info is not None and not info[1]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 다른 사용자의 process입니다. 존재하지만 우리 것이 아닐 수 있습니다.
        return True
    except OSError:
        return False
    if sys.platform.startswith("linux") and _linux_is_zombie(pid):
        return False
    return True


def start_token(pid: int) -> tuple[str, str | None]:
    """`(method, token)`을 돌려줍니다. 얻지 못하면 method는 `unavailable`입니다."""

    if pid <= 0:
        return METHOD_UNAVAILABLE, None
    if sys.platform == "win32":
        token = _win32_start_token(pid)
        return (METHOD_WIN32, token) if token else (METHOD_UNAVAILABLE, None)
    if sys.platform.startswith("linux"):
        token = _linux_start_token(pid)
        if token:
            return METHOD_PROC_STAT, token
    token = _ps_start_token(pid)
    return (METHOD_PS, token) if token else (METHOD_UNAVAILABLE, None)


def capture(pid: int, captured_at: str) -> ProcessIdentity:
    method, token = start_token(pid)
    return ProcessIdentity(pid=pid, method=method, start_token=token, captured_at=captured_at)


def verify(stored: ProcessIdentity | None) -> IdentityVerdict:
    """저장된 identity가 현재 살아 있는 같은 process를 가리키는지 확인합니다."""

    if stored is None or stored.pid <= 0:
        return IdentityVerdict.PROCESS_ABSENT
    if not process_exists(stored.pid):
        return IdentityVerdict.PROCESS_ABSENT
    if not stored.verifiable:
        # 저장 시점에 증거를 얻지 못했습니다. 살아 있는 PID가 같은 process라고
        # 단정할 수 없습니다.
        return IdentityVerdict.UNVERIFIABLE

    method, token = start_token(stored.pid)
    if method == METHOD_UNAVAILABLE or token is None:
        return IdentityVerdict.UNVERIFIABLE
    if method != stored.method:
        # 방법이 달라지면 값을 직접 비교할 수 없습니다.
        return IdentityVerdict.UNVERIFIABLE
    return IdentityVerdict.MATCH if token == stored.start_token else IdentityVerdict.MISMATCH


# -- 플랫폼별 구현 -------------------------------------------------------


def _linux_stat_fields(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except (OSError, ValueError):
        return None
    # comm 필드에 공백이 들어갈 수 있으므로 마지막 ')' 뒤부터 자릅니다.
    close = content.rfind(")")
    if close < 0:
        return None
    # 반환 목록의 index 0이 state(stat의 3번째 필드)입니다.
    return content[close + 2 :].split()


def _linux_start_token(pid: int) -> str | None:
    """`/proc/<pid>/stat`의 22번째 필드(starttime, clock tick)."""

    fields = _linux_stat_fields(pid)
    if fields is None or len(fields) <= 19:
        return None
    return fields[19]


def _linux_is_zombie(pid: int) -> bool:
    fields = _linux_stat_fields(pid)
    return bool(fields) and fields[0] == "Z"


def _ps_start_token(pid: int) -> str | None:
    """`ps`로 시작 시각을 읽습니다. macOS 등 `/proc`이 없는 POSIX용입니다."""

    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (completed.stdout or "").strip()
    return value or None


def _win32_handles():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    return ctypes, wintypes, kernel32


# PROCESS_QUERY_LIMITED_INFORMATION. 종료 코드나 메모리를 읽지 않고 identity만
# 확인하므로 가장 좁은 권한을 씁니다.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _win32_process_info(pid: int) -> tuple[str, bool] | None:
    """`(creation token, exited)`를 돌려줍니다.

    `OpenProcess`는 이미 종료한 process의 handle에도 성공합니다. 따라서 handle이
    열린다고 살아 있다고 볼 수 없습니다. `GetProcessTimes`의 exit time이 0이
    아니면 종료한 process입니다.
    """

    try:
        ctypes, wintypes, kernel32 = _win32_handles()
    except (ImportError, OSError, AttributeError):
        return None

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        exited = bool(exit_time.dwHighDateTime or exit_time.dwLowDateTime)
        return f"{creation.dwHighDateTime}:{creation.dwLowDateTime}", exited
    finally:
        kernel32.CloseHandle(handle)


def _win32_start_token(pid: int) -> str | None:
    info = _win32_process_info(pid)
    return info[0] if info else None
