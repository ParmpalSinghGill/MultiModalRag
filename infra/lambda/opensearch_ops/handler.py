"""
In-VPC data-plane operations for the controller.

OpenSearch and the internal ALB are unreachable from an operator workstation, so
the controller invokes this function. It has no third-party dependencies and
deliberately offers no way to delete a snapshot.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional

import boto3
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import get_cert_path

from opensearch_schema import build_index_body

REGION = os.environ["AWS_REGION"]
REPOSITORY = os.environ["SNAPSHOT_REPOSITORY"]
BACKUP_BUCKET = os.environ["BACKUP_BUCKET"]
SNAPSHOT_BASE_PATH = os.environ["SNAPSHOT_BASE_PATH"]
SNAPSHOT_ROLE_ARN = os.environ["SNAPSHOT_ROLE_ARN"]
REQUIRED_INDEX = os.environ["OPENSEARCH_INDEX"]
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "384"))

ENDPOINT_PATTERN = re.compile(r"^[a-z0-9.-]+\.es\.amazonaws\.com$")
ALB_PATTERN = re.compile(r"^[A-Za-z0-9.-]+\.elb\.amazonaws\.com$")
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

http = urllib3.PoolManager(
    cert_reqs="CERT_REQUIRED",
    ca_certs=get_cert_path(True),
    timeout=urllib3.Timeout(connect=5, read=50),
    retries=False,
)
session = boto3.Session()


class OpenSearchError(Exception):
    pass


def call(endpoint: str, method: str, path: str, body: Optional[Dict] = None,
         allow_404: bool = False) -> Optional[Any]:
    if not ENDPOINT_PATTERN.match(endpoint):
        raise ValueError("Invalid OpenSearch endpoint")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = AWSRequest(method=method, url=f"https://{endpoint}/{path}", data=data, headers=headers)
    SigV4Auth(session.get_credentials().get_frozen_credentials(), "es", REGION).add_auth(request)
    response = http.request(method, request.url, body=data, headers=dict(request.headers))
    if response.status == 404 and allow_404:
        return None
    if response.status >= 300:
        raise OpenSearchError(f"{method} {path} returned {response.status}: {response.data[:500]!r}")
    return json.loads(response.data) if response.data else {}


def safe_name(value: str) -> str:
    if not NAME_PATTERN.match(value):
        raise ValueError(f"Invalid name: {value}")
    return value


def cluster_health(e: Dict) -> Dict:
    health = call(e["endpoint"], "GET", "_cluster/health")
    return {key: health[key] for key in ("status", "number_of_nodes", "active_shards_percent_as_number")}


def list_indices(e: Dict) -> Dict:
    call(e["endpoint"], "POST", "_refresh")
    rows = call(e["endpoint"], "GET", "_cat/indices?format=json&h=index,health,status,docs.count,uuid")
    return {"indices": {
        row["index"]: {
            "docs": int(row.get("docs.count") or 0),
            "health": row.get("health"),
            "status": row.get("status"),
        }
        for row in rows if not row["index"].startswith(".")
    }}


def register_repository(e: Dict) -> Dict:
    call(e["endpoint"], "PUT", f"_snapshot/{REPOSITORY}", {
        "type": "s3",
        "settings": {
            "bucket": BACKUP_BUCKET,
            "base_path": SNAPSHOT_BASE_PATH,
            "region": REGION,
            "role_arn": SNAPSHOT_ROLE_ARN,
        },
    })
    return {"repository": REPOSITORY}


def verify_repository(e: Dict) -> Dict:
    result = call(e["endpoint"], "POST", f"_snapshot/{REPOSITORY}/_verify")
    return {"nodes": len(result.get("nodes", {}))}


def get_snapshot(e: Dict) -> Dict:
    name = safe_name(e["snapshot"])
    result = call(e["endpoint"], "GET", f"_snapshot/{REPOSITORY}/{name}", allow_404=True)
    snapshots = (result or {}).get("snapshots", [])
    if not snapshots:
        return {"snapshot": None}
    item = snapshots[0]
    return {"snapshot": {
        "name": item["snapshot"],
        "uuid": item["uuid"],
        "state": item["state"],
        "indices": item.get("indices", []),
        "version": item.get("version"),
        "shards": item.get("shards", {}),
        "failures": item.get("failures", []),
    }}


def create_snapshot(e: Dict) -> Dict:
    name = safe_name(e["snapshot"])
    indices: List[str] = [safe_name(i) for i in e["indices"]]
    call(e["endpoint"], "PUT", f"_snapshot/{REPOSITORY}/{name}", {
        "indices": ",".join(indices),
        "include_global_state": False,
        "ignore_unavailable": False,
        "partial": False,
    })
    return {"snapshot": name}


def restore_snapshot(e: Dict) -> Dict:
    name = safe_name(e["snapshot"])
    indices = [safe_name(i) for i in e["indices"]]
    call(e["endpoint"], "POST", f"_snapshot/{REPOSITORY}/{name}/_restore", {
        "indices": ",".join(indices),
        "include_global_state": False,
        "include_aliases": True,
    })
    return {"snapshot": name}


def recovery_active(e: Dict) -> Dict:
    result = call(e["endpoint"], "GET", "_recovery?active_only=true")
    return {"active": sorted(i for i, v in result.items() if v.get("shards") and not i.startswith("."))}


def delete_partial_index(e: Dict) -> Dict:
    """Removes a failed partial restore. The snapshot remains the source of truth."""
    name = safe_name(e["index"])
    call(e["endpoint"], "DELETE", name, allow_404=True)
    return {"deleted": name}


def ensure_index(e: Dict) -> Dict:
    exists = call(e["endpoint"], "GET", REQUIRED_INDEX, allow_404=True)
    if exists is not None:
        return {"created": False}
    call(e["endpoint"], "PUT", REQUIRED_INDEX, build_index_body(EMBEDDING_DIMENSION))
    return {"created": True}


def verify_index(e: Dict) -> Dict:
    data = call(e["endpoint"], "GET", REQUIRED_INDEX, allow_404=True)
    if data is None:
        return {"ok": False, "reason": "index missing"}
    index = next(iter(data.values()))
    embedding = index["mappings"].get("properties", {}).get("embedding", {})
    knn_enabled = str(index["settings"]["index"].get("knn")).lower() == "true"
    ok = (
        knn_enabled
        and embedding.get("type") == "knn_vector"
        and int(embedding.get("dimension", 0)) == EMBEDDING_DIMENSION
    )
    return {"ok": ok, "reason": None if ok else "k-NN settings or embedding mapping do not match"}


def http_check(e: Dict) -> Dict:
    host = e["host"]
    if not ALB_PATTERN.match(host):
        raise ValueError("Invalid load balancer host")
    response = http.request("GET", f"http://{host}/ready", timeout=10.0)
    return {"status": response.status, "body": response.data[:300].decode("utf-8", "replace")}


ACTIONS = {
    function.__name__: function
    for function in (
        cluster_health, list_indices, register_repository, verify_repository, get_snapshot,
        create_snapshot, restore_snapshot, recovery_active, delete_partial_index,
        ensure_index, verify_index, http_check,
    )
}


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    return ACTIONS[event["action"]](event)
