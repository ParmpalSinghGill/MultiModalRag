"""
End-to-End Multimodal Data Ingestion Pipeline with S3 Sync, Docling & Tokenizer Selection.

1. Fetches documents from AWS S3 (s3://multimodal-rag-data-nov14/raw/).
2. Converts & parses documents using Docling with layout, table, and figure awareness.
3. Crops high-resolution images of all charts/graphs and runs OCR to extract internal labels.
4. Generates layout-aware chunks sized according to your selected tokenizer:
    - 'text-embedding-3-small' / 'openai' (Default/Recommended: tiktoken cl100k_base, 8,191 max window)
    - 'amazon-titan-v2' (Amazon Titan Text Embeddings V2, 8,192 max window)
    - 'gemini' (Gemma SentencePiece 256k tokenizer, 2,048 max window)
    - 'sentence-transformers' (all-MiniLM-L6-v2, 220 tokens safe limit)
5. Saves processed chunks and cropped figures ready for vector indexing and multimodal RAG.
"""

import sys
import os
from pathlib import Path

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_ingestion.utils import ensure_cudnn_ld_path
from data_ingestion.s3_fetcher import S3DocumentFetcher
from data_ingestion.docling_processor import DoclingProcessor
from data_ingestion.llama_parse_processor import LlamaParseProcessor

# Resolve Linux cuDNN dynamic library paths
ensure_cudnn_ld_path()

# ==============================================================================
# ⚙️ INGESTION PIPELINE & TOKENIZER CONFIGURATION
# ==============================================================================
BUCKET_NAME = "multimodal-rag-data-nov14"
S3_PREFIX = "raw"           # 'raw' or 'router'
AWS_PROFILE = "rag-dev"
FETCH_FROM_S3 = False       # Set True to re-sync with S3, False to use cached local files

# 📄 1. PARSER ENGINE SELECTION:
# Choose which parser engine to run:
#   • "docling"     : Local IBM Docling with neural DocumentFigureClassifier-v2.5 + image cropping
#   • "llama_parse" : LlamaCloud v2 Parse API with LLAMA_INDEX_API_KEY
#   • "both"        : Run both side-by-side to compare outputs
PARSER_ENGINE = "llama_parse"      # Options: "docling", "llama_parse", "both"
LLAMA_PARSE_TIER = "cost_effective"  # Options: "cost_effective", "fast", "agentic"

# 🎛️ 2. TOKENIZER & TARGET EMBEDDING MODEL SELECTION:
# Choose which tokenizer to use for chunk size budgeting:
#   • "text-embedding-3-small" (Recommended: OpenAI 3 cl100k_base, 8,191 max window, 1536 dim)
#   • "amazon-titan-v2"        (Amazon Titan Text Embeddings V2, 8,192 max window, 1024 dim)
#   • "gemini"                 (Google Gemini / Gemma 256k SentencePiece tokenizer, 2048 max window)
#   • "sentence-transformers"  (Local all-MiniLM-L6-v2, 220 tokens max)
TOKENIZER_TYPE = "text-embedding-3-small"    # Selected: OpenAI text-embedding-3-small

# 📏 2. TOKEN CHUNK BUDGET:
#   • For OpenAI (text-embedding-3-small) / Amazon Titan V2: 500 - 1500 tokens (keeps large financial tables in a single chunk)
#   • For Gemini: 500 - 1000 tokens
#   • For Sentence-Transformers: 220 tokens max
MAX_TOKENS = 1000

# 🖼️ 3. MULTIMODAL FIGURE & GRAPH EXTRACTION:
# If True: crops every graph/chart/picture to disk as PNG, runs OCR to extract axis labels/numbers,
# and generates dedicated multimodal chunks with image path + captions.
EXTRACT_FIGURES = True
IMAGES_SCALE = 2.0           # 2x high resolution for crisp crops and accurate OCR

# 🔍 4. DEEP DEBUGGING MODE:
# If True: pauses after each stage (tables, pictures/figures, chunks) with interactive ENTER prompts
# and displays rich diagnostic breakdowns of extracted tables, neural classifications, and chunks.
# Set False to run uninterrupted at maximum throughput.
DEEP_DEBUG = True

