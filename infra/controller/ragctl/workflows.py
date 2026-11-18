"""The stop and start workflows, expressed as ordered, re-runnable steps."""

import json
from typing import Any, Dict, List, Optional, Tuple

from ragctl.engine import Context, Step, utc_now
from ragctl.errors import (
    DrainTimeout,
    RagctlError,
    SnapshotInvalid,
    VerificationFailed,
    WaitTimeout,
)

MANIFEST_VERSION = 1
MAX_SNAPSHOT_ATTEMPTS = 3
MAX_RESTORE_ATTEMPTS = 3
TASK_STOP_TIMEOUT = 300
PIPE_TIMEOUT = 600


# ------------------------------- Manifests ---------------------------------
def read_latest_manifest(ctx: Context) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Returns (manifest, problem). (None, None) means none has ever been written."""
    found = ctx.aws.get_json(ctx.cfg.latest_manifest_key)
    if found is None:
        return None, None
    manifest, checksum_ok = found
    if not checksum_ok:
        return manifest, "manifest checksum does not match its contents"
    if manifest.get("schema_version") != MANIFEST_VERSION:
        return manifest, f"unsupported manifest version {manifest.get('schema_version')}"
    if not manifest.get("verified"):
        return manifest, "manifest is not marked verified"
    snapshot = manifest.get("snapshot")
    if snapshot:
        if not snapshot.get("indices"):
            return manifest, "snapshot lists no indices"
        if not ctx.aws.object_exists(ctx.cfg.snapshot_file_key(snapshot["uuid"])):
            return manifest, f"snapshot files for {snapshot['name']} are missing from S3"
    return manifest, None


def version_tuple(engine: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in engine.split("_")[-1].split("."))


# --------------------------------- Stop -------------------------------------
def gate_closed(ctx: Context) -> bool:
    return not ctx.aws.gate_open()


def close_gate(ctx: Context) -> None:
    ctx.aws.set_gate(False)


def pipe_stopped(ctx: Context) -> bool:
    return ctx.aws.pipe_state() == "STOPPED"


def stop_pipe(ctx: Context) -> None:
    if ctx.aws.pipe_state() not in ("STOPPING",):
        ctx.aws.stop_pipe()
    ctx.wait_until("the pipe to stop", lambda: pipe_stopped(ctx), PIPE_TIMEOUT, 10)


def drained(ctx: Context) -> bool:
    return not ctx.aws.running_executions() and not ctx.aws.worker_tasks()


def requeue_running_executions(ctx: Context) -> None:
    """The message is restored before the execution stops, so no work can be lost."""
    for arn in ctx.aws.running_executions():
        payload = ctx.aws.execution_input(arn)
        records = payload if isinstance(payload, list) else [payload]
        for record in records:
            body = record.get("body", record) if isinstance(record, dict) else record
            ctx.aws.send_message(body if isinstance(body, str) else json.dumps(body))
        ctx.aws.stop_execution(arn, "Requeued by ragctl stop after the drain timeout")
        ctx.log(f"Requeued and stopped {arn.rsplit(':', 1)[-1]}")


def drain(ctx: Context) -> None:
    try:
        ctx.wait_until("active ingestion to finish", lambda: drained(ctx), ctx.options.drain_timeout, 30)
    except WaitTimeout:
        active = len(ctx.aws.running_executions())
        if not ctx.options.requeue_on_timeout:
            raise DrainTimeout(
                f"{active} execution(s) are still running. Ingestion stays paused and nothing "
                "was deleted. Run stop again to keep waiting, add --requeue-on-timeout to "
                "requeue them safely, or run start to resume."
            )
        requeue_running_executions(ctx)
        ctx.wait_until("interrupted tasks to exit", lambda: drained(ctx), TASK_STOP_TIMEOUT, 10)


def snapshot_decided(ctx: Context) -> bool:
    return not ctx.endpoint() or bool(ctx.data.get("snapshot"))


def take_verified_snapshot(ctx: Context, indices: Dict[str, Any]) -> Dict[str, Any]:
    names = sorted(indices)
    ctx.ops("register_repository")
    ctx.ops("verify_repository")
    for attempt in range(1, MAX_SNAPSHOT_ATTEMPTS + 1):
        name = f"stop-{ctx.operation_id}-r{attempt}"
        if not ctx.ops("get_snapshot", snapshot=name)["snapshot"]:
            ctx.ops("create_snapshot", snapshot=name, indices=names)

        def finished() -> bool:
            info = ctx.ops("get_snapshot", snapshot=name)["snapshot"]
            return bool(info) and info["state"] != "IN_PROGRESS"

        ctx.wait_until(f"snapshot {name}", finished)
        info = ctx.ops("get_snapshot", snapshot=name)["snapshot"]
        problems = []
        if info["state"] != "SUCCESS":
            problems.append(f"state {info['state']}")
        if info["shards"].get("failed", 0) != 0 or info["failures"]:
            problems.append("failed shards")
        if set(names) - set(info["indices"]):
            problems.append("missing indices")
        if not problems and not ctx.aws.object_exists(ctx.cfg.snapshot_file_key(info["uuid"])):
            problems.append("metadata file not found in S3")
        if not problems:
            return {"name": name, "uuid": info["uuid"], "version": info["version"], "indices": indices}
        ctx.log(f"Snapshot {name} rejected: {', '.join(problems)}")
    raise VerificationFailed("No verified snapshot could be created. Nothing was deleted.")


def create_snapshot(ctx: Context) -> None:
    previous, problem = read_latest_manifest(ctx)
    has_previous = bool(previous and not problem and previous.get("snapshot"))
    authoritative = ctx.verified_endpoint == ctx.endpoint()

    # A domain that never passed start verification may hold a partial restore.
    # Snapshotting it could replace the good backup with incomplete data.
    if not authoritative and has_previous:
        ctx.log("Domain was never verified, keeping the existing verified snapshot")
        ctx.data["snapshot"] = {"kind": "retained"}
        return

    indices = ctx.ops("list_indices")["indices"]
    red = [name for name, info in indices.items() if info["health"] == "red"]
    if red:
        raise VerificationFailed(f"Indices are red and cannot be snapshotted safely: {red}")
    if not indices:
        ctx.data["snapshot"] = {"kind": "retained"} if has_previous else {"kind": "empty"}
        return
    ctx.data["snapshot"] = {"kind": "created", **take_verified_snapshot(ctx, indices)}


def manifest_saved(ctx: Context) -> bool:
    decision = ctx.data.get("snapshot")
    if not decision:
        return not ctx.endpoint()
    if decision["kind"] == "retained":
        return True
    found = ctx.aws.get_json(ctx.cfg.latest_manifest_key)
    return bool(found and found[1] and found[0].get("operation_id") == ctx.operation_id)


def save_manifest(ctx: Context) -> None:
    decision = ctx.data["snapshot"]
    domain = ctx.aws.domain() or {}
    snapshot = None
    if decision["kind"] == "created":
        snapshot = {k: decision[k] for k in ("name", "uuid", "version", "indices")}
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "verified": True,
        "operation_id": ctx.operation_id,
        "created_at": utc_now(),
        "snapshot": snapshot,
        "repository": ctx.cfg.snapshot_repository,
        "base_path": ctx.cfg.snapshot_base_path,
        "opensearch": {k: domain.get(k) for k in ("name", "engine_version", "instance_type", "instance_count", "volume_gb")},
        "image_tag": ctx.cfg.image_tag,
        "redis_enabled": ctx.cfg.enable_redis,
        "runtime_outputs": ctx.tf.outputs(),
        "api_service": ctx.aws.api_service(),
        "pipe_state": ctx.aws.pipe_state(),
        "queue": ctx.aws.queue_depth(),
        "dead_letter_queue": ctx.aws.queue_depth(ctx.cfg.dlq_url),
    }
    versioned_key = ctx.cfg.manifest_key(ctx.operation_id)
    digest = ctx.aws.put_json(versioned_key, manifest)
    stored = ctx.aws.get_json(versioned_key)
    if not stored or not stored[1] or stored[0] != manifest:
        raise VerificationFailed(f"Manifest {versioned_key} failed read-back ({digest})")
    ctx.aws.put_json(ctx.cfg.latest_manifest_key, manifest)


def api_stopped(ctx: Context) -> bool:
    service = ctx.aws.api_service()
    return service is None or (service["desired"] == 0 and service["running"] == 0)


def stop_api(ctx: Context) -> None:
    ctx.aws.scale_api(0)
    ctx.wait_until("API tasks to stop", lambda: api_stopped(ctx), 600, 10)


def runtime_absent(ctx: Context) -> bool:
    return not ctx.tf.state_resources()


def preview_destroy(ctx: Context) -> str:
    return f"terraform destroy plan: {ctx.tf.plan(destroy=True, keep=False).summary}"


def destroy_runtime(ctx: Context) -> None:
    if ctx.endpoint():
        decision = ctx.data.get("snapshot")
        if not decision or not manifest_saved(ctx):
            raise VerificationFailed("Refusing to delete OpenSearch without a verified saved snapshot")
    plan = ctx.tf.plan(destroy=True)
    ctx.log(f"Destroying runtime resources: {plan.summary}")
    ctx.tf.apply(plan)


def verify_stopped(ctx: Context) -> None:
    aws = ctx.aws
    remaining = []
    domain = aws.domain()
    if domain and not domain["deleted"]:
        remaining.append("OpenSearch domain")
    if aws.api_service():
        remaining.append("ECS API service")
    if aws.nat_gateways():
        remaining.append("NAT Gateway")
    if aws.load_balancer():
        remaining.append("load balancer")
    if aws.redis_status():
        remaining.append("Redis")
    if remaining:
        raise VerificationFailed(f"Still present after destroy: {remaining}")
    if aws.gate_open() or aws.pipe_state() != "STOPPED":
        raise VerificationFailed("Ingestion is not paused")
    ctx.log(f"Queued messages preserved: {aws.queue_depth()}")
    ctx.verified_endpoint = None


def stop_steps() -> List[Step]:
    return [
        Step("close-ingestion-gate", "Stop accepting new ingestion jobs", gate_closed, close_gate),
        Step("pause-pipe", "Pause the EventBridge Pipe; messages stay in SQS", pipe_stopped, stop_pipe),
        Step("drain", "Wait for Step Functions executions and worker tasks", drained, drain),
        Step("snapshot-opensearch", "Create and verify an S3 snapshot", snapshot_decided, create_snapshot),
        Step("save-manifest", "Store configuration, identifiers and snapshot ID", manifest_saved, save_manifest),
        Step("stop-api", "Scale the API service to zero", api_stopped, stop_api),
        Step("destroy-runtime", "Delete OpenSearch, Redis, ALB, NAT Gateway and API service",
             runtime_absent, destroy_runtime, preview=preview_destroy),
        Step("verify-stopped", "Confirm billed resources are gone and data is intact",
             lambda _: False, verify_stopped, always=True),
    ]


# --------------------------------- Start ------------------------------------
def preflight(ctx: Context) -> None:
    problems = [
        f"image {ctx.cfg.image_tag} is missing from the {name} repository"
        for name in ("api", "worker") if not ctx.aws.image_exists(name)
    ]
    if not ctx.aws.opensearch_service_role_exists():
        problems.append(
            "the OpenSearch service-linked role is missing; set "
            "create_opensearch_service_linked_role=true in the foundation stack"
        )
    if problems:
        raise RagctlError("Preflight failed before creating billed resources: " + "; ".join(problems))


def inspect_snapshot(ctx: Context) -> None:
    endpoint = ctx.endpoint()
    if endpoint and endpoint == ctx.verified_endpoint:
        ctx.data["index_plan"] = {"mode": "keep"}
        return
    manifest, problem = read_latest_manifest(ctx)
    if problem:
        if not ctx.options.ignore_snapshot:
            raise SnapshotInvalid(
                f"Latest snapshot cannot be trusted: {problem}. Nothing was created. "
                "Investigate it, or use --ignore-snapshot to rebuild the index from S3 documents."
            )
        ctx.log(f"Ignoring invalid snapshot as requested: {problem}")
        manifest = None
    if ctx.options.ignore_snapshot:
        manifest = None
    if manifest and manifest.get("snapshot"):
        ctx.data["index_plan"] = {
            "mode": "restore",
            "snapshot": manifest["snapshot"],
            "engine_version": manifest["opensearch"].get("engine_version"),
        }
        ctx.log(f"Will restore snapshot {manifest['snapshot']['name']}")
    else:
        ctx.data["index_plan"] = {"mode": "create"}
        ctx.log("No usable snapshot exists, the index will be created")


def preview_snapshot(ctx: Context) -> str:
    manifest, problem = read_latest_manifest(ctx)
    if ctx.options.ignore_snapshot:
        return "snapshot ignored: an empty index would be created and rebuilt from S3"
    if problem:
        return f"start would stop here: {problem}"
    if manifest and manifest.get("snapshot"):
        return f"would restore {manifest['snapshot']['name']}"
    return "no snapshot found: the index would be created"


def runtime_current(ctx: Context) -> bool:
    return not ctx.tf.plan(keep=False).changes


def preview_apply(ctx: Context) -> str:
    return f"terraform plan: {ctx.tf.plan(keep=False).summary}"


def apply_runtime(ctx: Context) -> None:
    plan = ctx.tf.plan()
    ctx.log(f"Creating runtime resources: {plan.summary}")
    ctx.tf.apply(plan)


def search_ready(ctx: Context) -> bool:
    domain = ctx.aws.domain()
    if not domain or domain["deleted"] or domain["processing"] or not domain["endpoint"]:
        return False
    return ctx.ops("cluster_health")["status"] in ("green", "yellow")


def wait_for_search(ctx: Context) -> None:
    ctx.wait_until("OpenSearch to become healthy", lambda: search_ready(ctx), interval=30)


def index_prepared(ctx: Context) -> bool:
    endpoint = ctx.endpoint()
    if not endpoint:
        return False
    return endpoint == ctx.verified_endpoint or ctx.data.get("index_ready_on") == endpoint


def restore_finished(ctx: Context, wanted: Dict[str, Any]) -> bool:
    if ctx.ops("recovery_active")["active"]:
        return False
    current = ctx.ops("list_indices")["indices"]
    return all(name in current and current[name]["health"] != "red" for name in wanted)


def restore_index(ctx: Context, plan: Dict[str, Any]) -> None:
    snapshot = plan["snapshot"]
    wanted = snapshot["indices"]
    domain = ctx.aws.domain()
    if plan.get("engine_version") and version_tuple(domain["engine_version"]) < version_tuple(plan["engine_version"]):
        raise SnapshotInvalid(
            f"Snapshot came from {plan['engine_version']} and cannot restore to {domain['engine_version']}"
        )
    ctx.ops("register_repository")
    ctx.ops("verify_repository")
    info = ctx.ops("get_snapshot", snapshot=snapshot["name"])["snapshot"]
    if not info or info["state"] != "SUCCESS" or info["uuid"] != snapshot["uuid"]:
        raise SnapshotInvalid(f"Snapshot {snapshot['name']} is not readable from the repository")

    for attempt in range(1, MAX_RESTORE_ATTEMPTS + 1):
        ctx.wait_until("running recoveries", lambda: not ctx.ops("recovery_active")["active"])
        current = ctx.ops("list_indices")["indices"]
        incomplete = [
            name for name, expected in wanted.items()
            if name not in current or current[name]["docs"] != expected["docs"] or current[name]["health"] == "red"
        ]
        if not incomplete:
            return
        for name in incomplete:
            if name in current:  # Only an unverified, partial restore can reach this point.
                ctx.ops("delete_partial_index", index=name)
        ctx.log(f"Restoring {incomplete} from {snapshot['name']} (attempt {attempt})")
        ctx.ops("restore_snapshot", snapshot=snapshot["name"], indices=incomplete)
        ctx.wait_until("the restore", lambda: restore_finished(ctx, wanted), interval=20)
    raise VerificationFailed("Restored document counts do not match the snapshot")


def prepare_index(ctx: Context) -> None:
    plan = ctx.data["index_plan"]
    if plan["mode"] == "restore":
        restore_index(ctx, plan)
    else:
        ctx.ops("ensure_index")
    ctx.data["index_ready_on"] = ctx.endpoint()


def redis_ready(ctx: Context) -> bool:
    return not ctx.cfg.enable_redis or ctx.aws.redis_status() == "available"


def wait_for_redis(ctx: Context) -> None:
    ctx.wait_until("Redis", lambda: redis_ready(ctx), interval=20)


def desired_api_count(ctx: Context) -> int:
    return int(ctx.tf.outputs().get("api_desired_count", 1))


def api_healthy(ctx: Context) -> bool:
    service = ctx.aws.api_service()
    if not service or service["desired"] < 1:
        return False
    return (
        service["steady"]
        and service["running"] >= service["desired"]
        and ctx.aws.healthy_targets(service["target_group_arn"]) >= service["desired"]
    )


def start_api(ctx: Context) -> None:
    service = ctx.aws.api_service()
    if not service:
        raise RagctlError("The API service does not exist after terraform apply")
    if service["desired"] < 1:
        ctx.aws.scale_api(desired_api_count(ctx))
    ctx.wait_until("API health checks", lambda: api_healthy(ctx), 1200, 15)


def verify_platform(ctx: Context) -> None:
    plan = ctx.data["index_plan"]
    check = ctx.ops("verify_index")
    if not check["ok"]:
        raise VerificationFailed(f"Index verification failed: {check['reason']}")
    if plan["mode"] == "restore":
        current = ctx.ops("list_indices")["indices"]
        for name, expected in plan["snapshot"]["indices"].items():
            if current.get(name, {}).get("docs") != expected["docs"]:
                raise VerificationFailed(f"Index {name} does not match snapshot document count")
    host = ctx.tf.outputs()["alb_dns_name"]
    result = ctx.aws.ops("http_check", host=host)
    if result["status"] != 200:
        raise VerificationFailed(f"API readiness returned {result['status']}: {result['body']}")
    ctx.verified_endpoint = ctx.endpoint()


def reconciled(ctx: Context) -> bool:
    return ctx.options.skip_reconcile or bool(ctx.data.get("reconciled"))


def reconcile_documents(ctx: Context) -> None:
    """
    Re-queues S3 documents with no recorded outcome, which covers SQS messages that
    expired during a long shutdown. A newly created index also needs completed
    documents re-indexed; their saved parse output makes that inexpensive.
    """
    rebuild = ctx.data["index_plan"]["mode"] == "create"
    queued = 0
    for document in ctx.aws.raw_objects():
        status = ctx.aws.document_status(document["doc_id"], document["etag"])
        # PROCESSING is included because its owner may be gone. If it is still
        # running, the workflow's claim step drops the duplicate.
        needed = status in (None, "PROCESSING") or (rebuild and status == "COMPLETED")
        if needed:
            force = rebuild and status == "COMPLETED"
            ctx.aws.send_message(json.dumps({"document": {**document, "force": force}}))
            queued += 1
    ctx.log(f"Reconciliation queued {queued} document(s)")
    ctx.data["reconciled"] = True


def gate_open(ctx: Context) -> bool:
    return ctx.aws.gate_open()


def open_gate(ctx: Context) -> None:
    ctx.aws.set_gate(True)


def pipe_running(ctx: Context) -> bool:
    return ctx.aws.pipe_state() == "RUNNING"


def start_pipe(ctx: Context) -> None:
    if ctx.aws.pipe_state() != "STARTING":
        ctx.aws.start_pipe()
    ctx.wait_until("the pipe to start", lambda: pipe_running(ctx), PIPE_TIMEOUT, 10)


def start_steps() -> List[Step]:
    return [
        Step("preflight", "Check images and service roles", lambda _: False, preflight, always=True),
        Step("inspect-snapshot", "Find and validate the latest snapshot",
             lambda _: False, inspect_snapshot, always=True, preview=preview_snapshot),
        Step("apply-runtime", "Create networking and managed services", runtime_current, apply_runtime,
             preview=preview_apply, verify_after=False),
        Step("wait-opensearch", "Wait for a healthy OpenSearch domain", search_ready, wait_for_search),
        Step("prepare-index", "Restore the snapshot, or create the index if none exists",
             index_prepared, prepare_index),
        Step("wait-redis", "Wait for Redis when enabled", redis_ready, wait_for_redis),
        Step("start-api", "Start the API service and wait for health checks", api_healthy, start_api),
        Step("verify-platform", "Verify indexes, mappings and API connectivity",
             lambda _: False, verify_platform, always=True),
        Step("reconcile-documents", "Queue S3 documents with no recorded outcome",
             reconciled, reconcile_documents),
        Step("open-ingestion-gate", "Accept ingestion jobs again", gate_open, open_gate),
        Step("resume-pipe", "Resume the EventBridge Pipe", pipe_running, start_pipe),
    ]
