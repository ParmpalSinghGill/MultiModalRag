"""
Minimalistic, fast, and targeted downloader for Salesforce/UniDoc-Bench.
Ensures ONLY the required files are downloaded, avoiding gigabytes of unnecessary data.
"""

import os
import io
import json
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from huggingface_hub import hf_hub_download
    HF_HUB_AVAILABLE = True
except ImportError:
    HF_HUB_AVAILABLE = False

try:
    import pyarrow.parquet as pq
    PYARROW_AVAILABLE = True
except ImportError:
    PYARROW_AVAILABLE = False

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

from .parser import parse_hf_viewer_url


class UniDocDownloader:
    """
    Precision downloader for Salesforce/UniDoc-Bench dataset.
    """

    HF_BASE_URL = "https://huggingface.co/datasets/Salesforce/UniDoc-Bench/resolve/main"
    HF_SERVER_API = "https://datasets-server.huggingface.co/rows"

    def __init__(self, repo_id: str = "Salesforce/UniDoc-Bench", token: Optional[str] = None):
        self.repo_id = repo_id
        self.token = token or os.environ.get("HF_TOKEN")

    def _download_file(self, rel_path: str, dest_path: str) -> str:
        """
        Download a single file from the HF repository to dest_path if not already present.
        Ensures a clean direct download without extraneous cache directories.
        """
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        url = f"{self.HF_BASE_URL}/{rel_path}"
        headers = {"User-Agent": "Mozilla/5.0 (UniDoc-Targeted-Downloader/1.0)"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        temp_dest = dest_path + ".tmp"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as resp, open(temp_dest, "wb") as out_f:
                block_size = 64 * 1024
                while True:
                    chunk = resp.read(block_size)
                    if not chunk:
                        break
                    out_f.write(chunk)
            os.replace(temp_dest, dest_path)
            return dest_path
        except Exception as e:
            if os.path.exists(temp_dest):
                try:
                    os.remove(temp_dest)
                except OSError:
                    pass
            # If direct HTTP failed and HF_HUB is available, try hf_hub_download as secondary fallback
            if HF_HUB_AVAILABLE:
                try:
                    cached_file = hf_hub_download(
                        repo_id=self.repo_id,
                        repo_type="dataset",
                        filename=rel_path,
                        token=self.token
                    )
                    import shutil
                    shutil.copyfile(cached_file, dest_path)
                    return dest_path
                except Exception:
                    pass
            raise RuntimeError(f"Failed to download {rel_path}: {e}")

    def download_split_parquet(self, domain: str, output_dir: str = "./data") -> str:
        """
        Download ONLY the parquet metadata file for the specified domain.
        File size: ~58 KB - 110 KB.
        Does NOT download other splits or images.
        """
        filename = f"data/{domain}-00000-of-00001.parquet"
        dest_dir = os.path.join(output_dir, domain, "metadata")
        dest_path = os.path.join(dest_dir, f"{domain}.parquet")
        return self._download_file(filename, dest_path)

    def get_split_table(self, domain: str, output_dir: str = "./data") -> Dict[str, List[Any]]:
        """
        Loads the domain QA data as a dict of column lists using pyarrow.parquet.ParquetFile.
        Bypasses pandas and numpy to prevent any version incompatibility.
        """
        parquet_path = self.download_split_parquet(domain, output_dir)
        if not PYARROW_AVAILABLE:
            raise RuntimeError("pyarrow is required to read parquet files. Install with: pip install pyarrow")
        
        pf = pq.ParquetFile(parquet_path)
        table = pf.read()
        return table.to_pydict()

    def fetch_row_via_api(self, domain: str, row_idx: int) -> Optional[Dict[str, Any]]:
        """
        Fetches row metadata directly from HuggingFace Datasets-Server API.
        Zero parquet files downloaded (pure JSON response in ~150ms).
        """
        url = f"{self.HF_SERVER_API}?dataset={self.repo_id}&config=default&split={domain}&offset={row_idx}&limit=1"
        headers = {"User-Agent": "UniDoc-Minimal-Downloader/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                rows = data.get("rows", [])
                if rows:
                    return rows[0].get("row")
        except Exception:
            return None
        return None

    def get_row(self, domain: str, row_idx: int, output_dir: str = "./data") -> Dict[str, Any]:
        """
        Gets the metadata for a specific row index, trying the fast serverless API first,
        and falling back to the 58KB domain parquet.
        """
        row_data = self.fetch_row_via_api(domain, row_idx)
        if row_data:
            return row_data

        # Fallback: read from downloaded domain parquet
        data = self.get_split_table(domain, output_dir)
        total_rows = len(data["question"])
        if row_idx < 0 or row_idx >= total_rows:
            raise IndexError(f"Row index {row_idx} out of range (0..{total_rows - 1}) for domain '{domain}'")

        return {col: data[col][row_idx] for col in data}

    def download_row(
        self,
        domain: str,
        row_idx: int,
        output_dir: str = "./data",
        gt_only: bool = False,
        save_metadata: bool = True
    ) -> Dict[str, Any]:
        """
        Downloads ONLY the required files for a specific row.
        - Gets row metadata
        - Downloads ONLY the page images referenced by this QA pair
        - Does NOT download other domain data, unused images, or PDF archives.
        """
        row = self.get_row(domain, row_idx, output_dir)
        doc_id = None

        # Determine target image paths
        target_images = row.get("gt_image_paths", []) if gt_only else row.get("longdoc_image_paths", [])
        if not target_images and "gt_image_paths" in row:
            target_images = row["gt_image_paths"]

        downloaded_images = []
        for img_rel in target_images:
            dest_file = os.path.join(output_dir, img_rel)
            saved_path = self._download_file(img_rel, dest_file)
            downloaded_images.append(os.path.abspath(saved_path))
            if not doc_id and len(img_rel.split("/")) >= 3:
                doc_id = img_rel.split("/")[2]

        result = {
            "domain": domain,
            "row_idx": row_idx,
            "document_id": doc_id,
            "question": row.get("question"),
            "answer": row.get("answer"),
            "question_type": row.get("question_type"),
            "answer_type": row.get("answer_type"),
            "gt_image_paths_rel": row.get("gt_image_paths", []),
            "longdoc_image_paths_rel": row.get("longdoc_image_paths", []),
            "local_images": downloaded_images,
        }

        if save_metadata:
            meta_dir = os.path.join(output_dir, domain, "metadata")
            os.makedirs(meta_dir, exist_ok=True)
            meta_file = os.path.join(meta_dir, f"row_{row_idx:04d}.json")
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            result["metadata_file"] = os.path.abspath(meta_file)

        return result

    def extract_document_pdf(self, domain: str, doc_id: str, output_dir: str = "./data") -> str:
        """
        Extracts the original PDF for doc_id from the domain tar.gz archive.
        Downloads the domain tar.gz if not already present.
        """
        dest_dir = os.path.join(output_dir, domain, "documents", doc_id)
        os.makedirs(dest_dir, exist_ok=True)
        dest_pdf = os.path.join(dest_dir, f"{doc_id}.pdf")
        if os.path.exists(dest_pdf) and os.path.getsize(dest_pdf) > 0:
            return os.path.abspath(dest_pdf)

        archive_path = os.path.join(output_dir, domain, "pdfs", f"{domain}_pdfs.tar.gz")
        if not os.path.exists(archive_path) or os.path.getsize(archive_path) == 0:
            print(f"[*] Downloading {domain}_pdfs.tar.gz archive for PDF extraction...")
            self.download_domain_pdfs(domain, output_dir)

        import tarfile
        with tarfile.open(archive_path, "r:gz") as tar:
            target = f"{domain}/{doc_id}.pdf"
            try:
                member = tar.getmember(target)
                f = tar.extractfile(member)
                if f:
                    with open(dest_pdf, "wb") as out_f:
                        out_f.write(f.read())
                    return os.path.abspath(dest_pdf)
            except KeyError:
                # Try finding without domain prefix
                for m in tar.getmembers():
                    if m.name.endswith(f"{doc_id}.pdf") and not os.path.basename(m.name).startswith("._"):
                        f = tar.extractfile(m)
                        if f:
                            with open(dest_pdf, "wb") as out_f:
                                out_f.write(f.read())
                            return os.path.abspath(dest_pdf)

        raise FileNotFoundError(f"Document PDF '{doc_id}.pdf' not found in {archive_path}")

    def download_questions(
        self,
        domain: str,
        count: Optional[int] = None,
        indices: Optional[List[int]] = None,
        start_row: int = 0,
        end_row: Optional[int] = None,
        default_format: str = "pdf",
        formats: Optional[Dict[int, str]] = None,
        output_dir: str = "./data",
        gt_only: bool = False,
        save_qa_file: bool = True,
        max_workers: int = 6
    ) -> Dict[str, Any]:
        """
        Selects questions (by count, range start_row..end_row, or specific indices)
        and downloads ONLY the documents required to answer those questions.
        Supports selecting 'pdf' or 'image' format per question or as default.
        """
        data = self.get_split_table(domain, output_dir)
        total_available = len(data["question"])

        # Determine indices
        if indices is not None:
            chosen_indices = [idx for idx in indices if 0 <= idx < total_available]
        elif end_row is not None:
            chosen_indices = [i for i in range(start_row, min(end_row, total_available))]
        elif count is not None:
            count = max(1, count)
            chosen_indices = [i for i in range(start_row, min(start_row + count, total_available))]
        else:
            chosen_indices = [start_row] if start_row < total_available else [0]

        questions = []
        unique_doc_ids = set()
        pdf_docs_to_extract = set()
        images_to_download = set()

        for idx in chosen_indices:
            q_text = data["question"][idx]
            ans_text = data["answer"][idx]
            q_type = data["question_type"][idx]
            a_type = data["answer_type"][idx]
            gt_imgs = data["gt_image_paths"][idx]
            longdoc_imgs = data["longdoc_image_paths"][idx]

            doc_id = "unknown"
            if longdoc_imgs and len(longdoc_imgs[0].split("/")) >= 3:
                doc_id = longdoc_imgs[0].split("/")[2]
            elif gt_imgs and len(gt_imgs[0].split("/")) >= 3:
                doc_id = gt_imgs[0].split("/")[2]
            unique_doc_ids.add(doc_id)

            # Determine format for this question: "pdf" or "image"
            q_fmt = (formats.get(idx, default_format) if formats else default_format).lower()
            if q_fmt not in ("pdf", "image"):
                q_fmt = "pdf"

            needed_imgs = gt_imgs if gt_only else longdoc_imgs

            if q_fmt == "pdf":
                pdf_docs_to_extract.add(doc_id)
            else:
                for img in needed_imgs:
                    images_to_download.add(img)

            questions.append({
                "question_idx": idx,
                "document_id": doc_id,
                "format": q_fmt,
                "question": q_text,
                "answer": ans_text,
                "question_type": q_type,
                "answer_type": a_type,
                "gt_image_paths_rel": gt_imgs,
                "longdoc_image_paths_rel": longdoc_imgs,
                "required_images_rel": needed_imgs,
                "local_images": [],
                "local_pdf": None,
                "local_files": []
            })

        # 1. Extract required PDFs
        doc_pdf_map = {}
        for doc_id in pdf_docs_to_extract:
            try:
                pdf_path = self.extract_document_pdf(domain, doc_id, output_dir)
                doc_pdf_map[doc_id] = pdf_path
            except Exception as e:
                print(f"⚠️ Error extracting PDF for doc {doc_id}: {e}")

        # 2. Download required images (if any questions use images)
        downloaded_img_map = {}
        image_list = sorted(list(images_to_download))

        def _fetch(img_rel):
            dest = os.path.join(output_dir, img_rel)
            saved = self._download_file(img_rel, dest)
            return img_rel, os.path.abspath(saved)

        if image_list:
            print(f"[*] Downloading {len(image_list)} page images for image-based questions...")
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_fetch, img) for img in image_list]
                for f in as_completed(futures):
                    k, v = f.result()
                    downloaded_img_map[k] = v

        # 3. Populate local files for each question
        for q in questions:
            doc_id = q["document_id"]
            if q["format"] == "pdf":
                pdf_file = doc_pdf_map.get(doc_id)
                q["local_pdf"] = pdf_file
                q["local_files"] = [pdf_file] if pdf_file else []
            else:
                q_imgs = [downloaded_img_map[img] for img in q["required_images_rel"] if img in downloaded_img_map]
                q["local_images"] = q_imgs
                q["local_files"] = q_imgs

            q["gt_local_images"] = [
                downloaded_img_map[img] for img in q["gt_image_paths_rel"] if img in downloaded_img_map
            ]

        result = {
            "domain": domain,
            "num_questions": len(questions),
            "indices": chosen_indices,
            "unique_document_ids": sorted(list(unique_doc_ids)),
            "format_breakdown": {
                "pdf_questions": [q["question_idx"] for q in questions if q["format"] == "pdf"],
                "image_questions": [q["question_idx"] for q in questions if q["format"] == "image"]
            },
            "total_pdfs": len(doc_pdf_map),
            "total_images": len(image_list),
            "total_pages_downloaded": len(image_list),
            "questions": questions
        }

        if save_qa_file:
            meta_dir = os.path.join(output_dir, domain, "metadata")
            os.makedirs(meta_dir, exist_ok=True)
            qa_file = os.path.join(meta_dir, f"selected_questions_{len(questions)}.json")
            with open(qa_file, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            result["qa_file"] = os.path.abspath(qa_file)

            # Also update selected_questions.json
            default_file = os.path.join(meta_dir, "selected_questions.json")
            with open(default_file, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)

        return result

    def download_split_images(
        self,
        domain: str,
        output_dir: str = "./data",
        max_workers: int = 8
    ) -> List[str]:
        """
        Downloads ONLY the images belonging to this domain (e.g. 1,671 images for commerce_manufacturing).
        Excludes the other 13,500+ images from other domains.
        """
        data = self.get_split_table(domain, output_dir)
        all_images = set()
        for doc_images in data.get("longdoc_image_paths", []):
            for img in doc_images:
                all_images.add(img)

        image_list = sorted(list(all_images))
        downloaded = []

        print(f"Downloading {len(image_list)} images for domain '{domain}'...")

        def _task(rel_path):
            dest = os.path.join(output_dir, rel_path)
            return self._download_file(rel_path, dest)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_task, p): p for p in image_list}
            if tqdm:
                for f in tqdm(as_completed(futures), total=len(futures), desc=f"Downloading {domain} images"):
                    downloaded.append(f.result())
            else:
                for idx, f in enumerate(as_completed(futures), 1):
                    downloaded.append(f.result())
                    if idx % 50 == 0 or idx == len(futures):
                        print(f"  [{idx}/{len(futures)}] downloaded...")

        return downloaded

    def download_domain_pdfs(
        self,
        domain: str,
        output_dir: str = "./data",
        extract: bool = False
    ) -> str:
        """
        Downloads ONLY the PDF archive for the specified domain (e.g. commerce_manufacturing_pdfs.tar.gz).
        Does NOT download PDF archives for any other domain.
        """
        archive_name = f"{domain}_pdfs.tar.gz"
        dest_dir = os.path.join(output_dir, domain, "pdfs")
        dest_path = os.path.join(dest_dir, archive_name)
        
        print(f"Downloading {archive_name} to {dest_path}...")
        self._download_file(archive_name, dest_path)
        
        if extract:
            import tarfile
            print(f"Extracting {dest_path}...")
            with tarfile.open(dest_path, "r:gz") as tar:
                tar.extractall(dest_dir)
            print("Extraction complete.")
            
        return dest_path

    def list_questions(
        self,
        domain: str,
        count: int = 10,
        start_row: int = 0,
        output_dir: str = "./data"
    ) -> List[Dict[str, Any]]:
        """
        Lists available questions in the domain with metadata.
        """
        data = self.get_split_table(domain, output_dir)
        total = len(data["question"])
        end_row = min(start_row + count, total)
        items = []
        for idx in range(start_row, end_row):
            longdoc = data["longdoc_image_paths"][idx]
            doc_id = longdoc[0].split("/")[2] if longdoc and len(longdoc[0].split("/")) >= 3 else "unknown"
            items.append({
                "row_idx": idx,
                "question": data["question"][idx],
                "answer": data["answer"][idx],
                "document_id": doc_id,
                "page_count": len(longdoc),
                "gt_pages": data["gt_image_paths"][idx],
                "question_type": data["question_type"][idx]
            })
        return items

    def download_from_url(
        self,
        url_or_id: str,
        output_dir: str = "./data",
        num_questions: Optional[int] = None,
        gt_only: bool = False,
        all_split_images: bool = False,
        include_pdfs: bool = False,
        extract_pdfs: bool = False
    ) -> Dict[str, Any]:
        """
        Main entrypoint: parses the HuggingFace URL and downloads ONLY the required files.
        """
        parsed = parse_hf_viewer_url(url_or_id)
        domain = parsed["split"] or "commerce_manufacturing"
        row_idx = parsed["row"] or 0

        results = {
            "parsed_info": parsed,
            "domain": domain,
            "output_dir": os.path.abspath(output_dir)
        }

        # 1. Download parquet metadata for this split
        parquet_path = self.download_split_parquet(domain, output_dir)
        results["parquet_file"] = os.path.abspath(parquet_path)

        # 2. Download selected number of questions or single row
        if num_questions is not None and not all_split_images:
            q_results = self.download_questions(
                domain=domain,
                count=num_questions,
                start_row=row_idx,
                output_dir=output_dir,
                gt_only=gt_only
            )
            results["questions_data"] = q_results
        elif row_idx is not None and not all_split_images:
            row_result = self.download_row(
                domain=domain,
                row_idx=row_idx,
                output_dir=output_dir,
                gt_only=gt_only
            )
            results["row_data"] = row_result

        # 3. If all split images were requested
        if all_split_images:
            images = self.download_split_images(domain, output_dir)
            results["downloaded_images_count"] = len(images)

        # 4. If domain PDFs were requested
        if include_pdfs:
            pdf_path = self.download_domain_pdfs(domain, output_dir, extract=extract_pdfs)
            results["pdf_archive"] = os.path.abspath(pdf_path)

        return results
