"""
AWS S3 Document Fetcher for Multimodal RAG Data Ingestion.

Fetches documents (PDFs and Images) and metadata uploaded to S3
so they can be processed by the ingestion pipeline (chunking, parsing, embedding).
"""

import os
import json
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import boto3
    from botocore.exceptions import ClientError
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False


class S3DocumentFetcher:
    """
    Fetches raw documents and question metadata from AWS S3 for data ingestion.
    """

    def __init__(
        self,
        bucket_name: str = "multimodal-rag-data-nov14",
        prefix: str = "raw",
        profile_name: Optional[str] = "rag-dev",
        region_name: Optional[str] = None
    ):
        if not BOTO3_AVAILABLE:
            raise ImportError("boto3 is required to fetch files from S3. Install with: pip install boto3")

        self.bucket_name = bucket_name
        self.prefix = prefix.strip("/")
        self.profile_name = self._resolve_profile(profile_name)
        self.region_name = region_name

        self.session = (
            boto3.Session(profile_name=self.profile_name, region_name=self.region_name)
            if self.profile_name
            else boto3.Session(region_name=self.region_name)
        )
        self.s3_client = self.session.client("s3")

    def _resolve_profile(self, profile_name: Optional[str]) -> Optional[str]:
        if profile_name:
            return profile_name
        env_profile = os.environ.get("AWS_PROFILE")
        if env_profile:
            return env_profile
        try:
            available = boto3.Session().available_profiles
            if "rag-dev" in available:
                return "rag-dev"
        except Exception:
            pass
        return None

    def list_s3_documents(self, prefix: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """
        Lists all document folders and files stored in S3 under the prefix.
        Returns a dict: {doc_id: [{'key': s3_key, 'filename': filename, 'size': size, 'format': 'pdf'|'image'}]}
        """
        target_prefix = (prefix or self.prefix).strip("/")
        if target_prefix and not target_prefix.endswith("/"):
            target_prefix += "/"

        paginator = self.s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=self.bucket_name, Prefix=target_prefix)

        documents: Dict[str, List[Dict[str, Any]]] = {}

        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                size = obj["Size"]
                if size == 0 or key.endswith("/"):
                    continue

                # Strip base prefix: e.g. "raw/3152577/3152577.pdf" -> "3152577/3152577.pdf"
                rel_key = key[len(target_prefix):]
                parts = rel_key.split("/")

                if len(parts) >= 2:
                    folder_name = parts[0]
                    filename = parts[-1]

                    # Skip general metadata folder when collecting raw documents
                    if folder_name == "metadata":
                        continue

                    doc_id = folder_name
                    fmt = "pdf" if filename.lower().endswith(".pdf") else "image"

                    if doc_id not in documents:
                        documents[doc_id] = []

                    documents[doc_id].append({
                        "key": key,
                        "filename": filename,
                        "size": size,
                        "format": fmt
                    })

        return documents

    def download_file(self, s3_key: str, local_dest: str) -> str:
        """Downloads a single object from S3 if not already cached with same size."""
        if os.path.exists(local_dest):
            local_size = os.path.getsize(local_dest)
            # Fetch remote size via head_object
            try:
                head = self.s3_client.head_object(Bucket=self.bucket_name, Key=s3_key)
                if head.get("ContentLength") == local_size:
                    return local_dest
            except Exception:
                pass

        os.makedirs(os.path.dirname(local_dest), exist_ok=True)
        self.s3_client.download_file(
            Bucket=self.bucket_name,
            Key=s3_key,
            Filename=local_dest
        )
        return local_dest

    def fetch_all_documents(
        self,
        target_dir: str = "./raw_documents",
        prefix: Optional[str] = None,
        max_workers: int = 8
    ) -> Dict[str, Any]:
        """
        Downloads all documents from S3 into target_dir/<doc_id>/<filename>.
        Also downloads the latest questions metadata JSON from S3.
        Returns an ingestion manifest.
        """
        target_prefix = (prefix or self.prefix).strip("/")
        os.makedirs(target_dir, exist_ok=True)

        docs = self.list_s3_documents(prefix=target_prefix)
        print(f"[*] Discovered {len(docs)} document folder(s) in s3://{self.bucket_name}/{target_prefix}/")

        tasks = []
        for doc_id, files in docs.items():
            doc_folder = os.path.join(target_dir, doc_id)
            for f_info in files:
                dest_file = os.path.join(doc_folder, f_info["filename"])
                tasks.append((f_info["key"], dest_file, doc_id))

        print(f"[*] Downloading {len(tasks)} file(s) across {len(docs)} document(s)...")

        local_files_by_doc: Dict[str, List[str]] = {d: [] for d in docs}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_info = {
                executor.submit(self.download_file, s3_key, local_dest): (doc_id, local_dest)
                for s3_key, local_dest, doc_id in tasks
            }
            for future in as_completed(future_to_info):
                doc_id, local_dest = future_to_info[future]
                saved_path = future.result()
                local_files_by_doc[doc_id].append(os.path.abspath(saved_path))

        # Fetch latest metadata JSON from S3
        metadata_file = self.fetch_metadata(target_dir=target_dir, prefix=target_prefix)

        # Build clean ingestion manifest
        manifest = self._build_manifest(
            target_dir=target_dir,
            local_files_by_doc=local_files_by_doc,
            metadata_file=metadata_file
        )

        return manifest

    def fetch_metadata(self, target_dir: str = "./raw_documents", prefix: Optional[str] = None) -> Optional[str]:
        """Downloads the latest questions metadata JSON from S3 metadata/ prefix."""
        target_prefix = (prefix or self.prefix).strip("/")
        meta_prefix = f"{target_prefix}/metadata/"

        try:
            res = self.s3_client.list_objects_v2(Bucket=self.bucket_name, Prefix=meta_prefix)
            contents = res.get("Contents", [])
            json_objects = [obj for obj in contents if obj["Key"].endswith(".json")]

            if not json_objects:
                return None

            # Get the most recently modified metadata file
            latest_obj = sorted(json_objects, key=lambda x: x["LastModified"], reverse=True)[0]
            meta_key = latest_obj["Key"]
            filename = os.path.basename(meta_key)

            meta_dest = os.path.join(target_dir, "metadata", filename)
            os.makedirs(os.path.dirname(meta_dest), exist_ok=True)
            self.s3_client.download_file(Bucket=self.bucket_name, Key=meta_key, Filename=meta_dest)
            return os.path.abspath(meta_dest)
        except Exception as e:
            print(f"⚠️ Notice: Could not fetch metadata from S3: {e}")
            return None

    def _build_manifest(
        self,
        target_dir: str,
        local_files_by_doc: Dict[str, List[str]],
        metadata_file: Optional[str]
    ) -> Dict[str, Any]:
        """Builds and saves ingestion_manifest.json."""
        questions_meta = []
        if metadata_file and os.path.exists(metadata_file):
            try:
                with open(metadata_file, "r", encoding="utf-8") as f:
                    meta_data = json.load(f)
                    questions_meta = meta_data.get("questions", [])
            except Exception:
                pass

        doc_manifest = {}
        for doc_id, file_paths in local_files_by_doc.items():
            file_paths = sorted(file_paths)
            has_pdf = any(p.lower().endswith(".pdf") for p in file_paths)
            fmt = "pdf" if has_pdf else "image"

            # Match related questions from metadata
            related_questions = [
                {
                    "question_idx": q.get("question_idx"),
                    "question": q.get("question"),
                    "answer": q.get("answer"),
                    "question_type": q.get("question_type")
                }
                for q in questions_meta
                if q.get("document_id") == doc_id
            ]

            doc_manifest[doc_id] = {
                "document_id": doc_id,
                "format": fmt,
                "file_count": len(file_paths),
                "files": file_paths,
                "total_bytes": sum(os.path.getsize(p) for p in file_paths if os.path.exists(p)),
                "related_questions": related_questions
            }

        manifest = {
            "bucket": self.bucket_name,
            "prefix": self.prefix,
            "total_documents": len(doc_manifest),
            "documents": doc_manifest,
            "metadata_file": metadata_file
        }

        manifest_path = os.path.join(target_dir, "ingestion_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        manifest["manifest_file"] = os.path.abspath(manifest_path)
        return manifest
