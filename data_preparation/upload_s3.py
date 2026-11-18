"""
Upload downloaded documents (PDFs or Images) to AWS S3.
Configured for the Finance domain.

Target structure in S3:
    • For PDF documents   : s3://multimodal-rag-data-nov14/router/<document_id>/<document_id>.pdf
    • For Image documents : s3://multimodal-rag-data-nov14/router/<document_id>/<page_image>.png
"""

import os
import json
import boto3
import mimetypes
from concurrent.futures import ThreadPoolExecutor

# ==============================================================================
# ⚙️ S3 CONFIGURATION
# ==============================================================================
BUCKET_NAME = "multimodal-rag-data-nov14"
S3_PREFIX = "raw"
AWS_PROFILE = "rag-dev"
DOMAIN = "finance"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DATA = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "data"))
DATA_DIR = PARENT_DATA if os.path.exists(PARENT_DATA) else os.path.join(SCRIPT_DIR, "data")

meta_candidates = [
    os.path.join(DATA_DIR, DOMAIN, "metadata", "selected_questions.json"),
    os.path.join(DATA_DIR, DOMAIN, "metadata", "selected_questions_2.json"),
    os.path.join(DATA_DIR, DOMAIN, "metadata", "selected_questions_5.json")
]
METADATA_FILE = next((p for p in meta_candidates if os.path.exists(p)), meta_candidates[0])
# ==============================================================================


def get_s3_client():
    """Initializes S3 client using the configured AWS profile."""
    session = boto3.Session(profile_name=AWS_PROFILE)
    return session.client("s3")


def upload_file(s3_client, local_path: str, s3_key: str):
    """Uploads a single file (PDF or PNG) to S3 with appropriate Content-Type."""
    content_type, _ = mimetypes.guess_type(local_path)
    if not content_type:
        content_type = "application/pdf" if local_path.endswith(".pdf") else "application/octet-stream"

    s3_client.upload_file(
        Filename=local_path,
        Bucket=BUCKET_NAME,
        Key=s3_key,
        ExtraArgs={"ContentType": content_type}
    )
    return f"s3://{BUCKET_NAME}/{s3_key}"


def main():
    print("=" * 70)
    print("☁️ AWS S3 MULTI-FORMAT DOCUMENT UPLOADER (Finance Domain)")
    print("=" * 70)
    print(f"Target Bucket : s3://{BUCKET_NAME}/")
    print(f"Base Prefix   : {S3_PREFIX}/")
    print(f"Domain        : {DOMAIN}")
    print(f"AWS Profile   : {AWS_PROFILE}")
    print("-" * 70)

    # 1. Load downloaded questions metadata
    if not os.path.exists(METADATA_FILE):
        print(f"❌ Metadata file not found at: {METADATA_FILE}")
        print("Please run `python DownloadData.py` first to download the documents.")
        return

    with open(METADATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    questions = data.get("questions", [])
    print(f"Found {len(questions)} selected question(s) in local cache.")

    # 2. Collect unique documents, their formats (PDF/Image), and local files
    doc_info = {}
    for q in questions:
        doc_id = q.get("document_id")
        fmt = q.get("format", "pdf")
        if doc_id and doc_id not in doc_info:
            doc_info[doc_id] = {
                "format": fmt,
                "files": q.get("local_files", [])
            }

    print(f"Unique documents to upload: {len(doc_info)} ({', '.join(doc_info.keys())})")

    # 3. Connect to S3
    s3 = get_s3_client()

    # 4. Prepare upload tasks
    tasks = []
    for doc_id, info in doc_info.items():
        for local_file in info["files"]:
            if os.path.exists(local_file):
                filename = os.path.basename(local_file)
                s3_key = f"{S3_PREFIX}/{doc_id}/{filename}"
                tasks.append((local_file, s3_key, doc_id, info["format"]))
            else:
                print(f"⚠️ Warning: File not found locally: {local_file}")

    print(f"\n[*] Uploading {len(tasks)} file(s) to S3...")

    uploaded_uris = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_task = {
            executor.submit(upload_file, s3, local_path, s3_key): (doc_id, s3_key)
            for local_path, s3_key, doc_id, _ in tasks
        }
        for future in future_to_task:
            doc_id, s3_key = future_to_task[future]
            s3_uri = future.result()
            if doc_id not in uploaded_uris:
                uploaded_uris[doc_id] = []
            uploaded_uris[doc_id].append(s3_uri)

    # 5. Upload metadata JSON to S3
    meta_key = f"{S3_PREFIX}/metadata/selected_questions_{len(questions)}.json"
    with open(METADATA_FILE, "rb") as f:
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=meta_key,
            Body=f.read(),
            ContentType="application/json"
        )

    # 6. Summary
    print("\n" + "=" * 70)
    print("✅ S3 UPLOAD COMPLETE (Finance Domain)")
    print("=" * 70)
    for doc_id, info in doc_info.items():
        fmt_label = info["format"].upper()
        uris = uploaded_uris.get(doc_id, [])
        print(f"  📂 s3://{BUCKET_NAME}/{S3_PREFIX}/{doc_id}/ [{fmt_label}] ({len(uris)} file(s))")
        for u in uris:
            print(f"     • {u}")

    print(f"\n  📄 Metadata: s3://{BUCKET_NAME}/{meta_key}")
    print("=" * 70)


if __name__ == "__main__":
    main()
