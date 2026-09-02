"""Atlas worker CLI.

    python -m atlas 12                  # 단건 Issue 검증 (기존 동작)
    python -m atlas show 12
    python -m atlas poll                # 한 번 polling
    python -m atlas poll --watch        # interval 간격 반복
    python -m atlas claim               # Task 하나 claim
    python -m atlas release <claim-id>
    python -m atlas tasks

Exit code: 0 성공, 1 validation 실패 또는 claim 대상 없음, 2 source 오류.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from dataclasses import replace
from typing import Any

from .config import WorkerConfig
from .intake import DEFAULT_REPOSITORY, IssueIntake
from .issue_source import GitHubRestIssueSource, IssueSourceError
from .polling import IssuePoller
from .store import TaskStore

_ISSUE_NUMBER = re.compile(r"^\d+$")
COMMANDS = ("show", "poll", "claim", "release", "tasks")


def build_parser() -> argparse.ArgumentParser:
    # 공통 flag는 subcommand 앞뒤 어디에 와도 받도록 parent로 공유합니다.
    # default를 SUPPRESS로 두지 않으면 subparser의 기본값이 상위 parser가 이미
    # 읽은 값을 덮어씁니다. 값 조회는 `_option()`을 사용합니다.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--database", default=argparse.SUPPRESS, help="SQLite 파일 경로 (기본 ATLAS_DB_PATH)"
    )
    common.add_argument("--repository", default=argparse.SUPPRESS, help="owner/name")
    common.add_argument(
        "--indent", type=int, default=argparse.SUPPRESS, help="JSON 들여쓰기"
    )

    parser = argparse.ArgumentParser(
        prog="atlas", description="Atlas Task intake worker.", parents=[common]
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser(
        "show", parents=[common], help="Issue 한 건을 parse·검증만 합니다 (저장 없음)"
    )
    show.add_argument("issue_number", type=int)

    poll = subparsers.add_parser(
        "poll", parents=[common], help="후보 Issue를 polling해 valid Task를 저장합니다"
    )
    poll.add_argument("--watch", action="store_true", help="interval 간격으로 반복 실행")
    poll.add_argument("--interval", type=float, default=None, help="polling 간격(초)")
    poll.add_argument("--iterations", type=int, default=None, help="--watch의 최대 반복 횟수")
    poll.add_argument("--require-queue-label", action="store_true", help="queue label 필수화")

    claim = subparsers.add_parser(
        "claim", parents=[common], help="Task 하나를 원자적으로 claim합니다"
    )
    claim.add_argument("--worker-id", default=None, help="기본값은 host 기반 식별자")
    claim.add_argument("--lease-ttl", type=float, default=None, help="lease TTL(초)")
    claim.add_argument("--task-id", default=None, help="특정 Task만 claim")

    release = subparsers.add_parser("release", parents=[common], help="claim을 해제합니다")
    release.add_argument("claim_id")
    release.add_argument("--reason", default="manual_release")

    subparsers.add_parser("tasks", parents=[common], help="저장된 current Task를 나열합니다")

    return parser


def _normalize(argv: list[str]) -> list[str]:
    """`python -m atlas 12`를 `show 12`로 해석해 기존 동작을 유지합니다.

    subcommand가 이미 있으면 손대지 않습니다. flag의 값이 숫자인 경우
    (`--interval 5`)를 오인하지 않으려고 subcommand 유무를 먼저 확인합니다.
    """

    if any(token in COMMANDS for token in argv):
        return argv
    for index, token in enumerate(argv):
        if _ISSUE_NUMBER.match(token):
            return argv[:index] + ["show"] + argv[index:]
    return argv


def _option(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    """SUPPRESS 기본값을 쓰는 공통 flag를 읽습니다."""

    return getattr(args, name, default)


def _emit(payload: dict[str, Any], indent: int) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=indent, default=str)
    sys.stdout.write("\n")


def _config(args: argparse.Namespace) -> WorkerConfig:
    config = WorkerConfig.from_env()
    if database := _option(args, "database"):
        config = replace(config, database_path=database)
    polling = config.polling
    if repository := _option(args, "repository"):
        polling = replace(polling, repository=repository)
    if interval := _option(args, "interval"):
        polling = replace(polling, interval_seconds=interval)
    if _option(args, "require_queue_label", False):
        polling = replace(polling, require_queue_label=True)
    claim = config.claim
    if lease_ttl := _option(args, "lease_ttl"):
        claim = replace(claim, lease_ttl_seconds=lease_ttl)
    return replace(config, polling=polling, claim=claim)


def _default_worker_id() -> str:
    return f"worker-{socket.gethostname()}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(_normalize(list(argv) if argv is not None else sys.argv[1:]))
    config = _config(args)

    try:
        if args.command == "show":
            return _run_show(args, config)
        if args.command == "poll":
            return _run_poll(args, config)
        if args.command == "claim":
            return _run_claim(args, config)
        if args.command == "release":
            return _run_release(args, config)
        if args.command == "tasks":
            return _run_tasks(args, config)
    except IssueSourceError as error:
        _emit(
            {"status": "SourceError", "category": error.category, "message": error.message},
            _option(args, "indent", 2),
        )
        return 2
    return 2


def _run_show(args: argparse.Namespace, config: WorkerConfig) -> int:
    repository = _option(args, "repository") or DEFAULT_REPOSITORY
    result = IssueIntake(GitHubRestIssueSource()).intake(args.issue_number, repository)
    _emit(result.to_dict(), _option(args, "indent", 2))
    return 0 if result.is_valid else 1


def _run_poll(args: argparse.Namespace, config: WorkerConfig) -> int:
    source = GitHubRestIssueSource()
    with TaskStore(config.database_path) as store:
        poller = IssuePoller(source, IssueIntake(source), store, config.polling)
        if args.watch:
            reports = poller.run(max_iterations=args.iterations)
            _emit({"status": "Polled", "passes": [r.to_dict() for r in reports]}, _option(args, "indent", 2))
            return 0 if all(r.error is None for r in reports) else 2
        report = poller.poll_once()
        _emit({"status": "Polled", **report.to_dict()}, _option(args, "indent", 2))
        return 0 if report.error is None else 2


def _run_claim(args: argparse.Namespace, config: WorkerConfig) -> int:
    worker_id = args.worker_id or _default_worker_id()
    with TaskStore(config.database_path) as store:
        claim = store.claim(
            worker_id,
            config.claim.lease_ttl_seconds,
            task_id=args.task_id,
            grace_period_seconds=config.claim.grace_period_seconds,
        )
        if claim is None:
            _emit({"status": "NoClaimableTask", "worker_id": worker_id}, _option(args, "indent", 2))
            return 1
        _emit({"status": "Claimed", **claim.to_dict()}, _option(args, "indent", 2))
        return 0


def _run_release(args: argparse.Namespace, config: WorkerConfig) -> int:
    with TaskStore(config.database_path) as store:
        released = store.release(args.claim_id, args.reason)
    _emit(
        {
            "status": "Released" if released else "NoActiveClaim",
            "claim_id": args.claim_id,
            "reason": args.reason,
        },
        _option(args, "indent", 2),
    )
    return 0 if released else 1


def _run_tasks(args: argparse.Namespace, config: WorkerConfig) -> int:
    with TaskStore(config.database_path) as store:
        rows = store.current_tasks()
        payload = []
        for row in rows:
            claim = store.active_claim(row["task_id"])
            payload.append(
                {
                    "task_id": row["task_id"],
                    "fingerprint": row["fingerprint"],
                    "issue_number": row["issue_number"],
                    "status": row["status"],
                    "issue_revision": row["issue_revision"],
                    "created_at": row["created_at"],
                    "claim": (
                        {
                            "claim_id": claim["claim_id"],
                            "lease_owner": claim["lease_owner"],
                            "lease_expires_at": claim["lease_expires_at"],
                        }
                        if claim
                        else None
                    ),
                }
            )
    _emit({"status": "Tasks", "count": len(payload), "tasks": payload}, _option(args, "indent", 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
