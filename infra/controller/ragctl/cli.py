"""Command line interface: ragctl start | stop | status."""

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ragctl.config import load_config
from ragctl.engine import (
    LOG_DIR,
    Context,
    LockKeeper,
    Options,
    StateStore,
    Step,
    dry_run,
    prepare_context,
    run_steps,
)
from ragctl.errors import RagctlError
from ragctl.gateway import AwsGateway
from ragctl.terraform import Terraform
from ragctl.workflows import read_latest_manifest, start_steps, stop_steps


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "message": record.getMessage(),
            **getattr(record, "fields", {}),
        })


def configure_logging(command: str) -> None:
    logger = logging.getLogger("ragctl")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logger.addHandler(console)
    if command != "status":
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(LOG_DIR / f"{command}.jsonl")
        handler.setFormatter(JsonLinesFormatter())
        logger.addHandler(handler)


def collect_status(ctx: Context) -> Dict[str, Any]:
    aws, cfg = ctx.aws, ctx.cfg
    state = StateStore(aws).load()
    domain = aws.domain()
    if domain and domain["deleted"]:
        domain = None
    service = aws.api_service()
    nat = aws.nat_gateways()
    balancer = aws.load_balancer()
    redis = aws.redis_status()
    manifest, problem = read_latest_manifest(ctx)
    pipe = aws.pipe_state()
    gate = aws.gate_open()
    billed = {
        "opensearch": bool(domain), "api_service": bool(service), "nat_gateway": bool(nat),
        "load_balancer": bool(balancer), "redis": bool(redis),
    }

    if state.get("phase") in ("IN_PROGRESS", "FAILED"):
        overall = f"{state.get('operation', 'unknown').upper()}_{state['phase']}"
    elif not any(billed.values()) and pipe == "STOPPED" and not gate:
        overall = "STOPPED"
    elif domain and service and service["running"] > 0 and pipe == "RUNNING" and gate:
        overall = "RUNNING"
    else:
        overall = "DEGRADED"

    snapshot = (manifest or {}).get("snapshot") or {}
    return {
        "environment": cfg.prefix,
        "overall": overall,
        "last_operation": {key: state.get(key) for key in
                           ("operation", "operation_id", "phase", "step", "last_error", "updated_at")},
        "ingestion": {
            "accepting": gate,
            "pipe": pipe,
            "running_executions": len(aws.running_executions()),
            "worker_tasks": len(aws.worker_tasks()),
            "queue": aws.queue_depth(),
            "dead_letter_queue": aws.queue_depth(cfg.dlq_url),
        },
        "billed_resources": {
            "opensearch": domain and {k: domain[k] for k in ("engine_version", "instance_type", "processing")},
            "api_service": service and {k: service[k] for k in ("desired", "running")},
            "nat_gateways": nat,
            "load_balancer": balancer,
            "redis": redis,
        },
        "latest_backup": {
            "valid": bool(manifest) and problem is None,
            "problem": problem,
            "snapshot": snapshot.get("name"),
            "created_at": (manifest or {}).get("created_at"),
            "documents": {k: v.get("docs") for k, v in snapshot.get("indices", {}).items()},
        },
    }


def print_status(status: Dict[str, Any]) -> None:
    ingestion, billed, backup = status["ingestion"], status["billed_resources"], status["latest_backup"]
    last = status["last_operation"]
    print(f"Environment : {status['environment']}")
    print(f"Overall     : {status['overall']}")
    print(f"Last action : {last['operation']} {last['phase']} at {last['updated_at']}")
    if last.get("last_error"):
        print(f"Last error  : {last['step']}: {last['last_error']}")
    print(f"Ingestion   : accepting={ingestion['accepting']} pipe={ingestion['pipe']} "
          f"executions={ingestion['running_executions']} tasks={ingestion['worker_tasks']}")
    print(f"Queue       : {ingestion['queue']}  DLQ: {ingestion['dead_letter_queue']}")
    print(f"OpenSearch  : {billed['opensearch'] or 'not running'}")
    print(f"API service : {billed['api_service'] or 'not running'}")
    print(f"NAT Gateway : {billed['nat_gateways'] or 'not running'}")
    print(f"ALB         : {billed['load_balancer'] or 'not running'}")
    print(f"Redis       : {billed['redis'] or 'not running'}")
    print(f"Backup      : {backup['snapshot'] or 'none'} valid={backup['valid']} "
          f"created={backup['created_at']} documents={backup['documents']}")
    if backup["problem"]:
        print(f"Backup issue: {backup['problem']}")


