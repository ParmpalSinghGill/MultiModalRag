import json
from dataclasses import dataclass, fields
from typing import Dict, List


@dataclass(frozen=True)
class Config:
    """Foundation values published to SSM by Terraform."""

    prefix: str
    region: str
    state_bucket: str
    image_tag: str
    enable_redis: bool
    documents_bucket: str
    backup_bucket: str
    documents_table: str
    control_table: str
    queue_url: str
    dlq_url: str
    pipe_name: str
    state_machine_arn: str
    ecs_cluster: str
    worker_families: List[str]
    api_service: str
    ops_function: str
    domain_name: str
    snapshot_repository: str
    snapshot_base_path: str
    manifest_prefix: str
    opensearch_index: str
    document_suffixes: List[str]
    ecr_repositories: Dict[str, str]
    api_endpoint: str

    @classmethod
    def from_json(cls, text: str) -> "Config":
        data = json.loads(text)
        return cls(**{f.name: data[f.name] for f in fields(cls)})

    @property
    def latest_manifest_key(self) -> str:
        return f"{self.manifest_prefix}/latest.json"

    def manifest_key(self, operation_id: str) -> str:
        return f"{self.manifest_prefix}/manifests/{operation_id}.json"

    def snapshot_file_key(self, uuid: str) -> str:
        return f"{self.snapshot_base_path}/snap-{uuid}.dat"


def load_config(session, project: str, environment: str) -> Config:
    name = f"/{project}-{environment}/foundation/config"
    value = session.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
    return Config.from_json(value)
