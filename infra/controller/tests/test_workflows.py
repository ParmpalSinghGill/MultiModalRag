import json

import pytest

from fakes import ENDPOINT, INDEX, FakeCloud, FakeTerraform, make_config, make_context
from ragctl.engine import StateStore, dry_run, prepare_context, run_steps
from ragctl.errors import (
    DrainTimeout,
    SnapshotInvalid,
    TerraformFailed,
    UnsafePlan,
    VerificationFailed,
)
from ragctl.terraform import Change, assert_plan_safe
from ragctl.workflows import destroy_runtime, start_steps, stop_steps


def execute(cloud, tf, operation, **options):
    ctx = make_context(cloud, tf, **options)
    store = StateStore(cloud)
    prepare_context(ctx, store, operation)
    run_steps(ctx, stop_steps() if operation == "stop" else start_steps(), store)
    return ctx


def running_platform(**config):
    """A platform that has been started and verified, as in normal operation."""
    cloud = FakeCloud(make_config(**config))
    cloud.put_control({"pk": "CONTROLLER", "sk": "STATE", "operation": "start",
                       "operation_id": "old", "phase": "COMPLETE", "verified_endpoint": ENDPOINT})
    return cloud, FakeTerraform(cloud)


def stopped_platform():
    cloud, tf = running_platform()
    execute(cloud, tf, "stop")
    cloud.calls.clear()
    return cloud, tf


def position(cloud, name):
    return cloud.names().index(name)


def test_stop_orders_safety_steps_before_deletion():
    cloud, tf = running_platform()
    execute(cloud, tf, "stop")
    assert position(cloud, "set_gate") < position(cloud, "stop_pipe") < position(cloud, "ops:create_snapshot")
    assert position(cloud, "ops:create_snapshot") < position(cloud, "put_json") < position(cloud, "tf_apply")
    assert cloud.domain_info is None and not cloud.nat and cloud.alb is None
    manifest = cloud.get_json(cloud.cfg.latest_manifest_key)[0]
    assert manifest["verified"] and manifest["snapshot"]["indices"][INDEX]["docs"] == 502
    assert StateStore(cloud).load()["verified_endpoint"] is None


def test_stop_is_idempotent():
    cloud, tf = stopped_platform()
    execute(cloud, tf, "stop")
    assert "tf_apply" not in cloud.names()
    assert "ops:create_snapshot" not in cloud.names()
    assert "put_json" not in cloud.names()


def test_interrupted_destroy_resumes_without_a_new_snapshot():
    cloud, tf = running_platform()
    tf.fail_next_apply = TerraformFailed("network failure")
    with pytest.raises(TerraformFailed):
        execute(cloud, tf, "stop")
    assert cloud.domain_info is not None
    first_id = StateStore(cloud).load()["operation_id"]
    cloud.calls.clear()
    ctx = execute(cloud, tf, "stop")
    assert ctx.operation_id == first_id
    assert "ops:create_snapshot" not in cloud.names()
    assert cloud.domain_info is None


def test_drain_timeout_preserves_everything():
    cloud, tf = running_platform()
    cloud.executions = ["arn:exec:1"]
    with pytest.raises(DrainTimeout):
        execute(cloud, tf, "stop", drain_timeout=60)
    assert cloud.domain_info is not None
    assert "tf_apply" not in cloud.names() and "stop_execution" not in cloud.names()
    assert cloud.pipe == "STOPPED" and cloud.gate is False


def test_requeue_restores_message_before_stopping_execution():
    cloud, tf = running_platform()
    body = json.dumps({"Records": []})
    cloud.executions = ["arn:exec:1"]
    cloud.inputs["arn:exec:1"] = [{"messageId": "m", "body": body}]
    cloud.tasks = ["task"]
    execute(cloud, tf, "stop", drain_timeout=60, requeue_on_timeout=True)
    assert cloud.messages == [body]
    assert position(cloud, "send_message") < position(cloud, "stop_execution")
    assert cloud.domain_info is None


