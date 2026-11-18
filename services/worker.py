"""
Ingestion worker run as one-off Fargate tasks by Step Functions.

parse: Docling parsing, chunking, figure extraction, figure upload, chunk JSON upload.
index: embedding generation and idempotent OpenSearch indexing.

Both stages can be killed at any time. The chunk JSON is written in a single PUT
after parsing finishes, and chunk IDs are deterministic, so a rerun is safe.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

MAX_TOKENS = 220  # Safe window for all-MiniLM-L6-v2, matching index_to_opensearch.py


def processed_key(doc_id: str, etag: str) -> str:
    return f"processed/{doc_id}/{etag}/chunks.json"


def object_exists(s3_client, bucket: str, key: str) -> bool:
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def make_portable(record: Dict[str, Any], source_uri: str) -> Dict[str, Any]:
    """Removes container-local paths from a processed document record."""
    record["local_path"] = source_uri
    for figure in record.get("figures", []):
        if figure.get("image_file"):
            figure["image_file"] = Path(figure["image_file"]).name
    for chunk in record.get("chunks", []):
        chunk["source_file"] = source_uri
        image_file = chunk.get("image_file")
        if image_file:
            name = Path(image_file).name
            chunk["text"] = chunk["text"].replace(f"- Image File: {image_file}", f"- Image File: {name}")
            chunk["image_file"] = name
    return record


def run_parse(args: argparse.Namespace) -> None:
    from data_ingestion.docling_processor import DoclingProcessor
    from data_ingestion.s3_figure_uploader import S3FigureUploader

    s3_client = boto3.client("s3")
    output_key = processed_key(args.doc_id, args.etag)
    if object_exists(s3_client, args.bucket, output_key):
        print(f"[*] Reusing existing parse output s3://{args.bucket}/{output_key}")
        return

    workdir = tempfile.mkdtemp(prefix="ingest-")
    try:
        doc_dir = Path(workdir) / args.doc_id
        doc_dir.mkdir(parents=True)
        local_file = doc_dir / Path(args.key).name
        s3_client.download_file(args.bucket, args.key, str(local_file))

        figures_dir = Path(workdir) / "figures"
        processor = DoclingProcessor(
            tokenizer_type="sentence-transformers",
            max_tokens=MAX_TOKENS,
            extract_figures=True,
            images_scale=2.0,
            classify_figures=True,
            deep_debug=False,
        )
        record = processor.process_document(
            str(local_file), doc_id=args.doc_id, figures_output_dir=str(figures_dir)
        )

        uploader = S3FigureUploader(
            bucket_name=args.bucket,
            prefix="figures",
            profile_name="",
            region_name=os.environ["AWS_REGION"],
        )
        sync_results = uploader.sync_document_figures(args.doc_id, str(figures_dir))
        # Results are keyed by both file name and path, so count unique files.
        failed = {
            r.get("local_path") for r in sync_results.values()
            if r.get("action") in ("error", "client_unavailable")
        }
        if failed:
            raise RuntimeError(f"{len(failed)} figure upload(s) failed for {args.doc_id}")
        record["chunks"] = uploader.enrich_chunks_with_s3_links(record["chunks"], sync_results)

        make_portable(record, f"s3://{args.bucket}/{args.key}")
        s3_client.put_object(
            Bucket=args.bucket,
            Key=output_key,
            Body=json.dumps(record).encode("utf-8"),
            ContentType="application/json",
        )
        print(f"[*] Wrote {len(record['chunks'])} chunks to s3://{args.bucket}/{output_key}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def remove_stale_chunks(client, index: str, doc_id: str, current_ids: List[str]) -> None:
    """Deletes chunks left by an older version of the same document."""
    client.delete_by_query(
        index=index,
        body={
            "query": {
                "bool": {
                    "filter": [{"term": {"document_id": doc_id}}],
                    "must_not": [{"ids": {"values": current_ids}}],
                }
            }
        },
        params={"refresh": "true", "conflicts": "proceed"},
    )


def run_index(args: argparse.Namespace) -> None:
    from data_ingestion.opensearch_indexer import OpenSearchIndexer
    from services.common import EMBEDDING_MODEL, index_name, opensearch_client

    s3_client = boto3.client("s3")
    response = s3_client.get_object(Bucket=args.bucket, Key=processed_key(args.doc_id, args.etag))
    with response["Body"] as body:
        record = json.loads(body.read())
    chunks = record.get("chunks", [])

    client = opensearch_client()
    indexer = OpenSearchIndexer(
        host=os.environ["OPENSEARCH_ENDPOINT"],
        port=443,
        index_name=index_name(),
        embedding_model_name=EMBEDDING_MODEL,
        device="cpu",
        client=client,
    )
    # The controller owns index creation. Bulk writes must never auto-create an
    # unmapped index while a snapshot restore is pending.
    if not client.indices.exists(index=indexer.index_name):
        raise RuntimeError(f"Index {indexer.index_name} does not exist")

    result = indexer.index_chunks(chunks)
    if result["errors"]:
        raise RuntimeError(f"{result['errors']} chunk(s) failed to index for {args.doc_id}")
    if chunks:
        remove_stale_chunks(client, indexer.index_name, args.doc_id, [c["chunk_id"] for c in chunks])


def main() -> int:
    parser = argparse.ArgumentParser(description="MultiFormat RAG ingestion worker")
    parser.add_argument("stage", choices=["parse", "index"])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--etag", required=True)
    parser.add_argument("--doc-id", required=True)
    args = parser.parse_args()
    {"parse": run_parse, "index": run_index}[args.stage](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
