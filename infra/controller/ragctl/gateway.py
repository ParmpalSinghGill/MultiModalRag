"""
All AWS API calls made by the controller.

Boto3 here reads state and flips operational switches (pipe state, ECS desired
count, the ingestion gate). It never creates or deletes infrastructure: resource
lifecycles belong to Terraform.
"""

import hashlib
import json
import re
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from boto3.dynamodb.conditions import Attr
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from ragctl.config import Config
from ragctl.errors import LockHeld, RagctlError

RETRIES = BotoConfig(retries={"mode": "standard", "total_max_attempts": 8})
LAMBDA_CONFIG = BotoConfig(retries={"mode": "standard", "total_max_attempts": 3}, read_timeout=70)
NOT_FOUND_CODES = ("404", "NoSuchKey", "NotFound")
ACTIVE_NAT_STATES = ["pending", "available", "deleting"]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def derive_doc_id(key: str, suffixes: Tuple[str, ...]) -> Optional[str]:
    """Must match derive_doc_id in the pipeline Lambda."""
    if not key.startswith("raw/") or not key.lower().endswith(suffixes):
        return None
    parts = key[len("raw/"):].split("/")
    if parts[0] == "metadata":
        return None
    candidate = parts[0] if len(parts) > 1 else parts[0].rsplit(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9._-]", "_", candidate)[:128] or None