# ⚡ 5. LLAMAPARSE CONTENT-HASH DISK CACHE:
# If True: caches parsed response on disk (keyed by document SHA-256 hash).
# If input file has not changed, loads immediately from disk (0 API calls, 0 cost).
# If input file changed or FORCE_REPARSE is True, calls LlamaCloud API and refreshes cache.
LLAMA_USE_CACHE = True
LLAMA_FORCE_REPARSE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DOCS_DIR = os.path.join(BASE_DIR, "raw_documents")
CHUNKS_DIR = os.path.join(BASE_DIR, "processed_chunks")
# ==============================================================================


def main():
    print("=" * 76)
    print("🚀 MULTIMODAL RAG INGESTION PIPELINE (DOCLING + LLAMAPARSE BENCHMARK)")
    print("=" * 76)
    print(f"Parser Engine          : {PARSER_ENGINE.upper()}")
    print(f"Deep Debug Mode        : {'ENABLED (Interactive pauses)' if DEEP_DEBUG else 'DISABLED (Continuous)'}")
    print(f"LlamaParse Disk Cache  : {'ENABLED (0-credit repeat runs)' if LLAMA_USE_CACHE else 'DISABLED'} (Force Reparse: {LLAMA_FORCE_REPARSE})")
    print(f"Selected Tokenizer     : {TOKENIZER_TYPE.upper()} (Max Tokens: {MAX_TOKENS})")
    print(f"Extract Figures/Graphs : {EXTRACT_FIGURES} (Resolution Scale: {IMAGES_SCALE}x)")
    print(f"Source S3 Bucket       : s3://{BUCKET_NAME}/{S3_PREFIX}/")
    print(f"Raw Documents Dir      : {RAW_DOCS_DIR}")
    print(f"Output Chunks Dir      : {CHUNKS_DIR}")
    print("-" * 76)

    # Step 1: Fetch documents from S3 (optional)
    if FETCH_FROM_S3:
        print("\n[*] Step 1: Syncing documents from S3...")
        fetcher = S3DocumentFetcher(
            bucket_name=BUCKET_NAME,
            prefix=S3_PREFIX,
            profile_name=AWS_PROFILE
        )
        fetcher.fetch_all_documents(target_dir=RAW_DOCS_DIR)
    else:
        print("\n[*] Step 1: Using local raw_documents cache (FETCH_FROM_S3=False).")

    docling_summary = None
    llama_summary = None

    # Step 2A: Process with Docling (DocumentFigureClassifier-v2.5)
    if PARSER_ENGINE in ("docling", "both"):
        print(f"\n[*] Step 2A: Processing documents with IBM Docling + DocumentFigureClassifier-v2.5...")
        docling_proc = DoclingProcessor(
            tokenizer_type=TOKENIZER_TYPE,
            max_tokens=MAX_TOKENS,
            extract_figures=EXTRACT_FIGURES,
            images_scale=IMAGES_SCALE,
            classify_figures=True,
            deep_debug=DEEP_DEBUG
        )
        docling_summary = docling_proc.process_all_documents(
            raw_docs_dir=RAW_DOCS_DIR,
            output_chunks_dir=CHUNKS_DIR
        )

        print("\n" + "=" * 76)
        print("📋 DOCLING INGESTION SUMMARY (with DocumentFigureClassifier-v2.5)")
        print("=" * 76)
        print(f"Active Tokenizer          : {docling_summary['tokenizer']['type'].upper()} (Limit: {docling_summary['tokenizer']['max_tokens']} tokens)")
        print(f"Total Documents Processed : {docling_summary['total_documents']}")
        print(f"Total Figures Extracted   : {docling_summary['total_figures']}")
        print(f"Total Chunks Generated    : {docling_summary['total_chunks']}\n")

        for doc_id, doc in docling_summary["documents"].items():
            print(f"📂 Document ID: {doc_id}")
            print(f"   • File         : {doc['filename']}")
            print(f"   • Pages        : {doc['page_count']}")
            print(f"   • Tables       : {doc['table_count']} table(s) extracted to Markdown")
            print(f"   • Figures      : {doc['figures_count']} picture(s)/graph(s) cropped & classified")
            print(f"   • Total Chunks : {doc['chunk_count']} multimodal chunks")
            print(f"   • Chunk File   : {doc['chunk_file']}")
            print()

        # Show Extracted Figures with DocumentFigureClassifier-v2.5 predictions
        all_figures = docling_summary.get("figures", [])
        graphs = [f for f in all_figures if f.get("type") == "chart_graph"]
        print("=" * 76)
        print(f"📊 DOCLING NEURAL CLASSIFIER BREAKDOWN ({len(graphs)} Graphs Confirmed)")
        print("=" * 76)
        for g in graphs[:6]:
            clf = g.get("classifier_v2_5", {})
            pred = clf.get("top_class", "unknown")
            conf = clf.get("confidence", 0.0)
            print(f"• [{g['figure_id']}] Page {g['page_number']} | Neural Class: {pred} ({conf*100:.1f}% conf)")
            if g.get("caption"):
                print(f"  Caption : {g['caption']}")
            if g.get("image_file"):
                print(f"  Image   : {g['image_file']} (Size: {g['image_dimensions']})")
            if g.get("ocr_text"):
                clean_ocr = " ".join(g["ocr_text"].split()[:12])
                print(f"  OCR Data: {clean_ocr}...")
            print()

    # Step 2B: Process with LlamaParse v2 API
    if PARSER_ENGINE in ("llama_parse", "both"):
        print(f"\n[*] Step 2B: Processing documents with LlamaParse v2 API (Tier: {LLAMA_PARSE_TIER})...")
        llama_out_dir = os.path.join(CHUNKS_DIR, "llama_parse")
        try:
            llama_proc = LlamaParseProcessor(
                tier=LLAMA_PARSE_TIER,
                tokenizer_type=TOKENIZER_TYPE,
                max_tokens=MAX_TOKENS,
                deep_debug=DEEP_DEBUG,
                use_cache=LLAMA_USE_CACHE,
                force_reparse=LLAMA_FORCE_REPARSE
            )
            llama_summary = llama_proc.process_all_documents(
                raw_docs_dir=RAW_DOCS_DIR,
                output_chunks_dir=llama_out_dir
            )

            print("\n" + "=" * 76)
            print(f"📋 LLAMAPARSE v2 INGESTION SUMMARY (Tier: {LLAMA_PARSE_TIER})")
            print("=" * 76)
            print(f"Active Tokenizer          : {llama_summary['tokenizer']['type'].upper()} (Limit: {llama_summary['tokenizer']['max_tokens']} tokens)")
            print(f"Total Documents Processed : {llama_summary['total_documents']}")
            print(f"Total Tables Extracted    : {llama_summary['total_tables']}")
            print(f"Total Chunks Generated    : {llama_summary['total_chunks']}\n")

            for doc_id, doc in llama_summary["documents"].items():
                print(f"📂 Document ID: {doc_id}")
                print(f"   • File         : {doc['filename']}")
                print(f"   • Pages        : {doc['page_count']}")
                print(f"   • Tables       : {doc['table_count']} structured table(s)")
                print(f"   • Chunks       : {doc['chunk_count']} semantic chunks")
                print(f"   • Chunk File   : {doc['chunk_file']}")
                print()
        except Exception as e:
            print(f"[!] LlamaParse error: {e}")

    # Step 3: Comparative Breakdown
    if docling_summary and llama_summary:
        print("=" * 76)
        print("⚖️ PARSER COMPARISON: DOCLING vs LLAMAPARSE")
        print("=" * 76)
        print(f"{'Metric':<28} | {'Docling + Classifier v2.5':<25} | {'LlamaParse v2'}")
        print("-" * 76)
        print(f"{'Processing Location':<28} | {'Local GPU (Offline)':<25} | {'Cloud API'}")
        print(f"{'Total Chunks':<28} | {docling_summary['total_chunks']:<25} | {llama_summary['total_chunks']}")
        print(f"{'Cropped Figures/Images':<28} | {docling_summary['total_figures']:<25} | {'N/A (Markdown embeds)'}")
        print(f"{'Neural Figure Classifier':<28} | {'DocumentFigureClassifier-v2.5':<25} | {'Built-in'}")
        print(f"{'Tokenizer Window':<28} | {f'{MAX_TOKENS} tokens':<25} | {f'{MAX_TOKENS} tokens'}")
        print("=" * 76)

    print("=" * 76)
    print(f"✅ Ingestion complete! Master manifest saved to:")
    print(f"   {CHUNKS_DIR}/all_chunks.json")
    print(f"   Cropped figures saved in: {CHUNKS_DIR}/figures/")
    print("=" * 76)


if __name__ == "__main__":
    main()
