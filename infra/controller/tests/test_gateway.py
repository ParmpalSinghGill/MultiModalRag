"""Checks controller and Lambda code against the real AWS API models, offline."""

import importlib.util
import io
import json
import re
from pathlib import Path

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from fakes import make_config
from ragctl.gateway import AwsGateway
from ragctl.terraform import RUNTIME_RESOURCE_TYPES

INFRA = Path(__file__).resolve().parents[2]
ENDPOINT = "vpc-x.us-east-1.es.amazonaws.com"


@pytest.fixture
def gateway():
    session = boto3.Session(aws_access_key_id="t", aws_secret_access_key="t", region_name="us-east-1")
    return AwsGateway(session, make_config())


def stub(client, method, response, params=ANY):
    stubber = Stubber(client)
    expected = None if params is ANY else params
    stubber.add_response(method, response, expected)
    stubber.activate()
    return stubber


def test_api_calls_match_service_models(gateway):
    cfg = gateway.cfg
    with stub(gateway.pipes, "describe_pipe", {"CurrentState": "STOPPED"}, {"Name": cfg.pipe_name}):
        assert gateway.pipe_state() == "STOPPED"
    with stub(gateway.sfn, "list_executions", {"executions": []}):
        assert gateway.running_executions() == []
    stubber = Stubber(gateway.ecs)
    for _ in cfg.worker_families:
        stubber.add_response("list_tasks", {"taskArns": ["t"]})
    with stubber:
        assert gateway.worker_tasks() == ["t", "t"]
    with stub(gateway.ecs, "describe_services", {"services": []}):
        assert gateway.api_service() is None
    with stub(gateway.ecs, "update_service", {}, {"cluster": cfg.ecs_cluster, "service": cfg.api_service, "desiredCount": 0}):
        gateway.scale_api(0)
    with stub(gateway.ec2, "describe_nat_gateways", {"NatGateways": [{"NatGatewayId": "nat-1"}]}):
        assert gateway.nat_gateways() == ["nat-1"]
    with stub(gateway.elb, "describe_target_health", {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy"}}]}):
        assert gateway.healthy_targets("arn") == 1
    with stub(gateway.sqs, "get_queue_attributes", {"Attributes": {
        "ApproximateNumberOfMessages": "3", "ApproximateNumberOfMessagesNotVisible": "1"}}):
        assert gateway.queue_depth() == {"visible": 3, "in_flight": 1}
    with stub(gateway.ecr, "describe_images", {"imageDetails": []}):
        assert gateway.image_exists("api")


def test_raw_listing_filters_and_normalizes(gateway):
    objects = [
        {"Key": "raw/a/a.pdf", "ETag": '"e1"', "Size": 5},
        {"Key": "raw/metadata/q.pdf", "ETag": '"e2"', "Size": 5},
        {"Key": "raw/b/b.json", "ETag": '"e3"', "Size": 5},
        {"Key": "raw/c/empty.pdf", "ETag": '"e4"', "Size": 0},
    ]
    with stub(gateway.s3, "list_objects_v2", {"Contents": objects, "IsTruncated": False}):
        assert [o["doc_id"] for o in gateway.raw_objects()] == ["a"]


def test_missing_resources_are_reported_as_absent(gateway):
    cases = [
        (gateway.opensearch, "describe_domain", "ResourceNotFoundException", gateway.domain),
        (gateway.elb, "describe_load_balancers", "LoadBalancerNotFound", gateway.load_balancer),
        (gateway.elasticache, "describe_replication_groups", "ReplicationGroupNotFoundFault", gateway.redis_status),
    ]
    for client, method, code, call in cases:
        with Stubber(client) as stubber:
            stubber.add_client_error(method, code)
            assert call() is None


def test_manifest_checksum_detects_tampering(gateway):
    body = b'{"a":1}'
    for metadata, valid in (({"content-sha256": "wrong"}, False),):
        with stub(gateway.s3, "get_object", {"Body": StreamingBody(io.BytesIO(body), len(body)), "Metadata": metadata}):
            assert gateway.get_json("k") == ({"a": 1}, valid)


def test_gateway_never_manages_resource_lifecycles():
    source = (INFRA / "controller/ragctl/gateway.py").read_text()
    calls = re.findall(r"self\.\w+\.((?:create|delete|terminate|release|put_bucket)\w*)\(", source)
    assert set(calls) <= {"delete_item"}


def test_every_runtime_resource_is_allowed_by_the_plan_guard():
    source = (INFRA / "terraform/runtime/main.tf").read_text()
    assert set(re.findall(r'^resource "(\w+)"', source, re.M)) <= RUNTIME_RESOURCE_TYPES


@pytest.fixture
def ops(monkeypatch):
    for name, value in {
        "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "t",
        "AWS_SECRET_ACCESS_KEY": "t", "SNAPSHOT_REPOSITORY": "repo", "BACKUP_BUCKET": "backups",
        "SNAPSHOT_BASE_PATH": "base", "SNAPSHOT_ROLE_ARN": "arn:role", "OPENSEARCH_INDEX": "idx",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.syspath_prepend(str(INFRA.parent / "data_ingestion"))
    spec = importlib.util.spec_from_file_location("ops_handler", INFRA / "lambda/opensearch_ops/handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Http:
        def __init__(self):
            self.requests = []
            self.responses = []

        def request(self, method, url, body=None, headers=None, **_):
            self.requests.append((method, url, json.loads(body) if body else None, headers))
            status, data = self.responses.pop(0)
            return type("R", (), {"status": status, "data": json.dumps(data).encode()})

    module.http = Http()
    return module


def test_ops_signs_requests_and_never_restores_global_state(ops):
    ops.http.responses = [(200, {"accepted": True})]
    ops.handler({"action": "restore_snapshot", "endpoint": ENDPOINT, "snapshot": "s1", "indices": ["idx"]}, None)
    method, url, body, headers = ops.http.requests[0]
    assert (method, url) == ("POST", f"https://{ENDPOINT}/_snapshot/repo/s1/_restore")
    assert body["include_global_state"] is False and "Authorization" in headers


def test_ops_creates_the_shared_index_definition_only_when_missing(ops):
    ops.http.responses = [(404, {}), (200, {})]
    assert ops.handler({"action": "ensure_index", "endpoint": ENDPOINT}, None) == {"created": True}
    assert ops.http.requests[1][2]["mappings"]["properties"]["embedding"]["dimension"] == 384
    ops.http.responses = [(200, {"idx": {}})]
    assert ops.handler({"action": "ensure_index", "endpoint": ENDPOINT}, None) == {"created": False}


def test_ops_rejects_unexpected_targets_and_has_no_snapshot_delete(ops):
    with pytest.raises(ValueError):
        ops.call("attacker.example.com", "GET", "_cluster/health")
    with pytest.raises(ValueError):
        ops.handler({"action": "http_check", "host": "169.254.169.254"}, None)
    assert not [name for name in ops.ACTIONS if "delete" in name and "snapshot" in name]
