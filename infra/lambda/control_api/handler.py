"""
Always-available API routes. They cost nothing while idle and keep answering while
the runtime is stopped. POST /query is added by the runtime stack; when that stack
is gone the request falls through to $default and gets a clear 503.
"""

import json
import os
import re
from decimal import Decimal
from typing import Any, Dict

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config

dynamodb = boto3.resource("dynamodb")
documents = dynamodb.Table(os.environ["DOCUMENTS_TABLE"])
control = dynamodb.Table(os.environ["CONTROL_TABLE"])
s3 = boto3.client("s3", config=Config(signature_version="s3v4"))

BUCKET = os.environ["DOCUMENTS_BUCKET"]
SUFFIXES = tuple(s.lower() for s in json.loads(os.environ["DOCUMENT_SUFFIXES"]))
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
UPLOAD_URL_SECONDS = 900


def respond(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=lambda v: int(v) if isinstance(v, Decimal) else str(v)),
    }


def gate_open() -> bool:
    item = control.get_item(Key={"pk": "INGESTION", "sk": "GATE"}, ConsistentRead=True).get("Item")
    return bool(item and item.get("accepting"))


def platform_state() -> Dict[str, Any]:
    item = control.get_item(Key={"pk": "CONTROLLER", "sk": "STATE"}).get("Item") or {}
    return {
        "operation": item.get("operation"),
        "phase": item.get("phase"),
        "updated_at": item.get("updated_at"),
        "accepting_ingestion": gate_open(),
    }


def create_upload(event: Dict[str, Any]) -> Dict[str, Any]:
    if not gate_open():
        return respond(503, {"message": "Ingestion is paused. Start the platform and retry."})
    try:
        body = json.loads(event.get("body") or "{}")
        doc_id, filename = body["doc_id"], body["filename"]
    except (ValueError, KeyError, TypeError):
        return respond(400, {"message": "JSON body with doc_id and filename is required"})
    if not (isinstance(doc_id, str) and isinstance(filename, str)):
        return respond(400, {"message": "doc_id and filename must be strings"})
    if not SAFE_NAME.match(doc_id) or not SAFE_NAME.match(filename) or doc_id == "metadata":
        return respond(400, {"message": "doc_id or filename contains unsupported characters"})
    if not filename.lower().endswith(SUFFIXES):
        return respond(400, {"message": f"Supported file types: {', '.join(SUFFIXES)}"})
    key = f"raw/{doc_id}/{filename}"
    url = s3.generate_presigned_url(
        "put_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=UPLOAD_URL_SECONDS
    )
    return respond(201, {"upload_url": url, "method": "PUT", "key": key, "expires_in": UPLOAD_URL_SECONDS})


def get_job(event: Dict[str, Any]) -> Dict[str, Any]:
    doc_id = event["pathParameters"]["doc_id"]
    items = documents.query(
        KeyConditionExpression=Key("pk").eq(f"DOC#{doc_id}") & Key("sk").begins_with("VER#"),
        FilterExpression=Attr("status").exists(),
    )["Items"]
    if not items:
        return respond(404, {"message": "No ingestion job found"})
    fields = ("etag", "status", "attempts", "created_at", "finished_at", "error")
    versions = [{f: item[f] for f in fields if f in item} for item in items]
    return respond(200, {"doc_id": doc_id, "versions": versions})


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    route = event.get("routeKey")
    if route == "POST /documents":
        return create_upload(event)
    if route == "GET /jobs/{doc_id}":
        return get_job(event)
    if route == "GET /status":
        return respond(200, platform_state())
    return respond(503, {"message": "This service is stopped or not available.", **platform_state()})
