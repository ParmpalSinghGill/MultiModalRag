"""
Fetch documents from AWS S3 for the Data Ingestion pipeline.

Downloads all documents uploaded in S3 under s3://<bucket>/<prefix>/
and organizes them locally under raw_documents/<document_id>/ ready for ingestion.
"""

import sys
import os
from pathlib import Path

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_ingestion.s3_fetcher import S3DocumentFetcher

# ==============================================================================
# ⚙️ INGESTION FETCH CONFIGURATION
# ==============================================================================
BUCKET_NAME = "multimodal-rag-data-nov14"
S3_PREFIX = "raw"           # Prefix used during upload: 'raw' or 'router'
AWS_PROFILE = "rag-dev"     # Configured AWS CLI profile
DEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw_documents")
# ==============================================================================


def format_bytes(num_bytes: int) -> str:
    """Formats bytes to human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} TB"


def main():
    print("=" * 72)
    print("📥 DATA INGESTION: FETCHING DOCUMENTS FROM S3")
    print("=" * 72)
    print(f"Source Bucket : s3://{BUCKET_NAME}/")
    print(f"Prefix        : {S3_PREFIX}/")
    print(f"AWS Profile   : {AWS_PROFILE}")
    print(f"Local Dest    : {DEST_DIR}")
    print("-" * 72)

    fetcher = S3DocumentFetcher(
        bucket_name=BUCKET_NAME,
        prefix=S3_PREFIX,
        profile_name=AWS_PROFILE
    )

    # 1. Fetch all documents and metadata from S3
    manifest = fetcher.fetch_all_documents(target_dir=DEST_DIR)

    # 2. Display summary of fetched documents
    documents = manifest.get("documents", {})
    total_files = sum(d["file_count"] for d in documents.values())
    total_bytes = sum(d["total_bytes"] for d in documents.values())

    print("\n" + "=" * 72)
    print(f"📋 FETCHED DOCUMENTS SUMMARY ({len(documents)} Documents, {total_files} Files, {format_bytes(total_bytes)})")
    print("=" * 72)

    for doc_id, doc in documents.items():
        fmt_label = doc["format"].upper()
        print(f"\n📂 Document ID: {doc_id} [{fmt_label}]")
        print(f"   • File Count : {doc['file_count']} file(s) ({format_bytes(doc['total_bytes'])})")
        print(f"   • Primary Doc: {doc['files'][0]}")

        # Show related questions if linked in metadata
        related_q = doc.get("related_questions", [])
        if related_q:
            print(f"   • Related Question(s): {len(related_q)}")
            for q in related_q:
                print(f"     - [QID {q['question_idx']}]: {q['question'][:80]}...")

    print("\n" + "=" * 72)
    print("✅ S3 FETCH COMPLETE - DATA READY FOR INGESTION")
    print("=" * 72)
    print(f"Documents directory: {DEST_DIR}")
    print(f"Manifest JSON file : {manifest.get('manifest_file')}")
    print("=" * 72)


if __name__ == "__main__":
    main()
