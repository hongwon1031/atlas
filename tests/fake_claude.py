"""통합 테스트용 가짜 Claude Code CLI.

실제 CLI를 부르지 않고 adapter 배선을 검증합니다. 실제 CLI로 확인한 계약만
흉내 냅니다.

- `--print`와 `--output-format json`을 받습니다.
- prompt를 **stdin**으로 읽습니다.
- 결과를 stdout에 JSON 한 덩어리로 씁니다.
- `--version`은 `<version> (Claude Code)` 형태로 답합니다.

동작은 환경변수로 지시합니다. argv로 받으면 adapter가 만드는 argv를 그대로
검증할 수 없습니다.

- `FAKE_CLAUDE_MODE`: success | nochange | fail | sleep | badjson | commit | huge
- `FAKE_CLAUDE_RESULT`: 결과 JSON의 `result` 문자열을 이 값으로 바꿉니다.
- `FAKE_CLAUDE_HUGE_BYTES`: huge 모드에서 만들 result 길이.
- `FAKE_CLAUDE_WRITE`: 만들 파일 경로(cwd 기준). `:` 로 여러 개.
- `FAKE_CLAUDE_TEXT`: 파일에 쓸 내용.
- `FAKE_CLAUDE_ECHO`: 지정하면 받은 prompt를 이 파일에 그대로 씁니다.
- `FAKE_CLAUDE_ARGV`: 지정하면 받은 argv를 이 파일에 JSON으로 씁니다.
- `FAKE_CLAUDE_SLEEP`: sleep 모드에서 잠들 초.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _force_utf8_io() -> None:
    """stdin/stdout을 UTF-8로 고정합니다.

    Windows 기본 인코딩(cp949)으로 읽으면 UTF-8 prompt가 깨집니다. 실제 CLI는
    UTF-8을 읽으므로 가짜도 같은 조건으로 맞춥니다.
    """

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _emit(payload: dict, exit_code: int) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()
    return exit_code


def main() -> int:
    _force_utf8_io()
    argv = sys.argv[1:]
    if "--version" in argv:
        sys.stdout.write("9.9.9 (Claude Code)\n")
        return 0

    if record := os.environ.get("FAKE_CLAUDE_ARGV"):
        Path(record).write_text(json.dumps(argv, ensure_ascii=False), encoding="utf-8")

    prompt = sys.stdin.read() if not sys.stdin.closed else ""
    if echo := os.environ.get("FAKE_CLAUDE_ECHO"):
        Path(echo).write_text(prompt, encoding="utf-8")

    mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
    cwd = Path.cwd()

    if mode == "sleep":
        time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "60")))
        return _emit({"type": "result", "is_error": False, "result": "slept"}, 0)

    if mode == "huge":
        size = int(os.environ.get("FAKE_CLAUDE_HUGE_BYTES", "500000"))
        return _emit({"type": "result", "is_error": False, "result": "x" * size}, 0)

    if mode == "badjson":
        sys.stdout.write("이건 JSON이 아닙니다")
        return 0

    if mode == "fail":
        sys.stderr.write("fake claude failed\n")
        return _emit(
            {
                "type": "result",
                "is_error": True,
                "subtype": "success",
                "result": "something went wrong",
                "terminal_reason": "api_error",
            },
            1,
        )

    if mode == "nochange":
        return _emit(
            {"type": "result", "is_error": False, "subtype": "success", "result": "변경 없음"},
            0,
        )

    targets = [t for t in (os.environ.get("FAKE_CLAUDE_WRITE") or "").split(":") if t]
    text = os.environ.get("FAKE_CLAUDE_TEXT", "fake claude edit")
    for target in targets:
        path = cwd / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")

    if mode == "commit":
        # 실제 CLI에는 Bash를 주지 않지만, 그래도 commit이 생기면 탐지되는지
        # 확인하기 위한 모드입니다.
        subprocess.run(["git", "add", "-A"], cwd=str(cwd), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=f@e.com", "-c", "user.name=F", "commit", "-m", "fake"],
            cwd=str(cwd),
            capture_output=True,
        )

    return _emit(
        {
            "type": "result",
            "is_error": False,
            "subtype": "success",
            "num_turns": 2,
            "session_id": "fake-session",
            "permission_denials": [],
            "result": os.environ.get("FAKE_CLAUDE_RESULT")
            or f"{len(targets)}개 파일을 수정했습니다.",
        },
        0,
    )


if __name__ == "__main__":
    raise SystemExit(main())
