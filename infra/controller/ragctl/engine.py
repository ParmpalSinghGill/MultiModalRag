"""
Resumable step runner.

Each step observes real AWS state in check() before acting, so a repeated or
interrupted run converges instead of redoing work. Progress and data needed for a
resume are persisted to DynamoDB after every step.
"""

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ragctl.errors import RagctlError, WaitTimeout

LOCK_TTL_SECONDS = 300
JOURNAL_TTL_SECONDS = 90 * 24 * 3600
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"

logger = logging.getLogger("ragctl")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_operation_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    return f"{stamp}-{secrets.token_hex(2)}"


@dataclass
class Options:
    dry_run: bool = False
    drain_timeout: int = 3600
    requeue_on_timeout: bool = False
    ignore_snapshot: bool = False
    skip_reconcile: bool = False
    wait_timeout: int = 3600


@dataclass
class Step:
    name: str
    description: str
    check: Callable[["Context"], bool]
    apply: Callable[["Context"], None]
    always: bool = False  # Read-only validation that runs on every attempt.
    preview: Optional[Callable[["Context"], str]] = None
    verify_after: bool = True


@dataclass
class Context:
    cfg: Any
    aws: Any
    tf: Any
    options: Options
    operation: str = ""
    operation_id: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    verified_endpoint: Optional[str] = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    results: List[Dict[str, str]] = field(default_factory=list)

    def log(self, message: str, **fields: Any) -> None:
        logger.info(message, extra={"fields": {"operation_id": self.operation_id, **fields}})

    def wait_until(self, description: str, predicate: Callable[[], bool], timeout: Optional[int] = None,
                   interval: int = 15) -> None:
        limit = timeout if timeout is not None else self.options.wait_timeout
        deadline = self.clock() + limit
        announced = False
        while not predicate():
            if self.clock() >= deadline:
                raise WaitTimeout(f"Timed out after {limit}s waiting for {description}")
            if not announced:
                self.log(f"Waiting for {description}")
                announced = True
            self.sleep(interval)

    def endpoint(self) -> Optional[str]:
        domain = self.aws.domain()
        if domain and not domain["deleted"]:
            return domain["endpoint"]
        return None

    def ops(self, action: str, **arguments: Any) -> Dict[str, Any]:
        endpoint = self.endpoint()
        if not endpoint:
            raise RagctlError("OpenSearch endpoint is not available")
        return self.aws.ops(action, endpoint=endpoint, **arguments)


class StateStore:
    def __init__(self, aws: Any):
        self.aws = aws

    def load(self) -> Dict[str, Any]:
        return self.aws.get_control("CONTROLLER", "STATE") or {}

    def save(self, ctx: Context, phase: str, step: str = "", error: str = "") -> None:
        self.aws.put_control({
            "pk": "CONTROLLER",
            "sk": "STATE",
            "operation": ctx.operation,
            "operation_id": ctx.operation_id,
            "phase": phase,
            "step": step,
            "last_error": error[:1500],
            "verified_endpoint": ctx.verified_endpoint,
            "context_json": json.dumps(ctx.data),
            "updated_at": utc_now(),
        })

    def journal(self, ctx: Context, step: str, status: str, detail: str = "") -> None:
        self.aws.put_control({
            "pk": f"OPERATION#{ctx.operation_id}",
            "sk": f"{utc_now()}#{secrets.token_hex(3)}",
            "operation": ctx.operation,
            "step": step,
            "status": status,
            "detail": detail[:1500],
            "expires_at": int(time.time()) + JOURNAL_TTL_SECONDS,
        })


def prepare_context(ctx: Context, store: StateStore, operation: str) -> None:
    """Resumes an unfinished run of the same operation, otherwise begins a new one."""
    state = store.load()
    ctx.operation = operation
    ctx.verified_endpoint = state.get("verified_endpoint")
    resumable = state.get("operation") == operation and state.get("phase") in ("IN_PROGRESS", "FAILED")
    if resumable:
        ctx.operation_id = state["operation_id"]
        ctx.data = json.loads(state.get("context_json") or "{}")
    else:
        ctx.operation_id = new_operation_id()
        ctx.data = {}


def dry_run(ctx: Context, steps: List[Step]) -> List[Dict[str, str]]:
    plan = []
    for step in steps:
        done = False if step.always else step.check(ctx)
        action = "already satisfied" if done else ("would verify" if step.always else "would run")
        detail = step.preview(ctx) if step.preview and not done else ""
        plan.append({"step": step.name, "action": action, "description": step.description, "detail": detail})
    return plan


def run_steps(ctx: Context, steps: List[Step], store: StateStore,
              lock_lost: Optional[threading.Event] = None) -> None:
    current = ""
    try:
        store.save(ctx, "IN_PROGRESS")
        for step in steps:
            current = step.name
            if lock_lost is not None and lock_lost.is_set():
                raise RagctlError("The controller lock was lost. Stopping before the next step.")
            if not step.always and step.check(ctx):
                ctx.log(f"[skip] {step.name}: already satisfied", step=step.name, status="skipped")
                ctx.results.append({"step": step.name, "status": "skipped"})
                store.journal(ctx, step.name, "skipped")
                continue
            ctx.log(f"[run ] {step.name}: {step.description}", step=step.name, status="started")
            store.save(ctx, "IN_PROGRESS", step.name)
            store.journal(ctx, step.name, "started")
            started = time.monotonic()
            step.apply(ctx)
            if step.verify_after and not step.always and not step.check(ctx):
                raise RagctlError(f"Step {step.name} finished but its end state was not reached")
            seconds = int(time.monotonic() - started)
            ctx.log(f"[done] {step.name} ({seconds}s)", step=step.name, status="completed")
            ctx.results.append({"step": step.name, "status": "completed"})
            store.save(ctx, "IN_PROGRESS", step.name)
            store.journal(ctx, step.name, "completed", f"{seconds}s")
        store.save(ctx, "COMPLETE")
    except BaseException as exc:
        message = f"{type(exc).__name__}: {exc}"
        ctx.results.append({"step": current, "status": "failed"})
        store.save(ctx, "FAILED", current, message)
        store.journal(ctx, current, "failed", message)
        raise


class LockKeeper:
    """Holds the operation lock and renews it from a background thread."""

    def __init__(self, aws: Any, operation: str):
        self.aws = aws
        self.operation = operation
        self.owner = f"{operation}-{secrets.token_hex(6)}"
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._renew, daemon=True)

    def _renew(self) -> None:
        while not self._stop.wait(LOCK_TTL_SECONDS / 3):
            try:
                if not self.aws.renew_lock(self.owner, LOCK_TTL_SECONDS):
                    self.lost.set()
                    return
            except Exception as exc:  # A transient failure must not kill the run.
                logger.warning("Lock renewal failed: %s", exc)

    def __enter__(self) -> "LockKeeper":
        self.aws.acquire_lock(self.owner, self.operation, LOCK_TTL_SECONDS)
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.aws.release_lock(self.owner)
