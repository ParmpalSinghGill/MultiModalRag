"""Runs the runtime stack through Terraform and enforces what a plan may touch."""

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ragctl.config import Config
from ragctl.errors import RagctlError, TerraformFailed, UnsafePlan

RUNTIME_DIR = Path(__file__).resolve().parents[2] / "terraform" / "runtime"

# Everything the runtime stack may manage. Anything else in a plan aborts the run,
# so a mistake can never reach buckets, tables, queues or keys.
RUNTIME_RESOURCE_TYPES = frozenset({
    "aws_apigatewayv2_integration",
    "aws_apigatewayv2_route",
    "aws_apigatewayv2_vpc_link",
    "aws_appautoscaling_policy",
    "aws_appautoscaling_target",
    "aws_ecs_service",
    "aws_ecs_task_definition",
    "aws_eip",
    "aws_elasticache_replication_group",
    "aws_elasticache_subnet_group",
    "aws_lb",
    "aws_lb_listener",
    "aws_lb_target_group",
    "aws_nat_gateway",
    "aws_opensearch_domain",
    "aws_route",
    "aws_ssm_parameter",
})


@dataclass
class Change:
    address: str
    type: str
    actions: List[str]


@dataclass
class Plan:
    path: Optional[str]
    changes: List[Change]

    @property
    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for change in self.changes:
            for action in change.actions:
                counts[action] = counts.get(action, 0) + 1
        return counts


def assert_plan_safe(changes: List[Change], destroying: bool) -> None:
    outside = [c.address for c in changes if c.type not in RUNTIME_RESOURCE_TYPES]
    if outside:
        raise UnsafePlan(f"Plan touches resources outside the runtime stack: {outside}")
    if not destroying:
        replaced = [c.address for c in changes if c.type == "aws_opensearch_domain" and "delete" in c.actions]
        if replaced:
            raise UnsafePlan(
                f"Plan would replace {replaced} without a snapshot. Run stop first, then start."
            )


class Terraform:
    def __init__(self, cfg: Config, project: str, environment: str, log: Callable[[str], None],
                 directory: Path = RUNTIME_DIR, binary: str = "terraform"):
        self.cfg = cfg
        self.log = log
        self.directory = directory
        self.binary = binary
        self.environment = {
            **os.environ,
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
            "TF_VAR_project": project,
            "TF_VAR_environment": environment,
            "TF_VAR_region": cfg.region,
            "TF_VAR_state_bucket": cfg.state_bucket,
        }
        self._initialized = False

    def _run(self, *arguments: str, allowed=(0,), attempts: int = 3) -> subprocess.CompletedProcess:
        if not shutil.which(self.binary):
            raise RagctlError("Terraform 1.10 or newer is required on PATH")
        command = [self.binary, f"-chdir={self.directory}", *arguments]
        for attempt in range(1, attempts + 1):
            result = subprocess.run(command, env=self.environment, capture_output=True, text=True)
            if result.returncode in allowed:
                return result
            output = f"{result.stdout}\n{result.stderr}"
            transient = any(t in output for t in (
                "Error acquiring the state lock", "RequestLimitExceeded", "Throttling", "connection reset",
                "TooManyRequestsException",
            ))
            if not transient or attempt == attempts:
                raise TerraformFailed(f"terraform {arguments[0]} failed:\n{output[-3000:]}")
            self.log(f"terraform {arguments[0]} hit a transient error, retrying")
            time.sleep(30 * attempt)
        raise TerraformFailed(f"terraform {arguments[0]} failed")

    def init(self) -> None:
        if self._initialized:
            return
        self._run(
            "init", "-input=false", "-no-color", "-reconfigure",
            f"-backend-config=bucket={self.cfg.state_bucket}",
            f"-backend-config=region={self.cfg.region}",
            "-backend-config=encrypt=true",
            "-backend-config=use_lockfile=true",
        )
        self._initialized = True

    def state_resources(self) -> List[str]:
        self.init()
        # "state list" exits non-zero when no state object exists yet.
        result = self._run("state", "list", "-no-color", allowed=(0, 1))
        if result.returncode == 1 and "No state file was found" not in result.stderr:
            raise TerraformFailed(f"terraform state list failed:\n{result.stderr[-2000:]}")
        return [line for line in result.stdout.splitlines() if line.strip()]

    def outputs(self) -> Dict[str, object]:
        self.init()
        data = json.loads(self._run("output", "-json", "-no-color").stdout or "{}")
        return {name: entry["value"] for name, entry in data.items()}

    def plan(self, destroy: bool = False, keep: bool = True) -> Plan:
        self.init()
        workspace = tempfile.mkdtemp(prefix="ragctl-plan-")  # mkdtemp is private to this user
        path = os.path.join(workspace, "runtime.tfplan")
        try:
            arguments = ["plan", "-input=false", "-no-color", "-detailed-exitcode", f"-out={path}"]
            if destroy:
                arguments.append("-destroy")
            result = self._run(*arguments, allowed=(0, 2))
            changes: List[Change] = []
            if result.returncode == 2:
                shown = json.loads(self._run("show", "-json", "-no-color", path).stdout)
                for item in shown.get("resource_changes", []):
                    actions = item["change"]["actions"]
                    if item.get("mode") == "managed" and actions not in (["no-op"], ["read"]):
                        changes.append(Change(item["address"], item["type"], actions))
            assert_plan_safe(changes, destroy)
            if keep and changes:
                return Plan(path, changes)
            shutil.rmtree(workspace, ignore_errors=True)
            return Plan(None, changes)
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    def apply(self, plan: Plan) -> None:
        if not plan.path:
            return
        try:
            self._run("apply", "-input=false", "-no-color", "-auto-approve", plan.path, attempts=1)
        finally:
            shutil.rmtree(os.path.dirname(plan.path), ignore_errors=True)
