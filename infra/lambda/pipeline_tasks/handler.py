"""
Step Functions task handler: event parsing, concurrency slots and idempotency.

Ownership is tied to Step Functions executions. A slot or document claim held by
an execution that is no longer RUNNING is taken over, so crashes, stopped
executions and Spot interruptions cannot leak capacity or block a document.
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import unquote_plus

import boto3
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource("dynamodb")
documents = dynamodb.Table(os.environ["DOCUMENTS_TABLE"])
control = dynamodb.Table(os.environ["CONTROL_TABLE"])
sfn = boto3.client("stepfunctions")
conditional_failure = dynamodb.meta.client.exceptions.ConditionalCheckFailedException

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
RAW_PREFIX = os.environ.get("RAW_PREFIX", "raw/")
SUFFIXES = tuple(s.lower() for s in json.loads(os.environ.get("DOCUMENT_SUFFIXES", '[".pdf"]')))
RECORD_TTL_SECONDS = 365 * 24 * 3600


def now() -> int:
    return int(time.time())


def derive_doc_id(key: str) -> Optional[str]:
    """raw/<doc_id>/<file> keeps the existing layout; raw/<file> uses the file stem."""
    if not key.startswith(RAW_PREFIX) or not key.lower().endswith(SUFFIXES):
        return None
    parts = key[len(RAW_PREFIX):].split("/")
    if parts[0] == "metadata":
        return None
    candidate = parts[0] if len(parts) > 1 else parts[0].rsplit(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9._-]", "_", candidate)[:128] or None


def documents_from_body(body: Any) -> List[Dict[str, Any]]:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            print(f"Ignoring non-JSON message: {body[:200]}")
            return []
    if not isinstance(body, dict):
        return []
    if "document" in body:  # Requeued by the workflow or the controller.
        return [body["document"]]
    found = []
    for record in body.get("Records", []):
        if not str(record.get("eventName", "")).startswith("ObjectCreated"):
            continue
        s3 = record["s3"]
        key = unquote_plus(s3["object"]["key"])
        doc_id = derive_doc_id(key)
        if doc_id:
            found.append({
                "bucket": s3["bucket"]["name"],
                "key": key,
                "etag": s3["object"]["eTag"].strip('"'),
                "doc_id": doc_id,
                "force": False,
            })
    return found


def parse_event(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = event["event"]
    records = payload if isinstance(payload, list) else [payload]
    found = []
    for record in records:
        body = record.get("body", record) if isinstance(record, dict) else record
        found.extend(documents_from_body(body))
    return {"documents": found}


def execution_running(execution_id: Optional[str]) -> bool:
    if not execution_id:
        return False
    try:
        return sfn.describe_execution(executionArn=execution_id)["status"] == "RUNNING"
    except sfn.exceptions.ExecutionDoesNotExist:
        return False


def ingestion_open() -> bool:
    """Fails closed: a missing gate record means ingestion is not accepted."""
    item = control.get_item(Key={"pk": "INGESTION", "sk": "GATE"}, ConsistentRead=True).get("Item")
    return bool(item and item.get("accepting"))


def acquire_slot(event: Dict[str, Any]) -> Dict[str, Optional[str]]:
    if not ingestion_open():
        return {"status": "DEFERRED", "slot": None}
    me = event["execution_id"]
    for number in range(MAX_CONCURRENT_JOBS):
        key = {"pk": f"SLOT#{number}", "sk": "LEASE"}
        item = documents.get_item(Key=key, ConsistentRead=True).get("Item")
        owner = item.get("owner") if item else None
        if owner == me:
            return {"status": "ACQUIRED", "slot": key["pk"]}
        if item and execution_running(owner):
            continue
        condition = Attr("owner").eq(owner) if item else Attr("pk").not_exists()
        try:
            documents.put_item(Item={**key, "owner": me, "acquired_at": now()}, ConditionExpression=condition)
            return {"status": "ACQUIRED", "slot": key["pk"]}
        except conditional_failure:
            continue
    return {"status": "WAIT", "slot": None}


def release_slot(event: Dict[str, Any]) -> Dict[str, str]:
    slot = event.get("slot")
    if slot:
        try:
            documents.delete_item(
                Key={"pk": slot, "sk": "LEASE"},
                ConditionExpression=Attr("owner").eq(event["execution_id"]),
            )
        except conditional_failure:
            pass  # Another execution already took over this slot.
    return {"status": "RELEASED"}


def document_key(doc: Dict[str, Any]) -> Dict[str, str]:
    return {"pk": f"DOC#{doc['doc_id']}", "sk": f"VER#{doc['etag']}"}


def claim(event: Dict[str, Any]) -> Dict[str, str]:
    doc, me = event["doc"], event["execution_id"]
    key = document_key(doc)
    item = documents.get_item(Key=key, ConsistentRead=True).get("Item")
    status = item.get("status") if item else None
    owner = item.get("execution_id") if item else None

    if status == "COMPLETED" and not doc.get("force"):
        return {"status": "ALREADY_DONE"}
    if status == "PROCESSING" and owner != me and execution_running(owner):
        return {"status": "IN_PROGRESS"}

    condition = Attr("pk").not_exists()
    if item:
        condition = Attr("status").eq(status) & Attr("updated_at").eq(item["updated_at"])
    try:
        documents.put_item(
            Item={
                **key,
                "doc_id": doc["doc_id"],
                "status": "PROCESSING",
                "bucket": doc["bucket"],
                "object_key": doc["key"],
                "etag": doc["etag"],
                "execution_id": me,
                "attempts": int(item.get("attempts", 0)) + 1 if item else 1,
                "created_at": item.get("created_at", now()) if item else now(),
                "updated_at": time.time_ns(),
                "expires_at": now() + RECORD_TTL_SECONDS,
            },
            ConditionExpression=condition,
        )
    except conditional_failure:
        return {"status": "IN_PROGRESS"}
    return {"status": "CLAIMED"}


def finish(event: Dict[str, Any], status: str, error: Optional[Any] = None) -> Dict[str, str]:
    values = {"status": status, "updated_at": time.time_ns(), "finished_at": now()}
    if error is not None:
        values["error"] = json.dumps(error)[:2000]
    names = {f"#{k}": k for k in values}
    try:
        documents.update_item(
            Key=document_key(event["doc"]),
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in values),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={f":{k}": v for k, v in values.items()},
            ConditionExpression=Attr("execution_id").eq(event["execution_id"]),
        )
    except conditional_failure:
        return {"status": "NOT_OWNER"}
    return {"status": status}


ACTIONS = {
    "parse_event": parse_event,
    "acquire_slot": acquire_slot,
    "release_slot": release_slot,
    "claim": claim,
    "complete": lambda event: finish(event, "COMPLETED"),
    "fail": lambda event: finish(event, "FAILED", event.get("error")),
}


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    return ACTIONS[event["action"]](event)
