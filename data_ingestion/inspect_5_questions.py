"""
Comprehensive Verification & Deep Inspection of the 5 Benchmark Questions in Multimodal RAG.

Inspects:
- Question & Ground Truth Answer
- Redis In-Memory Cache Lookup & Latency
- Top-K Retrieved Chunks from OpenSearch k-NN
- Chunk Type (Visual Figure vs Text/Table)
- Neural Figure Classification (DocumentFigureClassifier-v2.5)
- Deterministic S3 URL (s3:// and https://)
- Full Context & Text Snippets passed into the RAG
"""

import os
import sys
import json
import time
from pathlib import Path

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_ingestion.query_engine import RAGQueryEngine


def main():
    qa_path = Path(__file__).resolve().parent / "raw_documents" / "metadata" / "selected_questions_5.json"
    if not qa_path.exists():
        print(f"[!] QA metadata file not found at {qa_path}")
        return

    with open(qa_path, "r", encoding="utf-8") as f:
        qa_data = json.load(f)

    questions = qa_data.get("questions", [])

    print("=" * 80)
    print("🔬 MULTIMODAL RAG DEEP INSPECTION: 5 BENCHMARK QUESTIONS")
    print("=" * 80)
    print(f"Total Questions to inspect: {len(questions)}")
    print("Vector Store             : OpenSearch 2.18 (multimodal_rag_finance)")
    print("Embedding Model          : sentence-transformers/all-MiniLM-L6-v2 (384-dim)")
    print("In-Memory Question Cache : Redis 7 (localhost:6380)")
    print("Figure S3 Storage        : s3://multimodal-rag-data-nov14/figures/")
    print("=" * 80)

    engine = RAGQueryEngine()

    for idx, item in enumerate(questions):
        q_idx = item.get("question_idx", idx)
        doc_id = item.get("document_id")
        q_text = item.get("question")
        gt_answer = item.get("answer")
        q_type = item.get("question_type", "N/A")

        print("\n" + "#" * 80)
        print(f"📌 QUESTION #{q_idx + 1} (QID: {q_idx} | Document: {doc_id} | Type: {q_type})")
        print("#" * 80)
        print(f"❓ Question Text:")
        print(f"   \"{q_text}\"")
        print(f"\n🎯 Ground Truth Answer:")
        print(f"   \"{gt_answer}\"")

        # Ask RAG Engine
        resp = engine.ask(question=q_text, doc_filter=doc_id, top_k=2, search_mode="hybrid")

        print(f"\n⚡ Cache Status: [{resp['source'].upper()}] | Retrieval Latency: {resp['latency_ms']} ms")
        if resp.get("ttl_remaining"):
            print(f"   TTL Remaining : {resp['ttl_remaining']} seconds")

        print(f"\n📦 Retrieved Chunks for RAG Context ({len(resp['results'])} chunks):")
        print("-" * 80)

        for rank, chunk in enumerate(resp["results"], 1):
            c_id = chunk.get("chunk_id")
            c_type = chunk.get("chunk_type")
            score = chunk.get("score", 0.0)
            pages = chunk.get("page_numbers") or []
            page_str = f"Page {pages[0]}" if pages else "N/A"
            neural_class = chunk.get("neural_class")
            s3_url = chunk.get("image_s3_url")
            s3_uri = chunk.get("image_s3_uri")
            local_img = chunk.get("image_file")
            caption = chunk.get("caption")

            print(f"  [{rank}] Chunk ID : {c_id}")
            print(f"      Score    : {score:.4f} | Type: {c_type.upper()} | Location: {page_str}")

            if neural_class:
                print(f"      Figure   : Neural Class '{neural_class}' | Caption: {caption}")
            if s3_url:
                print(f"      S3 URL   : {s3_url}")
                print(f"      S3 URI   : {s3_uri}")
                print(f"      Local PNG: {local_img}")

            # Preview text received by LLM / RAG generator
            raw_text = chunk.get("text", "")
            lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
            preview = "\n      ".join(lines[:6])
            print(f"      RAG Context Content:")
            print(f"      {preview}")
            if len(lines) > 6:
                print(f"      ... [{len(lines) - 6} more line(s)]")
            print()

    print("=" * 80)
    print("✅ DEEP INSPECTION COMPLETED FOR ALL 5 QUESTIONS!")
    print("=" * 80)


if __name__ == "__main__":
    main()
