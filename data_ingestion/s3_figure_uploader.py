"""
S3 Figure and Chart Uploader with Zero-Duplicate SHA-256 Deduplication.

Features:
- Checks AWS S3 before uploading using object metadata and content SHA-256 hash.
- If an image already exists in S3 with identical content hash, upload is SKIPPED
  (zero extra storage, zero cost, reusing existing deterministic S3 link).
- If an image has changed/updated, S3 object is safely updated with new hash metadata.
- Returns deterministic S3 URIs (s3://bucket/figures/<doc_id>/<filename>)
  and HTTPS URLs (https://<bucket>.s3.amazonaws.com/figures/<doc_id>/<filename>).
- Directly enriches chunk dictionaries and local JSON manifests with S3 links.
"""

import os
import sys
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import boto3
from botocore.exceptions import ClientError

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_ingestion.utils import compute_file_hash


class S3FigureUploader:
    """
    Manages deduplicated figure and chart PNG uploads to AWS S3.
    """

    def __init__(
        self,
        bucket_name: str = "multimodal-rag-data-nov14",
        prefix: str = "figures",
        profile_name: str = "rag-dev",
        region_name: str = "us-east-1"
    ):
        self.bucket_name = bucket_name
        self.prefix = prefix.strip("/")
        self.profile_name = profile_name
        self.region_name = region_name

        self._s3_client = None
        self._init_s3_client()

    def _init_s3_client(self):
        """Initializes boto3 S3 client using configured profile or fallback."""
        try:
            available = boto3.Session().available_profiles
            if self.profile_name and self.profile_name in available:
                session = boto3.Session(profile_name=self.profile_name)
            else:
                session = boto3.Session()
            self._s3_client = session.client("s3", region_name=self.region_name)
        except Exception as e:
            print(f"[!] Warning: Could not initialize boto3 S3 client: {e}")
            self._s3_client = None

    def get_s3_key(self, doc_id: str, filename: str) -> str:
        """Constructs deterministic S3 object key."""
        clean_filename = Path(filename).name
        return f"{self.prefix}/{doc_id}/{clean_filename}"

    def get_s3_uri(self, s3_key: str) -> str:
        """Returns s3:// URI."""
        return f"s3://{self.bucket_name}/{s3_key}"

    def get_s3_url(self, s3_key: str) -> str:
        """Returns public/signed HTTPS URL."""
        return f"https://{self.bucket_name}.s3.amazonaws.com/{s3_key}"

    def upload_figure(
        self,
        local_path: str,
        doc_id: str,
        filename: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Uploads a single figure PNG to S3 with SHA-256 deduplication.

        Returns dict with:
            - s3_uri: s3://...
            - s3_url: https://...
            - sha256: content hash
            - action: 'deduplicated' | 'uploaded' | 'updated' | 'error'
            - reused: True if upload was skipped due to deduplication
        """
        path_obj = Path(local_path)
        if not path_obj.exists():
            return {
                "local_path": local_path,
                "error": "File not found locally",
                "action": "error",
                "reused": False
            }

        fname = filename or path_obj.name
        s3_key = self.get_s3_key(doc_id, fname)
        s3_uri = self.get_s3_uri(s3_key)
        s3_url = self.get_s3_url(s3_key)

        file_hash = compute_file_hash(str(path_obj))
        file_size = path_obj.stat().st_size

        if self._s3_client is None:
            return {
                "doc_id": doc_id,
                "filename": fname,
                "local_path": str(path_obj.resolve()),
                "s3_key": s3_key,
                "s3_uri": s3_uri,
                "s3_url": s3_url,
                "sha256": file_hash,
                "size_bytes": file_size,
                "action": "client_unavailable",
                "reused": False
            }

        # Step 1: Check if object already exists in S3 (HeadObject)
        try:
            head = self._s3_client.head_object(Bucket=self.bucket_name, Key=s3_key)
            existing_meta = head.get("Metadata", {})
            existing_hash = existing_meta.get("sha256") or existing_meta.get("file-sha256")
            existing_size = head.get("ContentLength")

            # If hash matches, or if size matches and hash was recorded, SKIP upload
            if existing_hash == file_hash or (existing_hash is None and existing_size == file_size):
                return {
                    "doc_id": doc_id,
                    "filename": fname,
                    "local_path": str(path_obj.resolve()),
                    "s3_key": s3_key,
                    "s3_uri": s3_uri,
                    "s3_url": s3_url,
                    "sha256": file_hash,
                    "size_bytes": file_size,
                    "action": "deduplicated",
                    "reused": True
                }

            # If hash differs, document or crop was modified -> Update
            action = "updated"

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey", "NotFound"):
                action = "uploaded"
            else:
                # Unexpected client error
                print(f"[!] S3 HeadObject failed for {s3_key}: {e}")
                action = "uploaded"

        # Step 2: Perform S3 upload with SHA-256 metadata
        try:
            extra_args = {
                "ContentType": "image/png",
                "Metadata": {
                    "sha256": file_hash,
                    "doc_id": str(doc_id),
                    "original_filename": fname
                }
            }
            self._s3_client.upload_file(
                Filename=str(path_obj),
                Bucket=self.bucket_name,
                Key=s3_key,
                ExtraArgs=extra_args
            )
            return {
                "doc_id": doc_id,
                "filename": fname,
                "local_path": str(path_obj.resolve()),
                "s3_key": s3_key,
                "s3_uri": s3_uri,
                "s3_url": s3_url,
                "sha256": file_hash,
                "size_bytes": file_size,
                "action": action,
                "reused": False
            }

        except Exception as e:
            print(f"[!] S3 upload failed for {local_path} -> {s3_key}: {e}")
            return {
                "doc_id": doc_id,
                "filename": fname,
                "local_path": str(path_obj.resolve()),
                "s3_key": s3_key,
                "s3_uri": s3_uri,
                "s3_url": s3_url,
                "sha256": file_hash,
                "size_bytes": file_size,
                "action": "error",
                "error": str(e),
                "reused": False
            }

    def sync_document_figures(
        self,
        doc_id: str,
        figures_dir: str
    ) -> Dict[str, Dict[str, Any]]:
        """
        Syncs all PNG figure files for a specific document to S3 with deduplication.
        Returns a mapping: filename -> upload_result.
        """
        figures_path = Path(figures_dir)
        # Check either directly figures_dir or figures_dir/doc_id
        doc_fig_dir = figures_path / doc_id if (figures_path / doc_id).is_dir() else figures_path

        png_files = sorted(list(doc_fig_dir.glob("*.png")))
        if not png_files:
            return {}

        results = {}
        dedup_count = 0
        upload_count = 0
        update_count = 0

        for png_file in png_files:
            res = self.upload_figure(str(png_file), doc_id=doc_id, filename=png_file.name)
            results[png_file.name] = res
            results[str(png_file.resolve())] = res

            if res["action"] == "deduplicated":
                dedup_count += 1
            elif res["action"] == "updated":
                update_count += 1
            elif res["action"] == "uploaded":
                upload_count += 1

        print(f"[*] S3 Sync for Doc '{doc_id}' ({len(png_files)} figures): "
              f"{dedup_count} deduplicated (skipped), {upload_count} uploaded, {update_count} updated.")

        return results

    def enrich_chunks_with_s3_links(
        self,
        chunks: List[Dict[str, Any]],
        sync_results: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Attaches deterministic S3 URIs, HTTPS URLs, and SHA-256 hashes to chunk metadata
        and appends the S3 link to the chunk text representation.
        """
        for chunk in chunks:
            img_file = chunk.get("image_file")
            if not img_file:
                continue

            fname = Path(img_file).name
            match_res = sync_results.get(fname) or sync_results.get(str(Path(img_file).resolve()))

            if match_res:
                s3_uri = match_res.get("s3_uri")
                s3_url = match_res.get("s3_url")
                sha256 = match_res.get("sha256")

                chunk["image_s3_uri"] = s3_uri
                chunk["image_s3_url"] = s3_url
                chunk["image_sha256"] = sha256

                # Append S3 link directly to chunk text if not already present
                chunk_text = chunk.get("text", "")
                if s3_url and "- S3 Image URL:" not in chunk_text:
                    chunk["text"] = f"{chunk_text}\n- S3 Image URL: {s3_url}\n- S3 URI: {s3_uri}"

        return chunks
