"""Atlas worker CLI.

    python -m atlas 12                  # 단건 Issue 검증 (기존 동작)
    python -m atlas show 12
    python -m atlas poll                # 한 번 polling
    python -m atlas poll --watch        # interval 간격 반복
    python -m atlas claim               # Task 하나 claim
    python -m atlas release <claim-id>
    python -m atlas tasks
    python -m atlas runs                # Run 목록
    python -m atlas run-start <task-id> # claim된 Task에 Run 생성
    python -m atlas run-heartbeat <run-id>
    python -m atlas run-finish <run-id> --status Succeeded
    python -m atlas reconcile           # stale Run + workspace 정합성 확인
    python -m atlas workspace-create --run-id <run-id>
    python -m atlas workspace-show --run-id <run-id>
    python -m atlas workspace-cleanup --run-id <run-id>
    python -m atlas executor-start --run-id <run-id> --mock-mode success
    python -m atlas executor-show --run-id <run-id>
    python -m atlas executor-cancel --run-id <run-id>

Exit code: 0 성공, 1 대상 없음 또는 lifecycle 위반, 2 source 오류.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from pathlib import Path
from dataclasses import replace
from typing import Any

from .config import WorkerConfig
from .intake import DEFAULT_REPOSITORY, IssueIntake
from .issue_source import GitHubRestIssueSource, IssueSourceError
from .polling import IssuePoller
from .execution_service import DEFAULT_MAX_OUTPUT_BYTES, ExecutionService
from .executor import ExecutorError
from .local_process import LocalProcessExecutor
from .reconciliation import RunReconciler
from .schema import RunFailure, RunStatus
from .store import RunError, TaskStore
from .workspace import WorkspaceError, WorkspacePlanner
from .workspace_service import WorkspaceService

_ISSUE_NUMBER = re.compile(r"^\d+$")
COMMANDS = (
    "show",
    "poll",
    "claim",
    "release",
    "tasks",
    "runs",
    "run-start",
    "run-heartbeat",
    "run-finish",
    "reconcile",
    "workspace-create",
    "workspace-show",
    "workspace-cleanup",
    "executor-start",
    "executor-show",
    "executor-cancel",
)

# mock executor 실행 모드. 개발과 테스트 전용이며 public UX가 아닙니다.
MOCK_MODES = ("success", "fail", "sleep", "child", "output", "binary")

# finish에서 사람이 지정할 수 있는 terminal 상태. Orphaned는 reconciliation이
# 판단 근거와 함께 기록하는 상태이므로 CLI로 직접 지정하지 않습니다.
FINISH_STATUSES = ("Succeeded", "Failed", "Cancelled")


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"숫자가 아닙니다: {raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"0보다 커야 합니다: {raw!r}")
    return value


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"정수가 아닙니다: {raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"0보다 커야 합니다: {raw!r}")
    return value


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
    show.add_argument("issue_number", type=_positive_int)

    poll = subparsers.add_parser(
        "poll", parents=[common], help="후보 Issue를 polling해 valid Task를 저장합니다"
    )
    poll.add_argument("--watch", action="store_true", help="interval 간격으로 반복 실행")
    poll.add_argument("--interval", type=_positive_float, default=None, help="polling 간격(초)")
    poll.add_argument("--iterations", type=_positive_int, default=None, help="--watch의 최대 반복 횟수")
    poll.add_argument(
        "--no-queue-label",
        action="store_true",
        help="approval gate를 끕니다. queue label 없는 후보도 등록되므로 신뢰된 repository에서만 사용하세요",
    )

    claim = subparsers.add_parser(
        "claim", parents=[common], help="Task 하나를 원자적으로 claim합니다"
    )
    claim.add_argument("--worker-id", default=None, help="기본값은 host 기반 식별자")
    claim.add_argument("--lease-ttl", type=_positive_float, default=None, help="lease TTL(초)")
    claim.add_argument("--task-id", default=None, help="특정 Task만 claim")

    release = subparsers.add_parser("release", parents=[common], help="claim을 해제합니다")
    release.add_argument("claim_id")
    release.add_argument("--reason", default="manual_release")

    subparsers.add_parser("tasks", parents=[common], help="저장된 current Task를 나열합니다")

    runs = subparsers.add_parser("runs", parents=[common], help="Run을 나열합니다")
    runs.add_argument("--task-id", default=None, help="특정 Task의 Run만")
    runs.add_argument("--limit", type=_positive_int, default=50)

    run_start = subparsers.add_parser(
        "run-start", parents=[common], help="claim된 approved Task에 Run을 만듭니다"
    )
    run_start.add_argument("task_id")
    run_start.add_argument("--worker-id", default=None, help="기본값은 host 기반 식별자")
    run_start.add_argument("--previous-run-id", default=None, help="retry일 때 이전 Run")

    heartbeat = subparsers.add_parser(
        "run-heartbeat", parents=[common], help="Run이 살아 있음을 기록합니다"
    )
    heartbeat.add_argument("run_id")
    heartbeat.add_argument("--worker-id", default=None)

    finish = subparsers.add_parser(
        "run-finish", parents=[common], help="Run을 terminal 상태로 전이합니다"
    )
    finish.add_argument("run_id")
    finish.add_argument("--status", choices=FINISH_STATUSES, required=True)
    finish.add_argument("--worker-id", default=None, help="지정하면 owner 일치를 확인")
    finish.add_argument("--failure-category", default=None, help="Failed/Cancelled의 분류")
    finish.add_argument("--failure-message", default="", help="redaction을 마친 설명")

    reconcile = subparsers.add_parser(
        "reconcile", parents=[common], help="stale Run과 workspace 정합성을 확인합니다"
    )
    reconcile.add_argument("--stale-after", type=_positive_float, default=None, help="초")
    reconcile.add_argument(
        "--dry-run", action="store_true", help="판정만 하고 상태를 바꾸지 않습니다"
    )

    workspace_common = argparse.ArgumentParser(add_help=False)
    workspace_common.add_argument("--run-id", required=True)
    workspace_common.add_argument(
        "--repository-root", default=None, help="대상 repository의 local root"
    )
    workspace_common.add_argument(
        "--workspaces-root", default=None, help="worktree를 만들 worker root"
    )

    subparsers.add_parser(
        "workspace-create",
        parents=[common, workspace_common],
        help="Run에 격리된 branch와 worktree를 만듭니다",
    )
    subparsers.add_parser(
        "workspace-show",
        parents=[common, workspace_common],
        help="Run workspace의 기록과 실제 디스크 상태를 봅니다",
    )
    cleanup = subparsers.add_parser(
        "workspace-cleanup",
        parents=[common, workspace_common],
        help="terminal Run의 worktree를 제거합니다",
    )
    cleanup.add_argument(
        "--allow-dirty", action="store_true", help="저장되지 않은 변경이 있어도 제거"
    )
    cleanup.add_argument(
        "--delete-branch", action="store_true", help="branch까지 삭제(기본은 보존)"
    )

    executor_common = argparse.ArgumentParser(add_help=False)
    executor_common.add_argument("--run-id", required=True)
    executor_common.add_argument("--repository-root", default=None)
    executor_common.add_argument("--workspaces-root", default=None)
    executor_common.add_argument("--logs-root", default=None)

    start = subparsers.add_parser(
        "executor-start",
        parents=[common, executor_common],
        help="Run의 worktree에서 executor process를 실행합니다",
    )
    start.add_argument("--worker-id", default=None)
    start.add_argument("--timeout", type=_positive_float, default=None, help="초")
    start.add_argument(
        "--mock-mode",
        choices=MOCK_MODES,
        default="success",
        help="개발·테스트용 mock executor 모드. 실제 provider adapter가 아닙니다",
    )
    start.add_argument("--mock-write-file", default=None, help="worktree 안에 만들 파일")
    start.add_argument("--mock-sleep-seconds", type=float, default=1.0)
    start.add_argument("--mock-exit-code", type=int, default=0)

    subparsers.add_parser(
        "executor-show",
        parents=[common, executor_common],
        help="Run의 execution 기록과 process 상태를 봅니다",
    )
    cancel_exec = subparsers.add_parser(
        "executor-cancel",
        parents=[common, executor_common],
        help="실행 중인 executor process를 취소합니다",
    )
    cancel_exec.add_argument("--reason", default="manual_cancel")
    cancel_exec.add_argument("--grace", type=_positive_float, default=None, help="초")

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


def _emit(payload: dict[str, Any], indent: int | None) -> None:
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
    if _option(args, "no_queue_label", False):
        polling = replace(polling, require_queue_label=False)
    claim = config.claim
    if lease_ttl := _option(args, "lease_ttl"):
        claim = replace(claim, lease_ttl_seconds=lease_ttl)
    run = config.run
    if stale_after := _option(args, "stale_after"):
        run = replace(run, stale_after_seconds=stale_after)
    workspace = config.workspace
    if repository_root := _option(args, "repository_root"):
        workspace = replace(workspace, repository_root=repository_root)
    if workspaces_root := _option(args, "workspaces_root"):
        workspace = replace(workspace, workspaces_root=workspaces_root)
    if logs_root := _option(args, "logs_root"):
        workspace = replace(workspace, logs_root=logs_root)
    executor = config.executor
    if timeout := _option(args, "timeout"):
        executor = replace(executor, timeout_seconds=timeout)
    return replace(
        config, polling=polling, claim=claim, run=run, workspace=workspace, executor=executor
    )


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
        if args.command == "runs":
            return _run_runs(args, config)
        if args.command == "run-start":
            return _run_run_start(args, config)
        if args.command == "run-heartbeat":
            return _run_run_heartbeat(args, config)
        if args.command == "run-finish":
            return _run_run_finish(args, config)
        if args.command == "reconcile":
            return _run_reconcile(args, config)
        if args.command == "workspace-create":
            return _run_workspace_create(args, config)
        if args.command == "workspace-show":
            return _run_workspace_show(args, config)
        if args.command == "workspace-cleanup":
            return _run_workspace_cleanup(args, config)
        if args.command == "executor-start":
            return _run_executor_start(args, config)
        if args.command == "executor-show":
            return _run_executor_show(args, config)
        if args.command == "executor-cancel":
            return _run_executor_cancel(args, config)
    except IssueSourceError as error:
        _emit(
            {"status": "SourceError", "category": error.category, "message": error.message},
            _option(args, "indent", 2),
        )
        return 2
    except RunError as error:
        _emit(
            {"status": "RunError", "category": error.category, "message": error.message},
            _option(args, "indent", 2),
        )
        return 1
    except WorkspaceError as error:
        _emit(
            {"status": "WorkspaceError", "category": error.category, "message": error.message},
            _option(args, "indent", 2),
        )
        return 1
    except ExecutorError as error:
        payload = {
            "status": "ExecutorError",
            "category": error.category,
            "message": error.message,
        }
        gate = getattr(error, "gate", None)
        if gate is not None:
            payload["gate"] = gate.evidence()
        _emit(payload, _option(args, "indent", 2))
        return 1
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
            # 무한 watch는 종료되지 않으므로 pass마다 한 줄씩 즉시 출력합니다.
            # `run()`도 unbounded면 report를 누적하지 않습니다.
            failed = False

            def emit_pass(report) -> None:
                nonlocal failed
                failed = failed or report.error is not None
                # NDJSON: pass마다 정확히 한 줄. `indent=0`은 줄바꿈을 넣습니다.
                _emit({"status": "Polled", **report.to_dict()}, None)
                sys.stdout.flush()

            poller.run(max_iterations=args.iterations, on_report=emit_pass)
            return 0 if not failed else 2
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
                    "approved": bool(row["approved"]),
                    "approval_signal": row["approval_signal"],
                    "revoke_reason": row["revoke_reason"],
                    "claimable": bool(row["approved"]) and claim is None,
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


def _run_runs(args: argparse.Namespace, config: WorkerConfig) -> int:
    with TaskStore(config.database_path) as store:
        runs = store.runs(task_id=args.task_id, limit=args.limit)
    _emit(
        {"status": "Runs", "count": len(runs), "runs": [run.to_dict() for run in runs]},
        _option(args, "indent", 2),
    )
    return 0


def _run_run_start(args: argparse.Namespace, config: WorkerConfig) -> int:
    worker_id = args.worker_id or _default_worker_id()
    with TaskStore(config.database_path) as store:
        run = store.start_run(
            args.task_id, worker_id, previous_run_id=args.previous_run_id
        )
    _emit({"status": "RunStarted", "run": run.to_dict()}, _option(args, "indent", 2))
    return 0


def _run_run_heartbeat(args: argparse.Namespace, config: WorkerConfig) -> int:
    worker_id = args.worker_id or _default_worker_id()
    with TaskStore(config.database_path) as store:
        run = store.heartbeat(args.run_id, worker_id)
    _emit({"status": "Heartbeat", "run": run.to_dict()}, _option(args, "indent", 2))
    return 0


def _run_run_finish(args: argparse.Namespace, config: WorkerConfig) -> int:
    status = RunStatus(args.status)
    failure = None
    if args.failure_category:
        failure = RunFailure(args.failure_category, args.failure_message)
    elif status is RunStatus.FAILED:
        failure = RunFailure("unknown", args.failure_message or "사유가 지정되지 않았습니다.")
    elif status is RunStatus.CANCELLED and args.failure_message:
        failure = RunFailure("cancelled_by_human", args.failure_message)

    with TaskStore(config.database_path) as store:
        run = store.finish_run(
            args.run_id, status, worker_id=args.worker_id, failure=failure
        )
    _emit({"status": "RunFinished", "run": run.to_dict()}, _option(args, "indent", 2))
    return 0


def _workspace_service(store: TaskStore, config: WorkerConfig) -> WorkspaceService:
    """workspace 서비스를 만듭니다. repository root는 반드시 명시돼야 합니다."""

    root = config.workspace.repository_root
    if not root:
        raise WorkspaceError(
            "repository_root_missing",
            "repository root가 지정되지 않았습니다. --repository-root 또는 "
            "ATLAS_REPOSITORY_ROOT를 설정하세요. 현재 디렉터리를 추측하지 않습니다.",
        )
    planner = WorkspacePlanner(
        root,
        config.workspace.resolved_workspaces_root(),
        repository=config.polling.repository,
        base_branch="main",
        git_timeout_seconds=config.workspace.git_timeout_seconds,
    )
    return WorkspaceService(store, planner)


def _run_workspace_create(args: argparse.Namespace, config: WorkerConfig) -> int:
    with TaskStore(config.database_path) as store:
        result = _workspace_service(store, config).create(args.run_id)
    _emit(
        {"status": "WorkspaceReady" if result.created else "WorkspaceExists", **result.to_dict()},
        _option(args, "indent", 2),
    )
    return 0


def _run_workspace_show(args: argparse.Namespace, config: WorkerConfig) -> int:
    with TaskStore(config.database_path) as store:
        report = _workspace_service(store, config).show(args.run_id)
    _emit({"status": "Workspace", **report}, _option(args, "indent", 2))
    return 0


def _run_workspace_cleanup(args: argparse.Namespace, config: WorkerConfig) -> int:
    with TaskStore(config.database_path) as store:
        result = _workspace_service(store, config).cleanup(
            args.run_id,
            allow_dirty=args.allow_dirty,
            delete_branch=args.delete_branch,
        )
    _emit({"status": "WorkspaceCleanup", **result.to_dict()}, _option(args, "indent", 2))
    return 0 if result.error is None else 1


def _run_reconcile(args: argparse.Namespace, config: WorkerConfig) -> int:
    with TaskStore(config.database_path) as store:
        workspaces = None
        executions = None
        if config.workspace.repository_root:
            workspaces = _workspace_service(store, config)
            executions = _execution_service(store, config)
        reconciler = RunReconciler(store, config.run, workspaces, executions)
        if args.dry_run:
            verdicts = [reconciler.evaluate(run) for run in store.active_runs()]
            payload = {
                "status": "ReconcileDryRun",
                "checked": len(verdicts),
                "verdicts": [verdict.to_dict() for verdict in verdicts],
            }
        else:
            payload = {"status": "Reconciled", **reconciler.reconcile().to_dict()}
    _emit(payload, _option(args, "indent", 2))
    return 0


def _execution_service(store: TaskStore, config: WorkerConfig) -> ExecutionService:
    logs_root = config.workspace.resolved_logs_root()
    if not logs_root:
        raise ExecutorError(
            "logs_root_missing",
            "log root를 결정할 수 없습니다. --repository-root 또는 --logs-root를 지정하세요.",
        )
    return ExecutionService(
        store,
        LocalProcessExecutor(),
        _workspace_service(store, config),
        logs_root,
        config.run,
    )


def _mock_argv(args: argparse.Namespace) -> tuple[str, ...]:
    """mock executor를 별도 process로 띄우는 argv를 만듭니다."""

    argv = [sys.executable, "-m", "atlas.mock_executor", "--mode", args.mock_mode]
    if args.mock_write_file:
        argv += ["--write-file", args.mock_write_file]
    if args.mock_mode in ("sleep", "child"):
        argv += ["--sleep-seconds", str(args.mock_sleep_seconds)]
    if args.mock_exit_code:
        argv += ["--exit-code", str(args.mock_exit_code)]
    return tuple(argv)


def _run_executor_start(args: argparse.Namespace, config: WorkerConfig) -> int:
    worker_id = args.worker_id or _default_worker_id()
    with TaskStore(config.database_path) as store:
        service = _execution_service(store, config)
        outcome = service.run(
            args.run_id,
            worker_id,
            _mock_argv(args),
            timeout_seconds=config.executor.timeout_seconds,
            environment={"PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
            grace_period_seconds=config.executor.grace_period_seconds,
            max_output_bytes=config.executor.max_output_bytes,
        )
        if outcome.result is not None:
            service.apply_to_run(args.run_id, outcome.result)
    _emit({"status": "ExecutorFinished", **outcome.to_dict()}, _option(args, "indent", 2))
    return 0 if outcome.result and outcome.result.succeeded else 1


def _run_executor_show(args: argparse.Namespace, config: WorkerConfig) -> int:
    with TaskStore(config.database_path) as store:
        report = _execution_service(store, config).show(args.run_id)
    _emit({"status": "Executions", **report}, _option(args, "indent", 2))
    return 0


def _run_executor_cancel(args: argparse.Namespace, config: WorkerConfig) -> int:
    grace = args.grace or config.executor.grace_period_seconds
    with TaskStore(config.database_path) as store:
        outcome = _execution_service(store, config).cancel(args.run_id, args.reason, grace)
    _emit({"status": "ExecutorCancel", **outcome}, _option(args, "indent", 2))
    return 0 if outcome.get("cancelled") else 1


if __name__ == "__main__":
    raise SystemExit(main())