def test_failed_snapshots_block_deletion():
    cloud, tf = running_platform()
    cloud.snapshot_outcomes = ["FAILED", "PARTIAL", "FAILED"]
    with pytest.raises(VerificationFailed):
        execute(cloud, tf, "stop")
    assert cloud.domain_info is not None and "tf_apply" not in cloud.names()
    assert cloud.get_json(cloud.cfg.latest_manifest_key) is None


def test_snapshot_retries_with_a_new_name_after_failure():
    cloud, tf = running_platform()
    cloud.snapshot_outcomes = ["FAILED", "SUCCESS"]
    execute(cloud, tf, "stop")
    assert cloud.get_json(cloud.cfg.latest_manifest_key)[0]["snapshot"]["name"].endswith("-r2")


def test_start_restores_snapshot_and_never_creates_empty_index():
    cloud, tf = stopped_platform()
    execute(cloud, tf, "start")
    names = cloud.names()
    assert "ops:restore_snapshot" in names and "ops:ensure_index" not in names
    assert cloud.indices[INDEX]["docs"] == 502
    assert position(cloud, "ops:restore_snapshot") < position(cloud, "scale_api")
    assert position(cloud, "ops:http_check") < names.index("set_gate") < names.index("start_pipe")
    assert cloud.gate and cloud.pipe == "RUNNING"
    assert StateStore(cloud).load()["verified_endpoint"] == ENDPOINT


def test_first_start_creates_index_and_requeues_unprocessed_documents():
    cloud = FakeCloud(make_config(), running=False)
    tf = FakeTerraform(cloud)
    cloud.raw = [{"bucket": "docs", "key": "raw/a/a.pdf", "etag": "e1", "doc_id": "a"}]
    execute(cloud, tf, "start")
    assert "ops:ensure_index" in cloud.names() and "ops:restore_snapshot" not in cloud.names()
    assert json.loads(cloud.messages[0])["document"]["doc_id"] == "a"


def test_new_index_reindexes_completed_documents():
    cloud = FakeCloud(make_config(), running=False)
    tf = FakeTerraform(cloud)
    cloud.raw = [{"bucket": "docs", "key": "raw/a/a.pdf", "etag": "e1", "doc_id": "a"}]
    cloud.doc_statuses[("a", "e1")] = "COMPLETED"
    execute(cloud, tf, "start")
    assert json.loads(cloud.messages[0])["document"]["force"] is True


def test_restore_does_not_requeue_completed_documents():
    cloud, tf = stopped_platform()
    cloud.raw = [{"bucket": "docs", "key": "raw/a/a.pdf", "etag": "e1", "doc_id": "a"}]
    cloud.doc_statuses[("a", "e1")] = "COMPLETED"
    execute(cloud, tf, "start")
    assert cloud.messages == []


def test_invalid_snapshot_stops_before_any_billed_resource():
    cloud, tf = stopped_platform()
    manifest = cloud.get_json(cloud.cfg.latest_manifest_key)[0]
    del cloud.objects[cloud.cfg.snapshot_file_key(manifest["snapshot"]["uuid"])]
    with pytest.raises(SnapshotInvalid):
        execute(cloud, tf, "start")
    assert "tf_apply" not in cloud.names() and cloud.domain_info is None


def test_corrupt_manifest_is_rejected():
    cloud, tf = stopped_platform()
    key = cloud.cfg.latest_manifest_key
    body, digest = cloud.objects[key]
    cloud.objects[key] = (body.replace(b"502", b"503"), digest)
    with pytest.raises(SnapshotInvalid):
        execute(cloud, tf, "start")


def test_ignore_snapshot_rebuilds_explicitly():
    cloud, tf = stopped_platform()
    manifest = cloud.get_json(cloud.cfg.latest_manifest_key)[0]
    del cloud.objects[cloud.cfg.snapshot_file_key(manifest["snapshot"]["uuid"])]
    execute(cloud, tf, "start", ignore_snapshot=True)
    assert "ops:ensure_index" in cloud.names() and "ops:restore_snapshot" not in cloud.names()


