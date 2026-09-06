"""테스트와 개발용 mock executor.

실제 Claude Code나 Codex를 붙이기 전에 process lifecycle을 검증하기 위한
결정적인 프로그램입니다. 별도 OS process로 실행됩니다.

    python -m atlas.mock_executor --mode success --write-file out.txt

production executor가 아닙니다. adapter 계약을 시험하는 용도입니다.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

MODES = ("success", "fail", "sleep", "child", "output", "binary")


def _force_utf8_output() -> None:
    """플랫폼 기본 인코딩과 무관하게 결정적인 출력을 만듭니다."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas.mock_executor", description="Atlas executor runtime 검증용 mock."
    )
    parser.add_argument("--mode", choices=MODES, default="success")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--write-file", default=None, help="cwd 기준 상대 경로")
    parser.add_argument("--write-content", default="mock executor 실행 결과\n")
    parser.add_argument("--stdout-text", default="")
    parser.add_argument("--stderr-text", default="")
    parser.add_argument("--stdout-bytes", type=int, default=0, help="지정 크기만큼 출력")
    parser.add_argument("--child-sleep-seconds", type=float, default=60.0)
    parser.add_argument("--child-marker", default=None, help="child가 만들 파일")
    return parser


def _emit(args: argparse.Namespace) -> None:
    if args.stdout_text:
        sys.stdout.write(args.stdout_text + "\n")
    if args.stderr_text:
        sys.stderr.write(args.stderr_text + "\n")
    sys.stdout.flush()
    sys.stderr.flush()


def _spawn_child(args: argparse.Namespace) -> int:
    """오래 사는 child를 만듭니다. process tree 종료 검증용입니다."""

    marker = args.child_marker or "child-alive.txt"
    script = (
        "import pathlib, sys, time\n"
        "marker = pathlib.Path(sys.argv[1])\n"
        "marker.write_text(str(__import__('os').getpid()), encoding='utf-8')\n"
        "time.sleep(float(sys.argv[2]))\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, marker, str(args.child_sleep_seconds)],
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sys.stdout.write(f"child_pid={child.pid}\n")
    sys.stdout.flush()
    # child가 marker를 쓸 시간을 줍니다.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not Path(marker).exists():
        time.sleep(0.05)
    return child.pid


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)

    if args.write_file:
        target = Path(args.write_file)
        if not target.is_absolute():
            target = Path.cwd() / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.write_content, encoding="utf-8")

    if args.mode == "success":
        _emit(args)
        return 0

    if args.mode == "fail":
        _emit(args)
        sys.stderr.write("mock executor가 실패로 종료합니다.\n")
        sys.stderr.flush()
        return args.exit_code or 1

    if args.mode == "output":
        _emit(args)
        if args.stdout_bytes > 0:
            # 결정적인 내용으로 지정 크기를 채웁니다.
            chunk = "0123456789abcdef" * 64
            written = 0
            while written < args.stdout_bytes:
                piece = chunk[: min(len(chunk), args.stdout_bytes - written)]
                sys.stdout.write(piece)
                written += len(piece)
            sys.stdout.write("\n")
            sys.stdout.flush()
        return args.exit_code

    if args.mode == "binary":
        # 유효하지 않은 UTF-8을 그대로 내보냅니다.
        sys.stdout.flush()
        with os.fdopen(os.dup(sys.stdout.fileno()), "wb", closefd=True) as raw:
            raw.write(b"\xff\xfe\x00invalid utf-8 \xc3\x28 end\n")
            raw.flush()
        return args.exit_code

    if args.mode == "sleep":
        _emit(args)
        time.sleep(args.sleep_seconds)
        return args.exit_code

    if args.mode == "child":
        _spawn_child(args)
        _emit(args)
        time.sleep(args.sleep_seconds or args.child_sleep_seconds)
        return args.exit_code

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
