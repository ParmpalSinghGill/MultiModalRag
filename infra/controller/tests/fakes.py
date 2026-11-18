"""In-memory stand-ins for AWS and Terraform used by the workflow tests."""

import copy
import hashlib
import json
from types import SimpleNamespace

from ragctl.config import Config
from ragctl.engine import Context, Options
from ragctl.gateway import canonical_json
from ragctl.terraform import Plan, Change

INDEX = "multimodal_rag_finance"
ENDPOINT = "vpc-mfrag-prod-search-aaa.us-east-1.es.amazonaws.com"


def make_config(enable_redis=False):
    return Config(
        prefix="mfrag-prod", region="us-east-1", state_bucket="state", image_tag="v1",
        enable_redis=enable_redis, documents_bucket="docs", backup_bucket="backups",
        documents_table="documents", control_table="control", queue_url="queue", dlq_url="dlq",
        pipe_name="pipe", state_machine_arn="sm", ecs_cluster="cluster",
        worker_families=["parse", "index"], api_service="api", ops_function="ops",
        domain_name="search", snapshot_repository="repo", snapshot_base_path="opensearch-snapshots/mfrag-prod",
        manifest_prefix="infrastructure-state/mfrag-prod", opensearch_index=INDEX,
        document_suffixes=[".pdf"], ecr_repositories={"api": "api", "worker": "worker"},
        api_endpoint="https://example.invalid",
    )


