"""Shared helpers for the ECS containers. Credentials come from the task role."""

import os

import boto3
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

from data_ingestion.opensearch_schema import DEFAULT_INDEX_NAME

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def region() -> str:
    return os.environ["AWS_REGION"]


def index_name() -> str:
    return os.environ.get("OPENSEARCH_INDEX", DEFAULT_INDEX_NAME)


def opensearch_client() -> OpenSearch:
    """Builds a SigV4-signed client for the VPC OpenSearch domain."""
    credentials = boto3.Session().get_credentials()
    return OpenSearch(
        hosts=[{"host": os.environ["OPENSEARCH_ENDPOINT"], "port": 443}],
        http_auth=AWSV4SignerAuth(credentials, region(), "es"),
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=60,
        max_retries=3,
        retry_on_timeout=True,
    )
