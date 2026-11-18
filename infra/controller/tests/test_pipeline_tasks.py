import importlib.util
import json
import re
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from ragctl.gateway import derive_doc_id

INFRA = Path(__file__).resolve().parents[2]


class Conditional(ClientError):
    def __init__(self):
        super().__init__({"Error": {"Code": "ConditionalCheckFailedException"}}, "Write")


def holds(condition, item):
    if condition is None:
        return True
    expression = condition.get_expression()
    operator, values = expression["operator"], expression["values"]
    if operator == "AND":
        return holds(values[0], item) and holds(values[1], item)
    if operator == "OR":
        return holds(values[0], item) or holds(values[1], item)
    name = values[0].name
    if operator == "attribute_not_exists":
        return item is None or name not in item
    return item is not None and item.get(name) == values[1]


class Table:
    def __init__(self):
        self.items = {}

    @staticmethod
    def identity(key):
        return key["pk"], key["sk"]

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get(self.identity(Key))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = self.identity(Item)
        if not holds(ConditionExpression, self.items.get(key)):
            raise Conditional()
        self.items[key] = dict(Item)

    def delete_item(self, Key, ConditionExpression=None):
        key = self.identity(Key)
        if not holds(ConditionExpression, self.items.get(key)):
            raise Conditional()
        self.items.pop(key, None)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues, ConditionExpression=None):
        key = self.identity(Key)
        if not holds(ConditionExpression, self.items.get(key)):
            raise Conditional()
        for name, value in re.findall(r"(#\w+) = (:\w+)", UpdateExpression):
            self.items[key][ExpressionAttributeNames[name]] = ExpressionAttributeValues[value]


class StepFunctions:
    class exceptions:
        class ExecutionDoesNotExist(Exception):
            pass

    def __init__(self):
        self.running = set()

    def describe_execution(self, executionArn):
        return {"status": "RUNNING" if executionArn in self.running else "SUCCEEDED"}


@pytest.fixture
def tasks(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("DOCUMENTS_TABLE", "documents")
    monkeypatch.setenv("CONTROL_TABLE", "control")
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "1")
    spec = importlib.util.spec_from_file_location("pipeline_handler", INFRA / "lambda/pipeline_tasks/handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.documents, module.control, module.sfn = Table(), Table(), StepFunctions()
    module.conditional_failure = Conditional
    module.control.items[("INGESTION", "GATE")] = {"pk": "INGESTION", "sk": "GATE", "accepting": True}
    return module


DOC = {"bucket": "docs", "key": "raw/a/a.pdf", "etag": "e1", "doc_id": "a", "force": False}


def s3_message(key):
    return {"body": json.dumps({"Records": [{
        "eventName": "ObjectCreated:Put",
        "s3": {"bucket": {"name": "docs"}, "object": {"key": key, "eTag": "e1"}},
    }]})}


def test_parse_event_decodes_s3_keys_and_skips_metadata(tasks):
    result = tasks.parse_event({"event": [s3_message("raw/doc+1/file.pdf"), s3_message("raw/metadata/q.pdf")]})
    assert [d["key"] for d in result["documents"]] == ["raw/doc 1/file.pdf"]
    assert result["documents"][0]["doc_id"] == "doc_1"


def test_parse_event_accepts_requeued_documents(tasks):
    event = {"event": [{"body": json.dumps({"document": DOC})}]}
    assert tasks.parse_event(event)["documents"] == [DOC]


def test_closed_or_missing_gate_defers_work(tasks):
    tasks.control.items.clear()
    assert tasks.acquire_slot({"execution_id": "x"})["status"] == "DEFERRED"


def test_slots_limit_concurrency_and_recover_from_dead_owners(tasks):
    tasks.sfn.running = {"first", "second"}
    assert tasks.acquire_slot({"execution_id": "first"})["status"] == "ACQUIRED"
    assert tasks.acquire_slot({"execution_id": "second"})["status"] == "WAIT"
    tasks.sfn.running = {"second"}
    assert tasks.acquire_slot({"execution_id": "second"})["status"] == "ACQUIRED"


def test_claim_is_idempotent_and_recovers_interrupted_work(tasks):
    tasks.sfn.running = {"first", "second"}
    assert tasks.claim({"doc": DOC, "execution_id": "first"})["status"] == "CLAIMED"
    assert tasks.claim({"doc": DOC, "execution_id": "second"})["status"] == "IN_PROGRESS"
    tasks.sfn.running = {"second"}
    assert tasks.claim({"doc": DOC, "execution_id": "second"})["status"] == "CLAIMED"
    assert tasks.finish({"doc": DOC, "execution_id": "first"}, "FAILED")["status"] == "NOT_OWNER"
    assert tasks.finish({"doc": DOC, "execution_id": "second"}, "COMPLETED")["status"] == "COMPLETED"
    assert tasks.claim({"doc": DOC, "execution_id": "third"})["status"] == "ALREADY_DONE"
    assert tasks.claim({"doc": {**DOC, "force": True}, "execution_id": "third"})["status"] == "CLAIMED"


@pytest.mark.parametrize("key", [
    "raw/123/123.pdf", "raw/flat.PDF", "raw/metadata/x.pdf", "raw/a/b.json", "other/a.pdf", "raw/sp ace/x.pdf",
])
def test_doc_id_rules_match_controller(tasks, key):
    assert tasks.derive_doc_id(key) == derive_doc_id(key, (".pdf",))


def render_state_machine():
    text = (INFRA / "terraform/foundation/ingestion.asl.json.tftpl").read_text()
    text = re.sub(r"\$\{(subnets|security_groups)\}", '["x"]', text)
    return json.loads(re.sub(r"\$\{\w+\}", "value", text))


def walk(machine):
    states = machine["States"]
    assert machine["StartAt"] in states
    seen, pending = set(), [machine["StartAt"]]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        state = states[name]
        targets = [state.get("Next"), state.get("Default")]
        targets += [c["Next"] for c in state.get("Choices", []) + state.get("Catch", [])]
        pending += [t for t in targets if t]
        if "ItemProcessor" in state:
            walk(state["ItemProcessor"])
        assert state["Type"] in ("Choice", "Wait") or state.get("End") or state.get("Next") or state["Type"] == "Map"
    assert seen == set(states), f"Unreachable states: {set(states) - seen}"


def test_state_machine_is_well_formed():
    walk(render_state_machine())


def test_workflow_input_is_not_merged_into_array():
    assert "ResultPath" not in render_state_machine()["States"]["ParseEvent"]