class FakeCloud:
    def __init__(self, cfg, running=True):
        self.cfg = cfg
        self.calls = []
        self.items = {}
        self.objects = {}
        self.pipe = "RUNNING" if running else "STOPPED"
        self.gate = running
        self.executions = []
        self.inputs = {}
        self.tasks = []
        self.messages = []
        self.raw = []
        self.doc_statuses = {}
        self.domain_info = None
        self.indices = {}
        self.snapshots = {}
        self.snapshot_outcomes = []
        self.restore_docs_override = None
        self.service = None
        self.nat = []
        self.alb = None
        self.redis = None
        self.images = True
        self.fail_ops = {}
        if running:
            self.create_runtime()
            self.service["desired"] = self.service["running"] = 1
            self.indices = {INDEX: {"docs": 502, "health": "green", "status": "open"}}

    def record(self, name, *details):
        self.calls.append((name, *details))

    def names(self):
        return [c[0] for c in self.calls]

    def create_runtime(self, endpoint=ENDPOINT):
        self.domain_info = {
            "name": "search", "endpoint": endpoint, "engine_version": "OpenSearch_2.19",
            "processing": False, "deleted": False, "instance_type": "t3.small.search",
            "instance_count": 1, "volume_gb": 20,
        }
        self.service = {"desired": 0, "running": 0, "pending": 0, "target_group_arn": "tg", "steady": True}
        self.nat = ["nat-1"]
        self.alb = "internal.elb.amazonaws.com"
        self.indices = {}
        if self.cfg.enable_redis:
            self.redis = "available"

    def destroy_runtime(self):
        self.domain_info = None
        self.service = None
        self.nat = []
        self.alb = None
        self.redis = None
        self.indices = {}

    # control table
    def get_control(self, pk, sk):
        return copy.deepcopy(self.items.get((pk, sk)))

    def put_control(self, item):
        self.items[(item["pk"], item["sk"])] = copy.deepcopy(item)

    def gate_open(self):
        return self.gate

    def set_gate(self, accepting):
        self.record("set_gate", accepting)
        self.gate = accepting

    # ingestion
    def pipe_state(self):
        return self.pipe

    def stop_pipe(self):
        self.record("stop_pipe")
        self.pipe = "STOPPED"

    def start_pipe(self):
        self.record("start_pipe")
        self.pipe = "RUNNING"

    def running_executions(self):
        return list(self.executions)

    def execution_input(self, arn):
        return self.inputs[arn]

    def stop_execution(self, arn, cause):
        self.record("stop_execution", arn)
        self.executions.remove(arn)
        self.tasks = []

    def worker_tasks(self):
        return list(self.tasks)

    def queue_depth(self, url=None):
        return {"visible": len(self.messages), "in_flight": 0}

    def send_message(self, body):
        self.record("send_message", body)
        self.messages.append(body)

    def raw_objects(self):
        return iter(self.raw)

    def document_status(self, doc_id, etag):
        return self.doc_statuses.get((doc_id, etag))

    # discovery
    def domain(self):
        return copy.deepcopy(self.domain_info)

    def api_service(self):
        return copy.deepcopy(self.service)

    def scale_api(self, count):
        self.record("scale_api", count)
        self.service["desired"] = self.service["running"] = count

    def healthy_targets(self, _):
        return self.service["running"] if self.service else 0

    def nat_gateways(self):
        return list(self.nat)

    def load_balancer(self):
        return self.alb

    def redis_status(self):
        return self.redis

    def image_exists(self, _):
        return self.images

    def opensearch_service_role_exists(self):
        return True

    # S3
    def object_exists(self, key):
        return key in self.objects

    def put_json(self, key, value):
        self.record("put_json", key)
        body = canonical_json(value)
        digest = hashlib.sha256(body).hexdigest()
        self.objects[key] = (body, digest)
        return digest

    def get_json(self, key):
        if key not in self.objects:
            return None
        body, digest = self.objects[key]
        return json.loads(body), hashlib.sha256(body).hexdigest() == digest

    # in-VPC operations
    def ops(self, action, **kw):
        self.record(f"ops:{action}", kw)
        if action in self.fail_ops:
            raise self.fail_ops.pop(action)
        return getattr(self, f"_op_{action}")(**kw)

    def _op_cluster_health(self, endpoint):
        return {"status": "green"}

    def _op_list_indices(self, endpoint):
        return {"indices": copy.deepcopy(self.indices)}

    def _op_register_repository(self, endpoint):
        return {}

    def _op_verify_repository(self, endpoint):
        return {"nodes": 1}

    def _op_get_snapshot(self, endpoint, snapshot):
        return {"snapshot": copy.deepcopy(self.snapshots.get(snapshot))}

    def _op_create_snapshot(self, endpoint, snapshot, indices):
        state = self.snapshot_outcomes.pop(0) if self.snapshot_outcomes else "SUCCESS"
        uuid = f"uuid-{snapshot}"
        self.snapshots[snapshot] = {
            "name": snapshot, "uuid": uuid, "state": state, "indices": indices,
            "version": "2.19.0", "shards": {"failed": 0}, "failures": [],
        }
        if state == "SUCCESS":
            self.objects[self.cfg.snapshot_file_key(uuid)] = (b"", "")
        return {"snapshot": snapshot}

    def _op_restore_snapshot(self, endpoint, snapshot, indices):
        stored = self.get_json(self.cfg.latest_manifest_key)[0]["snapshot"]["indices"]
        for name in indices:
            docs = stored[name]["docs"]
            if self.restore_docs_override is not None:
                docs, self.restore_docs_override = self.restore_docs_override, None
            self.indices[name] = {"docs": docs, "health": "green", "status": "open"}
        return {"snapshot": snapshot}

    def _op_recovery_active(self, endpoint):
        return {"active": []}

    def _op_delete_partial_index(self, endpoint, index):
        self.indices.pop(index, None)
        return {"deleted": index}

    def _op_ensure_index(self, endpoint):
        self.indices.setdefault(INDEX, {"docs": 0, "health": "green", "status": "open"})
        return {"created": True}

    def _op_verify_index(self, endpoint):
        return {"ok": INDEX in self.indices, "reason": None}

    def _op_http_check(self, host):
        return {"status": 200, "body": "{}"}


class FakeTerraform:
    def __init__(self, cloud):
        self.cloud = cloud
        self.present = cloud.domain_info is not None
        self.fail_next_apply = None

    def state_resources(self):
        return ["aws_opensearch_domain.main"] if self.present else []

    def outputs(self):
        return {"alb_dns_name": self.cloud.alb, "api_desired_count": 1} if self.present else {}

    def plan(self, destroy=False, keep=True):
        needed = self.present if destroy else not self.present
        changes = [Change("aws_opensearch_domain.main", "aws_opensearch_domain", ["delete" if destroy else "create"])] if needed else []
        plan = Plan("plan" if keep and changes else None, changes)
        plan.destroying = destroy
        return plan

    def apply(self, plan):
        self.cloud.record("tf_apply", "destroy" if plan.destroying else "create")
        if self.fail_next_apply:
            error, self.fail_next_apply = self.fail_next_apply, None
            raise error
        if plan.destroying:
            self.cloud.destroy_runtime()
            self.present = False
        else:
            self.cloud.create_runtime()
            self.present = True


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_context(cloud, tf, **options):
    clock = FakeClock()
    return Context(
        cfg=cloud.cfg, aws=cloud, tf=tf, options=Options(**options),
        sleep=clock.sleep, clock=clock.time,
    )
