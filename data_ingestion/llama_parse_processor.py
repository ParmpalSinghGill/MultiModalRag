"""
LlamaParse Processor with Content-Hash Caching & Deep Debugging Mode.

Uses LlamaCloud v2 Parse API with the configured LLAMA_INDEX_API_KEY.
Features:
- SHA-256 Content-Hash Caching: Caches parsed Markdown & tables on disk.
  Zero API calls / zero credit cost on subsequent runs unless the input document changes.
- Deep Debug Mode: Interactive inspection checkpoints for pages, tables, and chunks.
- Cloud-accelerated layout and table parsing (Fast / Cost Effective / Agentic tiers).
- Structured HTML / Markdown table extraction with CSV data.
- Local semantic markdown chunking budgeted for OpenAI text-embedding-3-small (or other tokenizers).
- Standardized chunk schema compatible with the Multimodal RAG pipeline.
"""

import sys
import os
import json
import re
import hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_ingestion.rag_tokenizers import create_tokenizer
from data_ingestion.utils import compute_file_hash, debug_pause, save_json, load_json

# Load environment variables (.env)
load_dotenv(os.path.join(_ROOT, ".env"))


class LlamaParseProcessor:
    """
    Parses documents using LlamaCloud / LlamaParse v2 API with disk caching and deep debugging.
    Extracts:
    - Clean Markdown per page
    - Structured tables with CSV and Markdown representation
    - Layout items (headings, tables, text)
    - Semantic chunks budgeted for text-embedding-3-small (or other tokenizers)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        tier: str = "cost_effective",
        tokenizer_type: str = "text-embedding-3-small",
        max_tokens: int = 1000,
        deep_debug: bool = False,
        use_cache: bool = True,
        force_reparse: bool = False,
        cache_dir: Optional[str] = None,
    ):
        self.api_key = (
            api_key
            or os.environ.get("LLAMA_INDEX_API_KEY")
            or os.environ.get("LLAMA_CLOUD_API_KEY")
        )
        if not self.api_key:
            raise ValueError(
                "LlamaParse API key not found! Please set LLAMA_INDEX_API_KEY or LLAMA_CLOUD_API_KEY in .env"
            )

        self.tier = tier
        self.tokenizer_type = tokenizer_type
        self.max_tokens = max_tokens
        self.deep_debug = deep_debug
        self.use_cache = use_cache
        self.force_reparse = force_reparse
        self.tokenizer = create_tokenizer(tokenizer_type=tokenizer_type, max_tokens=max_tokens)

        # Setup cache directory
        if cache_dir:
            self.cache_dir = cache_dir
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            self.cache_dir = os.path.join(base_dir, ".cache", "llama_parse")
        os.makedirs(self.cache_dir, exist_ok=True)

        # Lazy initialize LlamaCloud client on first actual API call to save overhead
        self._client = None
        print(f"[*] Initialized LlamaParse v2 Processor | Tier: {self.tier} | Tokenizer: {self.tokenizer_type}")
        print(f"    • Disk Cache : {'ENABLED' if self.use_cache else 'DISABLED'} (Dir: {self.cache_dir})")
        print(f"    • Deep Debug : {'ENABLED' if self.deep_debug else 'DISABLED'}")

    @property
    def client(self):
        if self._client is None:
            from llama_cloud import LlamaCloud
            self._client = LlamaCloud(api_key=self.api_key)
        return self._client

    def _get_cache_file_path(self, file_path: str, file_hash: str) -> str:
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        cache_filename = f"{base_name}_{file_hash[:16]}_{self.tier}.json"
        return os.path.join(self.cache_dir, cache_filename)

    def _debug_pause(self, stage_name: str, message: str = ""):
        """Helper to pause for user input only when deep_debug is enabled."""
        if not self.deep_debug:
            return
        should_continue = debug_pause(stage_name, message, tag="LLAMAPARSE DEEP DEBUG")
        if not should_continue:
            self.deep_debug = False

    def process_document(
        self,
        file_path: str,
        doc_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Submits a document to LlamaParse v2 API (or retrieves from disk cache)
        and generates structured chunks.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        document_id = doc_id or os.path.basename(os.path.dirname(file_path)) or "unknown"
        filename = os.path.basename(file_path)

        # 1. Check SHA-256 Disk Cache to avoid repeated API calls
        file_hash = compute_file_hash(file_path)
        cache_path = self._get_cache_file_path(file_path, file_hash)

        cached_data = None
        if self.use_cache and not self.force_reparse and os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                print(f"[⚡ CACHE HIT] Loaded LlamaParse cache for '{filename}' (SHA256: {file_hash[:10]}...) - 0 API credits used!")
            except Exception as e:
                print(f"[!] Warning reading cache ({e}), falling back to API.")
                cached_data = None

        if cached_data is not None:
            pages_markdown = cached_data.get("pages_markdown", [])
            full_markdown = cached_data.get("full_markdown", "")
            tables_data = cached_data.get("tables_data", [])
        else:
            # 2. Cache Miss: Make the Cloud API Call
            print(f"[☁️ CACHE MISS] File modified or no cache found for '{filename}'. Calling LlamaCloud v2 API [{self.tier}]...")

            with open(file_path, "rb") as f:
                result = self.client.parsing.parse(
                    upload_file=f,
                    tier=self.tier,
                    version="latest",
                    expand=["markdown", "items"],
                )

            # 2.1 Extract Page-level Markdown and full text
            pages_markdown = []
            full_markdown_parts = []
            if hasattr(result, "markdown") and result.markdown:
                for p in getattr(result.markdown, "pages", []):
                    md_text = getattr(p, "markdown", "") or ""
                    pages_markdown.append(md_text)
                    full_markdown_parts.append(md_text)

            full_markdown = "\n\n---\n\n".join(full_markdown_parts)

            # 2.2 Extract structured tables from items
            tables_data = []
            table_idx = 0
            if hasattr(result, "items") and result.items:
                for page in getattr(result.items, "pages", []):
                    p_num = getattr(page, "page_number", None)
                    for item in getattr(page, "items", []):
                        if getattr(item, "type", "") == "table":
                            csv_text = getattr(item, "csv", "") or ""
                            md_text = getattr(item, "md", "") or ""
                            html_text = getattr(item, "html", "") or ""
                            bbox_obj = getattr(item, "bbox", None)

                            tables_data.append({
                                "table_index": table_idx,
                                "page_number": p_num,
                                "csv": csv_text,
                                "markdown": md_text,
                                "html": html_text,
                                "bbox": str(bbox_obj) if bbox_obj else None
                            })
                            table_idx += 1

            # 2.3 Save to Cache File
            if self.use_cache:
                try:
                    cache_payload = {
                        "document_id": document_id,
                        "filename": filename,
                        "file_hash": file_hash,
                        "tier": self.tier,
                        "pages_markdown": pages_markdown,
                        "full_markdown": full_markdown,
                        "tables_data": tables_data
                    }
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(cache_payload, f, indent=2)
                    print(f"    💾 Saved parsed response to cache: {cache_path}")
                except Exception as e:
                    print(f"    ⚠️ Warning saving cache file: {e}")

        # [DEEP DEBUG] Checkpoint 1: Markdown Page Inspection
        if self.deep_debug:
            print("\n" + "=" * 76)
            print(f"🔍 [LLAMAPARSE DEEP DEBUG] CHECKPOINT 1: MARKDOWN PAGES FOR '{document_id}'")
            print("=" * 76)
            print(f"Total Pages Parsed: {len(pages_markdown)}")
            print(f"Total Character Length: {len(full_markdown)}")
            if pages_markdown:
                print(f"• Preview of Page 1 Markdown (First 15 lines):")
                print("-" * 76)
                p1_lines = pages_markdown[0].strip().split("\n")
                print("\n".join(p1_lines[:15]))
                if len(p1_lines) > 15:
                    print(f"... [{len(p1_lines) - 15} more lines]")
                print("-" * 76)
            self._debug_pause("Markdown Page Inspection", f"Inspected {len(pages_markdown)} parsed page(s). Ready for table inspection.")

        # [DEEP DEBUG] Checkpoint 2: Table Extraction Inspection
        if self.deep_debug:
            print("\n" + "=" * 76)
            print(f"🔍 [LLAMAPARSE DEEP DEBUG] CHECKPOINT 2: TABLES EXTRACTED FOR '{document_id}'")
            print("=" * 76)
            print(f"Total Structured Tables Detected: {len(tables_data)}")
            if tables_data:
                sample_tbl = tables_data[0]
                p_num = sample_tbl.get("page_number", 1)
                print(f"• Sample Table (Index {sample_tbl['table_index']} | Page {p_num}):")
                print("-" * 76)
                if sample_tbl.get("markdown"):
                    lines = sample_tbl["markdown"].strip().split("\n")
                    print("\n".join(lines[:12]))
                    if len(lines) > 12:
                        print(f"... [{len(lines) - 12} more rows in Markdown]")
                elif sample_tbl.get("csv"):
                    lines = sample_tbl["csv"].strip().split("\n")
                    print("\n".join(lines[:12]))
                print("-" * 76)
                if len(tables_data) > 1:
                    last_tbl = tables_data[-1]
                    print(f"• Latest / Last Table (Index {last_tbl['table_index']} | Page {last_tbl.get('page_number', 'N/A')}):")
                    if last_tbl.get("markdown"):
                        last_lines = last_tbl["markdown"].strip().split("\n")
                        print("\n".join(last_lines[:8]))
                    print("-" * 76)
            else:
                print("• No tables detected in this document.")
            self._debug_pause("Table Extraction Inspection", f"Inspected {len(tables_data)} table(s). Ready for chunking.")

        # 3. Local Semantic Markdown Chunking
        # Chunks are created page-by-page or by major section headings (# or ##)
        # while strictly respecting the max_tokens limit of the active tokenizer.
        structured_chunks = []
        chunk_counter = 0

        for page_idx, page_text in enumerate(pages_markdown):
            page_no = page_idx + 1
            if not page_text.strip():
                continue

            raw_sections = [s.strip() for s in re.split(r"\n(?=#{1,3}\s)", page_text) if s.strip()]

            current_chunk_text = ""
            current_headings = []

            for sec in raw_sections:
                h_match = re.match(r"^(#{1,3}\s+.+)", sec)
                if h_match:
                    current_headings.append(h_match.group(1).lstrip("#").strip())

                candidate = (current_chunk_text + "\n\n" + sec).strip() if current_chunk_text else sec
                cand_tok = self.tokenizer.count_tokens(candidate)

                if cand_tok <= self.max_tokens:
                    current_chunk_text = candidate
                else:
                    if current_chunk_text:
                        tok_count = self.tokenizer.count_tokens(current_chunk_text)
                        structured_chunks.append({
                            "chunk_id": f"{document_id}_llama_chunk_{chunk_counter:04d}",
                            "chunk_index": chunk_counter,
                            "document_id": document_id,
                            "chunk_type": "text_or_table",
                            "parser": "llama_parse_v2",
                            "text": current_chunk_text,
                            "token_count": tok_count,
                            "headings": list(set(current_headings)),
                            "page_numbers": [page_no],
                            "has_table": "<table" in current_chunk_text.lower() or "|" in current_chunk_text,
                            "has_image": False,
                            "char_length": len(current_chunk_text),
                            "word_count": len(current_chunk_text.split()),
                            "source_file": os.path.abspath(file_path)
                        })
                        chunk_counter += 1

                    # If section alone is still larger than max_tokens, split by paragraphs
                    sec_tok = self.tokenizer.count_tokens(sec)
                    if sec_tok > self.max_tokens:
                        paras = sec.split("\n\n")
                        sub_text = ""
                        for p in paras:
                            sub_cand = (sub_text + "\n\n" + p).strip() if sub_text else p
                            if self.tokenizer.count_tokens(sub_cand) <= self.max_tokens:
                                sub_text = sub_cand
                            else:
                                if sub_text:
                                    structured_chunks.append({
                                        "chunk_id": f"{document_id}_llama_chunk_{chunk_counter:04d}",
                                        "chunk_index": chunk_counter,
                                        "document_id": document_id,
                                        "chunk_type": "text_or_table",
                                        "parser": "llama_parse_v2",
                                        "text": sub_text,
                                        "token_count": self.tokenizer.count_tokens(sub_text),
                                        "headings": list(set(current_headings)),
                                        "page_numbers": [page_no],
                                        "has_table": "<table" in sub_text.lower() or "|" in sub_text,
                                        "has_image": False,
                                        "char_length": len(sub_text),
                                        "word_count": len(sub_text.split()),
                                        "source_file": os.path.abspath(file_path)
                                    })
                                    chunk_counter += 1
                                sub_text = p
                        current_chunk_text = sub_text
                    else:
                        current_chunk_text = sec

            # Flush trailing chunk on page
            if current_chunk_text.strip():
                tok_count = self.tokenizer.count_tokens(current_chunk_text)
                structured_chunks.append({
                    "chunk_id": f"{document_id}_llama_chunk_{chunk_counter:04d}",
                    "chunk_index": chunk_counter,
                    "document_id": document_id,
                    "chunk_type": "text_or_table",
                    "parser": "llama_parse_v2",
                    "text": current_chunk_text,
                    "token_count": tok_count,
                    "headings": list(set(current_headings)),
                    "page_numbers": [page_no],
                    "has_table": "<table" in current_chunk_text.lower() or "|" in current_chunk_text,
                    "has_image": False,
                    "char_length": len(current_chunk_text),
                    "word_count": len(current_chunk_text.split()),
                    "source_file": os.path.abspath(file_path)
                })
                chunk_counter += 1

        # [DEEP DEBUG] Checkpoint 3: Structured Chunks Inspection
        if self.deep_debug:
            print("\n" + "=" * 76)
            print(f"🔍 [LLAMAPARSE DEEP DEBUG] CHECKPOINT 3: STRUCTURED CHUNKS FOR '{document_id}'")
            print("=" * 76)
            print(f"Total Structured Chunks: {len(structured_chunks)}")
            if structured_chunks:
                sample_c = structured_chunks[0]
                print(f"• Sample Chunk [{sample_c['chunk_id']}] ({sample_c['token_count']} tokens | Page {sample_c['page_numbers']}):")
                print(f"   Headings : {sample_c['headings']}")
                print(f"   Has Table: {sample_c['has_table']}")
                print(f"   Preview  : {sample_c['text'][:250]}...")
            self._debug_pause("Chunk Generation Inspection", f"Completed document '{document_id}'. Ready to proceed.")

        print(f"    ✓ LlamaParse completed: {len(pages_markdown)} pages, {len(tables_data)} tables, {len(structured_chunks)} chunks.")

        return {
            "document_id": document_id,
            "filename": filename,
            "file_hash": file_hash,
            "parser": "llama_parse_v2",
            "tier": self.tier,
            "cached": cached_data is not None,
            "local_path": os.path.abspath(file_path),
            "page_count": len(pages_markdown),
            "table_count": len(tables_data),
            "total_chunks": len(structured_chunks),
            "tokenizer": {
                "type": self.tokenizer_type,
                "max_tokens": self.max_tokens
            },
            "markdown_text": full_markdown,
            "tables": tables_data,
            "chunks": structured_chunks
        }

    def process_all_documents(
        self,
        raw_docs_dir: str,
        output_chunks_dir: str
    ) -> Dict[str, Any]:
        """
        Processes all documents in raw_docs_dir using LlamaParse v2 API (with cache & deep debug).
        """
        os.makedirs(output_chunks_dir, exist_ok=True)
        manifest_file = os.path.join(raw_docs_dir, "ingestion_manifest.json")
        manifest_data = {}
        if os.path.exists(manifest_file):
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest_data = json.load(f)
            except Exception:
                pass

        all_doc_records = {}
        all_chunks = []
        all_tables = []

        doc_folders = [
            d for d in os.listdir(raw_docs_dir)
            if os.path.isdir(os.path.join(raw_docs_dir, d)) and d not in ("metadata", "__pycache__")
        ]

        print(f"[*] Processing {len(doc_folders)} document(s) with LlamaParse v2 [{self.tier}]...")

        for doc_id in sorted(doc_folders):
            doc_path = os.path.join(raw_docs_dir, doc_id)
            pdf_files = [f for f in os.listdir(doc_path) if f.lower().endswith(".pdf")]

            if pdf_files:
                target_file = os.path.join(doc_path, pdf_files[0])
            else:
                img_files = sorted([f for f in os.listdir(doc_path) if f.lower().endswith((".png", ".jpg", ".jpeg"))])
                if not img_files:
                    continue
                target_file = os.path.join(doc_path, img_files[0])

            doc_record = self.process_document(target_file, doc_id=doc_id)

            related_q = (
                manifest_data.get("documents", {}).get(doc_id, {}).get("related_questions", [])
            )
            doc_record["related_questions"] = related_q

            # Save individual document chunks JSON
            doc_chunk_file = os.path.join(output_chunks_dir, f"{doc_id}_llama_chunks.json")
            with open(doc_chunk_file, "w", encoding="utf-8") as f:
                json.dump(doc_record, f, indent=2)

            all_doc_records[doc_id] = {
                "document_id": doc_id,
                "filename": doc_record["filename"],
                "parser": "llama_parse_v2",
                "cached": doc_record.get("cached", False),
                "page_count": doc_record["page_count"],
                "table_count": doc_record["table_count"],
                "chunk_count": doc_record["total_chunks"],
                "chunk_file": os.path.abspath(doc_chunk_file)
            }

            all_chunks.extend(doc_record["chunks"])
            all_tables.extend(doc_record["tables"])

        # Save consolidated all_chunks.json
        master_manifest_path = os.path.join(output_chunks_dir, "llama_parse_all_chunks.json")
        master_summary = {
            "parser": "llama_parse_v2",
            "tier": self.tier,
            "tokenizer": {
                "type": self.tokenizer_type,
                "max_tokens": self.max_tokens
            },
            "total_documents": len(all_doc_records),
            "total_chunks": len(all_chunks),
            "total_tables": len(all_tables),
            "documents": all_doc_records,
            "tables": all_tables,
            "chunks": all_chunks
        }
        with open(master_manifest_path, "w", encoding="utf-8") as f:
            json.dump(master_summary, f, indent=2)

        return master_summary


if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.abspath(__file__))
    raw_dir = os.path.join(base_dir, "raw_documents")
    out_dir = os.path.join(base_dir, "processed_chunks", "llama_parse")

    processor = LlamaParseProcessor(tier="cost_effective", max_tokens=1000, deep_debug=True, use_cache=True)
    summary = processor.process_all_documents(raw_dir, out_dir)
    print("\n" + "=" * 70)
    print("✅ LLAMAPARSE INGESTION COMPLETE")
    print(f"Total Documents: {summary['total_documents']}")
    print(f"Total Chunks:    {summary['total_chunks']}")
    print(f"Total Tables:    {summary['total_tables']}")
    print(f"Saved to:        {out_dir}/llama_parse_all_chunks.json")
    print("=" * 70)