def confirm(prefix: str, verb: str, details: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise RagctlError("Confirmation is required. Run interactively or pass --yes.")
    print(details)
    expected = f"{verb} {prefix}"
    return input(f"Type '{expected}' to continue: ").strip() == expected


def print_plan(operation: str, plan: List[Dict[str, str]]) -> None:
    print(f"Dry run for {operation}. No changes were made.")
    for number, item in enumerate(plan, 1):
        print(f"{number:2}. {item['step']:<22} {item['action']:<18} {item['description']}")
        if item["detail"]:
            print(f"    {item['detail']}")


def run_operation(ctx: Context, operation: str, steps: List[Step], assume_yes: bool) -> int:
    store = StateStore(ctx.aws)
    prepare_context(ctx, store, operation)
    if ctx.options.dry_run:
        print_plan(operation, dry_run(ctx, steps))
        return 0

    details: Optional[str] = None
    if operation == "stop":
        details = (
            f"This pauses ingestion, snapshots OpenSearch, then deletes the disposable runtime of "
            f"{ctx.cfg.prefix}: OpenSearch, Redis, ALB, NAT Gateway and the API service.\n"
            "S3 data, verified backups, DynamoDB and SQS messages are kept."
        )
    elif ctx.options.ignore_snapshot:
        details = "This ignores the latest snapshot and rebuilds the index from S3 documents."
    if details and not confirm(ctx.cfg.prefix, operation, details, assume_yes):
        print("Cancelled. No changes were made.")
        return 2

    with LockKeeper(ctx.aws, operation) as lock:
        prepare_context(ctx, store, operation)  # Re-read now that the lock is held.
        run_steps(ctx, steps, store, lock.lost)
    print(f"{operation} completed. Operation ID: {ctx.operation_id}")
    print_status(collect_status(ctx))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ragctl", description=__doc__)
    parser.add_argument("--project", default=os.environ.get("RAGCTL_PROJECT", "mfrag"))
    parser.add_argument("--environment", default=os.environ.get("RAGCTL_ENVIRONMENT", "prod"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="Report live platform status")
    status.add_argument("--json", action="store_true")

    stop = commands.add_parser("stop", help="Drain, back up and delete billed runtime resources")
    stop.add_argument("--dry-run", action="store_true")
    stop.add_argument("--yes", action="store_true", help="Skip the typed confirmation")
    stop.add_argument("--drain-timeout", type=int, default=3600, help="Seconds to wait for active ingestion")
    stop.add_argument("--requeue-on-timeout", action="store_true",
                      help="After the drain timeout, requeue active work and stop it")

    start = commands.add_parser("start", help="Recreate the runtime and resume ingestion")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--yes", action="store_true")
    start.add_argument("--ignore-snapshot", action="store_true",
                       help="Do not restore; create an empty index and re-index documents from S3")
    start.add_argument("--skip-reconcile", action="store_true", help="Do not scan S3 for unqueued documents")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.command)
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile  # Terraform must use the same identity.
    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        cfg = load_config(session, args.project, args.environment)
        aws = AwsGateway(session, cfg)
        logger = logging.getLogger("ragctl")
        tf = Terraform(cfg, args.project, args.environment, logger.info)
        options = Options(
            dry_run=getattr(args, "dry_run", False),
            drain_timeout=getattr(args, "drain_timeout", 3600),
            requeue_on_timeout=getattr(args, "requeue_on_timeout", False),
            ignore_snapshot=getattr(args, "ignore_snapshot", False),
            skip_reconcile=getattr(args, "skip_reconcile", False),
        )
        ctx = Context(cfg=cfg, aws=aws, tf=tf, options=options)
        if args.command == "status":
            status = collect_status(ctx)
            if args.json:
                print(json.dumps(status, indent=2, default=str))
            else:
                print_status(status)
            return 0
        steps = stop_steps() if args.command == "stop" else start_steps()
        return run_operation(ctx, args.command, steps, args.yes)
    except KeyboardInterrupt:
        print("Interrupted. Progress is saved; run the same command to resume.", file=sys.stderr)
        return 130
    except (RagctlError, ClientError, BotoCoreError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Progress is saved. Fix the cause and run the same command to resume.", file=sys.stderr)
        return 1