def test_partial_restore_is_replaced_on_retry():
    cloud, tf = stopped_platform()
    cloud.restore_docs_override = 10
    execute(cloud, tf, "start")
    assert cloud.names().count("ops:restore_snapshot") == 2
    assert "ops:delete_partial_index" in cloud.names()
    assert cloud.indices[INDEX]["docs"] == 502


def test_stop_after_failed_start_keeps_the_good_backup():
    cloud, tf = stopped_platform()
    original = cloud.get_json(cloud.cfg.latest_manifest_key)[0]
    cloud.fail_ops["restore_snapshot"] = RuntimeError("restore interrupted")
    with pytest.raises(RuntimeError):
        execute(cloud, tf, "start")
    assert cloud.domain_info is not None and not cloud.indices
    cloud.calls.clear()
    execute(cloud, tf, "stop")
    assert "ops:create_snapshot" not in cloud.names()
    assert cloud.get_json(cloud.cfg.latest_manifest_key)[0] == original
    assert cloud.domain_info is None


def test_partial_restore_is_never_snapshotted_over_the_good_backup():
    cloud, tf = stopped_platform()
    original = cloud.get_json(cloud.cfg.latest_manifest_key)[0]
    cloud.fail_ops["verify_index"] = RuntimeError("start interrupted")
    with pytest.raises(RuntimeError):
        execute(cloud, tf, "start")
    cloud.indices[INDEX]["docs"] = 10
    cloud.calls.clear()
    execute(cloud, tf, "stop")
    assert "ops:create_snapshot" not in cloud.names()
    assert cloud.get_json(cloud.cfg.latest_manifest_key)[0] == original


def test_destroy_refuses_a_live_domain_without_a_saved_manifest():
    cloud, tf = running_platform()
    ctx = make_context(cloud, tf)
    prepare_context(ctx, StateStore(cloud), "stop")
    with pytest.raises(VerificationFailed):
        destroy_runtime(ctx)
    assert cloud.domain_info is not None and "tf_apply" not in cloud.names()


def test_interrupted_start_resumes_and_finishes():
    cloud, tf = stopped_platform()
    cloud.fail_ops["http_check"] = RuntimeError("API not reachable")
    with pytest.raises(RuntimeError):
        execute(cloud, tf, "start")
    assert cloud.gate is False and cloud.pipe == "STOPPED"
    execute(cloud, tf, "start")
    assert cloud.gate and cloud.pipe == "RUNNING"
    assert cloud.names().count("ops:restore_snapshot") == 1


def test_start_on_running_platform_changes_nothing():
    cloud, tf = running_platform()
    cloud.indices[INDEX]["docs"] = 900  # Newer than any snapshot.
    execute(cloud, tf, "start")
    assert not {"ops:restore_snapshot", "ops:ensure_index", "tf_apply", "scale_api"} & set(cloud.names())
    assert cloud.indices[INDEX]["docs"] == 900


@pytest.mark.parametrize("operation,steps", [("stop", stop_steps), ("start", start_steps)])
def test_dry_run_makes_no_changes(operation, steps):
    cloud, tf = running_platform() if operation == "stop" else stopped_platform()
    before = (cloud.gate, cloud.pipe, cloud.domain_info, dict(cloud.items), dict(cloud.objects))
    ctx = make_context(cloud, tf, dry_run=True)
    prepare_context(ctx, StateStore(cloud), operation)
    plan = dry_run(ctx, steps())
    assert plan and not [c for c in cloud.calls if not c[0].startswith("ops:") or c[0] in ("ops:create_snapshot", "ops:restore_snapshot")]
    assert (cloud.gate, cloud.pipe, cloud.domain_info, dict(cloud.items), dict(cloud.objects)) == before


def test_plan_guard_rejects_foundation_resources():
    with pytest.raises(UnsafePlan):
        assert_plan_safe([Change("aws_s3_bucket.documents", "aws_s3_bucket", ["delete"])], destroying=True)


def test_plan_guard_rejects_domain_replacement_outside_stop():
    change = Change("aws_opensearch_domain.main", "aws_opensearch_domain", ["delete", "create"])
    with pytest.raises(UnsafePlan):
        assert_plan_safe([change], destroying=False)
    assert_plan_safe([change], destroying=True)
