"""Issue intake CLI.

    python -m atlas 12
    python -m atlas 12 --repository owner/name

Exit code: 0 valid, 1 validation 실패, 2 source 오류.
"""

from __future__ import annotations

import argparse
import json
import sys

from .intake import DEFAULT_REPOSITORY, IssueIntake
from .issue_source import GitHubRestIssueSource, IssueSourceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="GitHub Issue를 Atlas Task 후보로 parse하고 검증합니다.",
    )
    parser.add_argument("issue_number", type=int, help="Atlas Task Issue 번호")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY, help="owner/name")
    parser.add_argument("--indent", type=int, default=2, help="JSON 들여쓰기")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = IssueIntake(GitHubRestIssueSource()).intake(args.issue_number, args.repository)
    except IssueSourceError as error:
        json.dump(
            {"status": "SourceError", "category": error.category, "message": error.message},
            sys.stdout,
            ensure_ascii=False,
            indent=args.indent,
        )
        sys.stdout.write("\n")
        return 2

    json.dump(result.to_dict(), sys.stdout, ensure_ascii=False, indent=args.indent, default=str)
    sys.stdout.write("\n")
    return 0 if result.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
