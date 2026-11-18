"""
AWS S3 Uploader for Multimodal RAG Documents.

Uploads document images and question metadata to AWS S3 under a clean folder hierarchy:
s3://<bucket>/<prefix>/<document_id>/<image_name>.png
"""

import os
import json
import mimetypes
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import boto3
    from botocore.exceptions import ClientError, ProfileNotFound
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False


class S3DocumentUploader:
    """
    Manages uploading multimodal documents and question datasets to AWS S3.
    """

    def __init__(
        self,
        bucket_name: str = "multimodal-rag-data-nov14",
        profile_name: Optional[str] = None,
        region_name: Optional[str] = None
    ):
        if not BOTO3_AVAILABLE:
            raise ImportError("boto3 is required for S3 uploads. Install with: pip install boto3")

        self.bucket_name = bucket_name
        self.profile_name = self._resolve_profile(profile_name)
        self.region_name = region_name
        
        # Initialize boto3 session and client
        self.session = (
            boto3.Session(profile_name=self.profile_name, region_name=self.region_name)
            if self.profile_name
            else boto3.Session(region_name=self.region_name)
        )
        self.s3_client = self.session.client("s3")
        self._verify_bucket()

    def _resolve_profile(self, profile_name: Optional[str]) -> Optional[str]:
        """
        Resolves AWS CLI profile, preferring explicit profile, then AWS_PROFILE env var,
        then 'rag-dev' if configured in AWS CLI, and finally default.
        """
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

    def _verify_bucket(self):
        """
        Verifies access to the target S3 bucket.
        """
        try:
            # Check bucket access by listing with max-keys=1
            self.s3_client.list_objects_v2(Bucket=self.bucket_name, MaxKeys=1)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchBucket":
                raise ValueError(f"S3 Bucket '{self.bucket_name}' does not exist.")
            elif error_code in ("AccessDenied", "403"):
                raise PermissionError(
                    f"Access denied to S3 Bucket '{self.bucket_name}' using profile '{self.profile_name}'."
                )
            raise e

    def upload_file(self, local_path: str, s3_key: str) -> str:
        """
        Uploads a single local file to S3.
        Returns the s3:// URI.
        """
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")

        content_type, _ = mimetypes.guess_type(local_path)
        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type

        self.s3_client.upload_file(
            Filename=local_path,
            Bucket=self.bucket_name,
            Key=s3_key,
            ExtraArgs=extra_args if extra_args else None
        )
        return f"s3://{self.bucket_name}/{s3_key}"

    def upload_document_files(
        self,
        document_id: str,
        local_files: List[str],
        prefix: str = "router",
        max_workers: int = 8
    ) -> List[str]:
        """
        Uploads all files (PDFs or images) of a document to s3://<bucket>/<prefix>/<document_id>/<filename>.
        """
        clean_prefix = prefix.strip("/")
        tasks = []
        for file_path in local_files:
            filename = os.path.basename(file_path)
            s3_key = f"{clean_prefix}/{document_id}/{filename}"
            tasks.append((file_path, s3_key))

        uploaded_uris = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_key = {
                executor.submit(self.upload_file, local_path, s3_key): s3_key
                for local_path, s3_key in tasks
            }
            for future in as_completed(future_to_key):
                s3_uri = future.result()
                uploaded_uris.append(s3_uri)

        return sorted(uploaded_uris)

    def upload_questions_dataset(
        self,
        download_result: Dict[str, Any],
        prefix: str = "router",
        upload_metadata: bool = True,
        max_workers: int = 8
    ) -> Dict[str, Any]:
        """
        Uploads all documents (PDFs or images) required by the selected questions to S3.
        Folder structure in S3:
            s3://<bucket>/<prefix>/<document_id>/<filename> (.pdf or .png)
            s3://<bucket>/<prefix>/metadata/selected_questions.json
        """
        clean_prefix = prefix.strip("/")
        questions = download_result.get("questions", [])

        # Group files by document ID to avoid duplicate uploads
        docs_to_files: Dict[str, Dict[str, Any]] = {}
        for q in questions:
            doc_id = q.get("document_id", "unknown")
            fmt = q.get("format", "image")
            if doc_id not in docs_to_files:
                docs_to_files[doc_id] = {"format": fmt, "files": []}

            target_files = q.get("local_files")
            if not target_files:
                if q.get("local_pdf"):
                    target_files = [q["local_pdf"]]
                else:
                    target_files = q.get("local_images", [])

            for f_path in target_files:
                if f_path and f_path not in docs_to_files[doc_id]["files"]:
                    docs_to_files[doc_id]["files"].append(f_path)

        # Upload documents concurrently
        doc_s3_map: Dict[str, List[str]] = {}
        print(f"[*] Uploading {len(docs_to_files)} document(s) to S3 (s3://{self.bucket_name}/{clean_prefix}/)...")

        for doc_id, info in docs_to_files.items():
            files = info["files"]
            fmt = info["format"]
            print(f"    • Document {doc_id} ({fmt.upper()}): uploading {len(files)} file(s)...")
            uris = self.upload_document_files(
                document_id=doc_id,
                local_files=files,
                prefix=clean_prefix,
                max_workers=max_workers
            )
            doc_s3_map[doc_id] = uris

        # Attach S3 URIs to each question record
        for q in questions:
            doc_id = q.get("document_id", "unknown")
            q["s3_document_prefix"] = f"s3://{self.bucket_name}/{clean_prefix}/{doc_id}/"
            q["s3_files"] = doc_s3_map.get(doc_id, [])

            if q.get("format") == "pdf":
                pdf_uris = [u for u in q["s3_files"] if u.endswith(".pdf")]
                q["s3_pdf"] = pdf_uris[0] if pdf_uris else None
            else:
                q["s3_images"] = q["s3_files"]

            # Ground-truth page S3 URIs
            gt_s3 = []
            for gt_rel in q.get("gt_image_paths_rel", []):
                gt_filename = os.path.basename(gt_rel)
                gt_s3.append(f"s3://{self.bucket_name}/{clean_prefix}/{doc_id}/{gt_filename}")
            q["s3_gt_images"] = gt_s3

        # Optionally upload consolidated metadata JSON to S3
        metadata_s3_uri = None
        if upload_metadata:
            meta_key = f"{clean_prefix}/metadata/selected_questions_{len(questions)}.json"
            meta_json = json.dumps(
                {
                    "bucket": self.bucket_name,
                    "prefix": clean_prefix,
                    "domain": download_result.get("domain"),
                    "num_questions": len(questions),
                    "unique_documents": list(docs_to_files.keys()),
                    "questions": questions
                },
                indent=2
            )
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=meta_key,
                Body=meta_json.encode("utf-8"),
                ContentType="application/json"
            )
            metadata_s3_uri = f"s3://{self.bucket_name}/{meta_key}"
            print(f"    ✓ Question metadata uploaded to {metadata_s3_uri}")

        return {
            "bucket": self.bucket_name,
            "prefix": clean_prefix,
            "uploaded_documents": sorted(list(docs_to_files.keys())),
            "total_files_uploaded": sum(len(uris) for uris in doc_s3_map.values()),
            "doc_s3_map": doc_s3_map,
            "metadata_s3_uri": metadata_s3_uri,
            "questions": questions
        }
