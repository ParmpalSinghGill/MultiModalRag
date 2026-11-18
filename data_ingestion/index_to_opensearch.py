"""
Master OpenSearch Ingestion Pipeline for Multimodal RAG.

Pipeline Workflow:
1. Verifies OpenSearch Docker container running locally on http://localhost:9200.
2. Filters 1 or 2 documents (e.g., '3152577' and '6002966').
3. Generates or loads semantic chunks using local SentenceTransformer ('sentence-transformers/all-MiniLM-L6-v2', max 220 tokens).
4. Deduplicates and syncs figure/chart PNGs to AWS S3 (s3://multimodal-rag-data-nov14/figures/<doc_id>/):
   - Computes SHA-256 hash of each local image.
   - Compares with S3 object metadata.
   - SKIPS already-uploaded identical images (0 duplicate uploads, zero wasted storage).
   - Re-uploads only if image content changes.
   - Attaches deterministic S3 URLs (https://... and s3://...) to chunk metadata and text.
5. Ingests multimodal chunks (prose, tables, figures, S3 links) into OpenSearch k-NN index.
6. Runs vector and hybrid search retrieval to verify end-to-end functionality.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_ingestion.utils import ensure_cudnn_ld_path
from data_ingestion.s3_figure_uploader import S3FigureUploader
from data_ingestion.opensearch_indexer import OpenSearchIndexer
from data_ingestion.docling_processor import DoclingProcessor

ensure_cudnn_ld_path()

# ==============================================================================
# ⚙️ DEFAULT CONFIGURATION
# ==============================================================================
DEFAULT_DOC_IDS = ["3152577", "6002966", "5510314", "4119285", "6077605"]  # 5 Question Documents
DEFAULT_INDEX_NAME = "multimodal_rag_finance"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_TOKENS = 220  # Safe token window for all-MiniLM-L6-v2 (trained limit 256)
S3_BUCKET = "multimodal-rag-data-nov14"
S3_PREFIX = "figures"
AWS_PROFILE = "rag-dev"
# ==============================================================================


def run_pipeline(
    doc_ids: List[str] = DEFAULT_DOC_IDS,
    index_name: str = DEFAULT_INDEX_NAME,
    recreate_index: bool = False,
    rechunk: bool = False,
    max_tokens: int = MAX_TOKENS,
    base_dir: Optional[str] = None
):
    base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
    raw_docs_dir = os.path.join(base_dir, "raw_documents")
    chunks_dir = os.path.join(base_dir, "processed_chunks")
    figures_dir = os.path.join(chunks_dir, "figures")

    print("=" * 76)
    print("🚀 OPENSEARCH MULTIMODAL INGESTION PIPELINE")
    print("=" * 76)
    print(f"Target Documents       : {doc_ids}")
    print(f"OpenSearch Host        : http://localhost:9200")
    print(f"OpenSearch Index       : {index_name}")
    print(f"Embedding Model        : {EMBEDDING_MODEL} (384-dim, Max Tokens: {max_tokens})")
    print(f"S3 Figure Bucket       : s3://{S3_BUCKET}/{S3_PREFIX}/ (AWS Profile: {AWS_PROFILE})")
    print(f"Deduplication Mode     : SHA-256 Hash Matching (Zero Duplicate S3 Storage)")
    print("-" * 76)

    # --------------------------------------------------------------------------
    # Step 1: Initialize OpenSearch Indexer & Create k-NN Index
    # --------------------------------------------------------------------------
    print("\n[*] Step 1: Connecting to OpenSearch and setting up k-NN index...")
    indexer = OpenSearchIndexer(
        host="localhost",
        port=9200,
        index_name=index_name,
        embedding_model_name=EMBEDDING_MODEL
    )
    if not indexer.ping():
        print("[!] ERROR: Cannot connect to OpenSearch at http://localhost:9200.")
        print("    Ensure the Docker container 'multiformat-rag-opensearch' is running.")
        sys.exit(1)

    indexer.create_index(recreate=recreate_index)

    # --------------------------------------------------------------------------
    # Step 2: Initialize S3 Deduplicating Figure Uploader
    # --------------------------------------------------------------------------
    print("\n[*] Step 2: Initializing S3 Deduplicated Figure Uploader...")
    uploader = S3FigureUploader(
        bucket_name=S3_BUCKET,
        prefix=S3_PREFIX,
        profile_name=AWS_PROFILE
    )

    # --------------------------------------------------------------------------
    # Step 3: Chunk Documents (or Load Existing Chunks)
    # --------------------------------------------------------------------------
    print(f"\n[*] Step 3: Processing / Loading chunks for documents: {doc_ids}...")
    all_chunks_to_index = []

    for doc_id in doc_ids:
        chunk_file = os.path.join(chunks_dir, f"{doc_id}_chunks.json")
        doc_record = None

        need_reprocess = rechunk or not os.path.exists(chunk_file)

        if not need_reprocess:
            try:
                with open(chunk_file, "r", encoding="utf-8") as f:
                    doc_record = json.load(f)
                tok_info = doc_record.get("tokenizer", {})
                tok_type = tok_info.get("type", "")
                tok_max = tok_info.get("max_tokens", 1000)

                # If existing chunks were sized for > 250 tokens and user wants MiniLM, rechunk
                if tok_max > 250 and rechunk:
                    need_reprocess = True
                else:
                    print(f"  • Doc '{doc_id}': Loaded {len(doc_record.get('chunks', []))} cached chunks from {Path(chunk_file).name}")
            except Exception as e:
                print(f"  [!] Failed reading {chunk_file}: {e}. Reprocessing.")
                need_reprocess = True

        if need_reprocess:
            print(f"  • Doc '{doc_id}': Processing with Docling (sentence-transformers tokenizer, max {max_tokens} tokens)...")
            processor = DoclingProcessor(
                tokenizer_type="sentence-transformers",
                max_tokens=max_tokens,
                extract_figures=True,
                images_scale=2.0,
                classify_figures=True,
                deep_debug=False
            )
            raw_doc_folder = os.path.join(raw_docs_dir, doc_id)
            pdf_files = [f for f in os.listdir(raw_doc_folder) if f.lower().endswith(".pdf")]
            if not pdf_files:
                print(f"  [!] No PDF found in {raw_doc_folder}")
                continue
            target_pdf = os.path.join(raw_doc_folder, pdf_files[0])
            doc_record = processor.process_document(
                target_pdf,
                doc_id=doc_id,
                figures_output_dir=figures_dir
            )

        # ----------------------------------------------------------------------
        # Step 4: S3 Deduplicated Upload of Figures & Chunk Enrichment
        # ----------------------------------------------------------------------
        print(f"\n[*] Step 4: Deduplicating and syncing figures for Doc '{doc_id}' to S3...")
        doc_figures_dir = os.path.join(figures_dir, doc_id)
        sync_results = uploader.sync_document_figures(doc_id=doc_id, figures_dir=doc_figures_dir)

        # Enrich chunks with S3 URLs and SHA-256 hashes
        enriched_chunks = uploader.enrich_chunks_with_s3_links(
            doc_record.get("chunks", []),
            sync_results
        )
        doc_record["chunks"] = enriched_chunks

        # Update figure metadata in doc_record
        for fig in doc_record.get("figures", []):
            img_path = fig.get("image_file")
            if img_path:
                match = sync_results.get(Path(img_path).name) or sync_results.get(str(Path(img_path).resolve()))
                if match:
                    fig["s3_uri"] = match.get("s3_uri")
                    fig["s3_url"] = match.get("s3_url")
                    fig["sha256"] = match.get("sha256")

        # Save enriched chunks back to local disk
        with open(chunk_file, "w", encoding="utf-8") as f:
            json.dump(doc_record, f, indent=2)

        all_chunks_to_index.extend(enriched_chunks)

    # --------------------------------------------------------------------------
    # Step 5: OpenSearch Bulk Ingestion
    # --------------------------------------------------------------------------
    print(f"\n[*] Step 5: Ingesting {len(all_chunks_to_index)} multimodal chunks into OpenSearch index '{index_name}'...")
    ingest_stats = indexer.index_chunks(all_chunks_to_index)
    print(f"[✓] OpenSearch Ingestion Result: {ingest_stats['indexed']} indexed, {ingest_stats['errors']} errors.")

    # --------------------------------------------------------------------------
    # Step 6: Retrieval & Verification Tests for 5 Questions
    # --------------------------------------------------------------------------
    print("\n" + "=" * 76)
    print("🔍 STEP 6: VERIFICATION & RETRIEVAL TESTS FOR 5 BENCHMARK QUESTIONS")
    print("=" * 76)

    qa_file = os.path.join(raw_docs_dir, "metadata", "selected_questions_5.json")
    benchmark_questions = []
    if os.path.exists(qa_file):
        try:
            with open(qa_file, "r", encoding="utf-8") as f:
                q_meta = json.load(f)
            benchmark_questions = q_meta.get("questions", [])
        except Exception:
            pass

    if not benchmark_questions:
        benchmark_questions = [
            {"question_idx": 0, "document_id": "3152577", "question": "predicted probability of firm exit by age of founder", "answer": ""},
            {"question_idx": 1, "document_id": "6002966", "question": "pre-tax NTA performance and shares", "answer": ""}
        ]

    for q_item in benchmark_questions:
        q_idx = q_item.get("question_idx", 0)
        q_doc = q_item.get("document_id", "")
        q_text = q_item.get("question", "")
        gt_answer = q_item.get("answer", "")

        print(f"\n🔎 [QID {q_idx}] Target Doc: {q_doc}")
        print(f"   Question       : \"{q_text}\"")
        if gt_answer:
            print(f"   Expected Answer: \"{gt_answer[:100]}...\"")
        print("-" * 76)

        results = indexer.search_hybrid(q_text, top_k=2)

        if not results:
            print("  [!] No matches found.")
            continue

        for rank, r in enumerate(results, 1):
            chunk_type_badge = f"[{r['chunk_type'].upper()}]"
            pages = r.get("page_numbers") or []
            page_str = f"Page {pages[0]}" if pages else "N/A"
            neural_info = f" | Neural Class: {r['neural_class']}" if r.get("neural_class") else ""

            print(f"  #{rank} Score: {r['score']:.4f} | ID: {r['chunk_id']} | Doc: {r['document_id']} ({page_str}){neural_info}")

            # Display S3 Link if visual chunk
            if r.get("image_s3_url"):
                print(f"     🖼️  S3 Image URL : {r['image_s3_url']}")
                print(f"     📁 S3 URI       : {r['image_s3_uri']}")
                print(f"     💾 Local Image  : {r.get('image_file')}")

            # Display text snippet
            clean_text = " ".join(r.get("text", "").split()[:45])
            print(f"     📝 Text Snippet : {clean_text}...")
            print()

    print("=" * 76)
    print("✅ OPENSEARCH MULTIMODAL INGESTION & DEDUPLICATION PIPELINE COMPLETE!")
    print("=" * 76)


def main():
    parser = argparse.ArgumentParser(description="Ingest multimodal documents into local OpenSearch.")
    parser.add_argument("--doc-ids", nargs="+", default=DEFAULT_DOC_IDS, help="Document IDs to process/index")
    parser.add_argument("--index-name", default=DEFAULT_INDEX_NAME, help="OpenSearch index name")
    parser.add_argument("--recreate-index", action="store_true", help="Delete and recreate OpenSearch index")
    parser.add_argument("--rechunk", action="store_true", help="Re-chunk with sentence-transformers 220-token limit")
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS, help="Max tokens per chunk (default 220)")

    args = parser.parse_args()

    run_pipeline(
        doc_ids=args.doc_ids,
        index_name=args.index_name,
        recreate_index=args.recreate_index,
        rechunk=args.rechunk,
        max_tokens=args.max_tokens
    )


if __name__ == "__main__":
    main()