class AwsGateway:
    def __init__(self, session, config: Config):
        self.cfg = config
        client = lambda name, cfg=RETRIES: session.client(name, region_name=config.region, config=cfg)
        self.s3 = client("s3")
        self.sqs = client("sqs")
        self.pipes = client("pipes")
        self.sfn = client("stepfunctions")
        self.ecs = client("ecs")
        self.ecr = client("ecr")
        self.iam = client("iam")
        self.ec2 = client("ec2")
        self.elb = client("elbv2")
        self.opensearch = client("opensearch")
        self.elasticache = client("elasticache")
        self.lambda_client = client("lambda", LAMBDA_CONFIG)
        dynamodb = session.resource("dynamodb", region_name=config.region, config=RETRIES)
        self.control = dynamodb.Table(config.control_table)
        self.documents = dynamodb.Table(config.documents_table)
        self._conditional = dynamodb.meta.client.exceptions.ConditionalCheckFailedException

    # ------------------------------ Control table ---------------------------
    def get_control(self, pk: str, sk: str) -> Optional[Dict[str, Any]]:
        return self.control.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")

    def put_control(self, item: Dict[str, Any]) -> None:
        self.control.put_item(Item=item)

    def gate_open(self) -> bool:
        item = self.get_control("INGESTION", "GATE")
        return bool(item and item.get("accepting"))

    def set_gate(self, accepting: bool) -> None:
        self.put_control({"pk": "INGESTION", "sk": "GATE", "accepting": accepting, "updated_at": int(time.time())})

    def acquire_lock(self, owner: str, operation: str, ttl: int) -> None:
        now = int(time.time())
        try:
            self.control.put_item(
                Item={"pk": "CONTROLLER", "sk": "LOCK", "owner": owner, "operation": operation, "expires_at": now + ttl},
                ConditionExpression=Attr("pk").not_exists() | Attr("expires_at").lt(now) | Attr("owner").eq(owner),
            )
        except self._conditional:
            holder = self.get_control("CONTROLLER", "LOCK") or {}
            raise LockHeld(
                f"Another controller holds the lock: {holder.get('owner')} running "
                f"{holder.get('operation')}. It expires by itself if that process died."
            )

    def renew_lock(self, owner: str, ttl: int) -> bool:
        try:
            self.control.update_item(
                Key={"pk": "CONTROLLER", "sk": "LOCK"},
                UpdateExpression="SET expires_at = :expires",
                ExpressionAttributeValues={":expires": int(time.time()) + ttl},
                ConditionExpression=Attr("owner").eq(owner),
            )
            return True
        except self._conditional:
            return False

    def release_lock(self, owner: str) -> None:
        try:
            self.control.delete_item(
                Key={"pk": "CONTROLLER", "sk": "LOCK"}, ConditionExpression=Attr("owner").eq(owner)
            )
        except self._conditional:
            pass  # The lock expired and now belongs to someone else.

    # -------------------------------- Ingestion ------------------------------
    def pipe_state(self) -> str:
        return self.pipes.describe_pipe(Name=self.cfg.pipe_name)["CurrentState"]

    def stop_pipe(self) -> None:
        self.pipes.stop_pipe(Name=self.cfg.pipe_name)

    def start_pipe(self) -> None:
        self.pipes.start_pipe(Name=self.cfg.pipe_name)

    def running_executions(self) -> List[str]:
        paginator = self.sfn.get_paginator("list_executions")
        pages = paginator.paginate(stateMachineArn=self.cfg.state_machine_arn, statusFilter="RUNNING")
        return list(pages.search("executions[].executionArn"))

    def execution_input(self, arn: str) -> Any:
        return json.loads(self.sfn.describe_execution(executionArn=arn).get("input") or "null")

    def stop_execution(self, arn: str, cause: str) -> None:
        try:
            self.sfn.stop_execution(executionArn=arn, error="ragctl.Requeued", cause=cause)
        except self.sfn.exceptions.ExecutionDoesNotExist:
            pass

    def worker_tasks(self) -> List[str]:
        tasks: List[str] = []
        paginator = self.ecs.get_paginator("list_tasks")
        for family in self.cfg.worker_families:
            pages = paginator.paginate(cluster=self.cfg.ecs_cluster, family=family, desiredStatus="RUNNING")
            tasks.extend(pages.search("taskArns[]"))
        return tasks

    def queue_depth(self, url: Optional[str] = None) -> Dict[str, int]:
        attributes = self.sqs.get_queue_attributes(
            QueueUrl=url or self.cfg.queue_url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return {
            "visible": int(attributes["ApproximateNumberOfMessages"]),
            "in_flight": int(attributes["ApproximateNumberOfMessagesNotVisible"]),
        }

    def send_message(self, body: str) -> None:
        self.sqs.send_message(QueueUrl=self.cfg.queue_url, MessageBody=body)

    def raw_objects(self) -> Iterator[Dict[str, str]]:
        suffixes = tuple(s.lower() for s in self.cfg.document_suffixes)
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.cfg.documents_bucket, Prefix="raw/"):
            for item in page.get("Contents", []):
                doc_id = derive_doc_id(item["Key"], suffixes)
                if doc_id and item["Size"] > 0:
                    yield {
                        "bucket": self.cfg.documents_bucket,
                        "key": item["Key"],
                        "etag": item["ETag"].strip('"'),
                        "doc_id": doc_id,
                    }

    def document_status(self, doc_id: str, etag: str) -> Optional[str]:
        item = self.documents.get_item(
            Key={"pk": f"DOC#{doc_id}", "sk": f"VER#{etag}"}, ConsistentRead=True
        ).get("Item")
        return item.get("status") if item else None

    # ------------------------------ Runtime discovery -----------------------
    def domain(self) -> Optional[Dict[str, Any]]:
        try:
            status = self.opensearch.describe_domain(DomainName=self.cfg.domain_name)["DomainStatus"]
        except self.opensearch.exceptions.ResourceNotFoundException:
            return None
        return {
            "name": status["DomainName"],
            "endpoint": (status.get("Endpoints") or {}).get("vpc"),
            "engine_version": status.get("EngineVersion"),
            "processing": status.get("Processing", False),
            "deleted": status.get("Deleted", False),
            "instance_type": status.get("ClusterConfig", {}).get("InstanceType"),
            "instance_count": status.get("ClusterConfig", {}).get("InstanceCount"),
            "volume_gb": status.get("EBSOptions", {}).get("VolumeSize"),
        }

    def api_service(self) -> Optional[Dict[str, Any]]:
        services = self.ecs.describe_services(
            cluster=self.cfg.ecs_cluster, services=[self.cfg.api_service]
        )["services"]
        active = [s for s in services if s["status"] == "ACTIVE"]
        if not active:
            return None
        service = active[0]
        balancers = service.get("loadBalancers", [])
        return {
            "desired": service["desiredCount"],
            "running": service["runningCount"],
            "pending": service["pendingCount"],
            "target_group_arn": balancers[0]["targetGroupArn"] if balancers else None,
            "steady": len(service.get("deployments", [])) == 1,
        }

    def scale_api(self, count: int) -> None:
        self.ecs.update_service(
            cluster=self.cfg.ecs_cluster, service=self.cfg.api_service, desiredCount=count
        )

    def healthy_targets(self, target_group_arn: str) -> int:
        descriptions = self.elb.describe_target_health(TargetGroupArn=target_group_arn)["TargetHealthDescriptions"]
        return sum(1 for d in descriptions if d["TargetHealth"]["State"] == "healthy")

    def nat_gateways(self) -> List[str]:
        paginator = self.ec2.get_paginator("describe_nat_gateways")
        project, environment = self.cfg.prefix.rsplit("-", 1)
        pages = paginator.paginate(Filters=[
            {"Name": "tag:Project", "Values": [project]},
            {"Name": "tag:Environment", "Values": [environment]},
            {"Name": "state", "Values": ACTIVE_NAT_STATES},
        ])
        return list(pages.search("NatGateways[].NatGatewayId"))

    def load_balancer(self) -> Optional[str]:
        try:
            found = self.elb.describe_load_balancers(Names=[f"{self.cfg.prefix}-api"])["LoadBalancers"]
        except self.elb.exceptions.LoadBalancerNotFoundException:
            return None
        return found[0]["DNSName"] if found else None

    def redis_status(self) -> Optional[str]:
        try:
            groups = self.elasticache.describe_replication_groups(
                ReplicationGroupId=f"{self.cfg.prefix}-redis"
            )["ReplicationGroups"]
        except self.elasticache.exceptions.ReplicationGroupNotFoundFault:
            return None
        return groups[0]["Status"] if groups else None

    # -------------------------------- Preflight ------------------------------
    def image_exists(self, component: str) -> bool:
        try:
            self.ecr.describe_images(
                repositoryName=self.cfg.ecr_repositories[component],
                imageIds=[{"imageTag": self.cfg.image_tag}],
            )
            return True
        except self.ecr.exceptions.ImageNotFoundException:
            return False

    def opensearch_service_role_exists(self) -> bool:
        try:
            self.iam.get_role(RoleName="AWSServiceRoleForAmazonOpenSearchService")
            return True
        except self.iam.exceptions.NoSuchEntityException:
            return False

    # ----------------------------- In-VPC operations ------------------------
    def ops(self, action: str, **arguments: Any) -> Dict[str, Any]:
        response = self.lambda_client.invoke(
            FunctionName=self.cfg.ops_function,
            Payload=json.dumps({"action": action, **arguments}).encode("utf-8"),
        )
        payload = json.loads(response["Payload"].read())
        if response.get("FunctionError"):
            raise RagctlError(f"Operation {action} failed: {payload.get('errorMessage', payload)}")
        return payload

    # ------------------------------ Backup bucket ---------------------------
    def object_exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.cfg.backup_bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in NOT_FOUND_CODES:
                return False
            raise

    def put_json(self, key: str, value: Dict[str, Any]) -> str:
        body = canonical_json(value)
        digest = hashlib.sha256(body).hexdigest()
        self.s3.put_object(
            Bucket=self.cfg.backup_bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            Metadata={"content-sha256": digest},
        )
        return digest

    def get_json(self, key: str) -> Optional[Tuple[Dict[str, Any], bool]]:
        """Returns (document, checksum_valid), or None when the object is absent."""
        try:
            response = self.s3.get_object(Bucket=self.cfg.backup_bucket, Key=key)
        except self.s3.exceptions.NoSuchKey:
            return None
        with response["Body"] as stream:
            body = stream.read()
        expected = response.get("Metadata", {}).get("content-sha256")
        return json.loads(body), hashlib.sha256(body).hexdigest() == expected
