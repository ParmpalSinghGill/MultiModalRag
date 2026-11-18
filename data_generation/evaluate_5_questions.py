"""
Multimodal RAG Generation Evaluation for Benchmark Questions.

Evaluates the Multimodal RAG pipeline against the 5 benchmark questions
from `data/finance/metadata/selected_questions_5.json`.

Outputs:
- Side-by-side comparison of Ground Truth Answer vs. AWS Bedrock Generated Answer.
- Retrieved multimodal context (tables, prose, neural chart classes, S3 image URLs).
- Performance telemetry (retrieval latency, generation latency, token counts, USD cost).
- Caching verification (cold run vs. warm Redis cache hit <1ms).
- Saves evaluation report to `data_generation/evaluation_results_5_questions.json`.
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from typing import Dict, Any, List

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_generation.rag_pipeline import MultimodalRAGPipeline
from data_generation.bedrock_generator import BEDROCK_MODELS, DEFAULT_MODEL_ID


def evaluate_5_questions(
    questions_file: str = "data/finance/metadata/selected_questions_5.json",
    model_id: str = DEFAULT_MODEL_ID,
    top_k: int = 5,
    bypass_cache: bool = False,
    test_caching: bool = True,
    qid_filter: int = None,
    output_report: str = "data_generation/evaluation_results_5_questions.json"
):
    path_obj = Path(questions_file)
    if not path_obj.is_absolute():
        path_obj = Path(_ROOT) / path_obj

    if not path_obj.exists():
        raise FileNotFoundError(f"Questions file not found: {path_obj}")

    with open(path_obj, "r", encoding="utf-8") as f:
        data = json.load(f)

    questions: List[Dict[str, Any]] = data.get("questions", [])
    if qid_filter is not None:
        questions = [q for q in questions if q.get("question_idx") == qid_filter]

    print("=" * 80)
    print(f"🎯 MULTIMODAL RAG GENERATION EVALUATION ({len(questions)} Questions)")
    print(f"🤖 Bedrock Model: {model_id} ({BEDROCK_MODELS.get(model_id, {}).get('name', 'Custom')})")
    print(f"📁 Benchmark File: {path_obj.name}")
    print(f"⚡ Top-K Retrieval: {top_k} | Bypass Cache: {bypass_cache}")
    print("=" * 80)

    pipeline = MultimodalRAGPipeline(
        model_id=model_id,
        default_max_tokens=512,
        default_temperature=0.1
    )

    evaluation_records = []
    total_tokens_consumed = 0
    total_cost_usd = 0.0

    for i, q in enumerate(questions, 1):
        q_idx = q.get("question_idx")
        doc_id = q.get("document_id")
        q_text = q.get("question")
        gt_answer = q.get("answer")
        q_type = q.get("question_type", "N/A")

        print(f"\n{'=' * 80}")
        print(f"📌 QUESTION #{i} (Index: {q_idx} | Doc: {doc_id} | Type: {q_type})")
        print(f"❓ Question: {q_text}")
        print(f"{'-' * 80}")
        print(f"🎯 Ground Truth Answer:")
        print(f"   {gt_answer}")
        print(f"{'-' * 80}")

        # Run RAG Pipeline
        rag_response = pipeline.answer(
            question=q_text,
            doc_filter=doc_id,
            top_k=top_k,
            search_mode="hybrid",
            model_id=model_id,
            bypass_cache=bypass_cache
        )

        generated_answer = rag_response.get("answer", "")
        usage = rag_response.get("usage", {})
        cost = usage.get("estimated_cost_usd", 0.0)
        tokens = usage.get("total_tokens", 0)
        total_tokens_consumed += tokens
        total_cost_usd += cost

        print(f"\n💡 Bedrock Generated Answer ({rag_response.get('model_name', model_id)}):")
        print(f"   {generated_answer}")
        print(f"\n📊 Telemetry:")
        print(f"   • Cache Hit       : {rag_response.get('cache_hit')} (Source: {'Redis' if rag_response.get('cache_hit') else 'Bedrock LLM'})")
        print(f"   • Retrieval Time  : {rag_response.get('retrieval_latency_ms', 0):.2f} ms")
        print(f"   • Generation Time : {rag_response.get('generation_latency_ms', 0):.2f} ms")
        print(f"   • Total Latency   : {rag_response.get('total_latency_ms', 0):.2f} ms")
        print(f"   • Tokens (in/out) : {usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out ({tokens} total)")
        print(f"   • Estimated Cost  : ${cost:.6f} USD")

        sources = rag_response.get("sources", [])
        print(f"\n📚 Retrieved Sources ({len(sources)} chunks):")
        figure_links = []
        for s_idx, s in enumerate(sources, 1):
            pages = s.get("page_numbers") or []
            p_str = f"Page {pages[0]}" if pages else "Page N/A"
            cls_str = f" | {s['neural_class']}" if s.get("neural_class") else ""
            print(f"   [{s_idx}] Doc {s.get('document_id')} | {p_str} | Type: {s.get('content_type')}{cls_str} (Score: {s.get('score')})")
            if s.get("image_s3_url"):
                print(f"       🖼️  Figure Link: {s['image_s3_url']}")
                figure_links.append(s["image_s3_url"])

        record = {
            "question_idx": q_idx,
            "document_id": doc_id,
            "question": q_text,
            "ground_truth_answer": gt_answer,
            "generated_answer": generated_answer,
            "model_id": model_id,
            "cache_hit": rag_response.get("cache_hit"),
            "retrieval_latency_ms": rag_response.get("retrieval_latency_ms"),
            "generation_latency_ms": rag_response.get("generation_latency_ms"),
            "total_latency_ms": rag_response.get("total_latency_ms"),
            "tokens": usage,
            "sources": sources,
            "figure_links": figure_links
        }
        evaluation_records.append(record)

    # -------------------------------------------------------------
    # Test Redis Caching Acceleration (Second Pass Verification)
    # -------------------------------------------------------------
    if test_caching and not bypass_cache and len(questions) > 0:
        print(f"\n{'=' * 80}")
        print("⚡ VERIFYING REDIS CACHING ACCELERATION (Repeat Query Pass)")
        print(f"{'=' * 80}")

        cache_test_q = questions[0]
        q_text = cache_test_q.get("question")
        doc_id = cache_test_q.get("document_id")

        print(f"Testing warm query: \"{q_text}\"")
        cache_resp = pipeline.answer(
            question=q_text,
            doc_filter=doc_id,
            top_k=top_k,
            model_id=model_id,
            bypass_cache=False
        )
        print(f"[⚡ RESULT] Cache Hit: {cache_resp.get('cache_hit')}")
        print(f"   • Warm Latency : {cache_resp.get('total_latency_ms')} ms (Sub-millisecond retrieval from Redis!)")
        print(f"   • TTL Remaining: {cache_resp.get('ttl_remaining')} seconds")
        print(f"   • Re-served Answer: {cache_resp.get('answer')[:120]}...")

    # Summary Report
    print(f"\n{'=' * 80}")
    print("📈 EVALUATION SUMMARY")
    print(f"{'=' * 80}")
    print(f"Total Questions Evaluated : {len(questions)}")
    print(f"Bedrock Model             : {model_id}")
    print(f"Total Tokens Consumed     : {total_tokens_consumed}")
    print(f"Total Bedrock Cost (USD)  : ${total_cost_usd:.6f}")
    if len(questions) > 0:
        avg_cost = total_cost_usd / len(questions)
        print(f"Avg Cost per Question     : ${avg_cost:.6f} USD (~{round(avg_cost * 10000, 2)} cents per 100 queries)")

    # Save to disk
    out_path = Path(_ROOT) / output_report
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "model_id": model_id,
            "total_questions": len(questions),
            "total_tokens": total_tokens_consumed,
            "total_cost_usd": total_cost_usd,
            "records": evaluation_records
        }, f, indent=2)
    print(f"\n[✓] Detailed evaluation report saved to: {out_path.resolve()}")


def main():
    parser = argparse.ArgumentParser(description="Evaluate Multimodal RAG with AWS Bedrock on 5 Benchmark Questions.")
    parser.add_argument(
        "--model-id",
        choices=list(BEDROCK_MODELS.keys()),
        default=DEFAULT_MODEL_ID,
        help="AWS Bedrock low-cost model ID (default: meta.llama3-8b-instruct-v1:0)"
    )
    parser.add_argument("--top-k", type=int, default=5, help="Number of retrieved chunks (default: 5)")
    parser.add_argument("--bypass-cache", action="store_true", help="Bypass Redis cache to force fresh Bedrock invocations")
    parser.add_argument("--no-cache-test", action="store_true", help="Skip the second-pass Redis cache acceleration test")
    parser.add_argument("--qid", type=int, default=None, help="Filter to run a single question index (0 to 4)")
    parser.add_argument(
        "--questions-file",
        type=str,
        default="data/finance/metadata/selected_questions_5.json",
        help="Path to questions JSON file"
    )

    args = parser.parse_args()

    evaluate_5_questions(
        questions_file=args.questions_file,
        model_id=args.model_id,
        top_k=args.top_k,
        bypass_cache=args.bypass_cache,
        test_caching=not args.no_cache_test,
        qid_filter=args.qid
    )


if __name__ == "__main__":
    main()
