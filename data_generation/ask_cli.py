"""
Interactive Multimodal RAG CLI using AWS Bedrock & OpenSearch.

Usage examples:
  # Ask a specific question
  python data_generation/ask_cli.py --question "What is the NTA before tax for June 2021?" --doc-id 6002966

  # Interactive question answering loop
  python data_generation/ask_cli.py --interactive

  # Use ultra-low-cost Amazon Nova Micro
  python data_generation/ask_cli.py --model-id amazon.nova-micro-v1:0 --question "..."
"""

import sys
import argparse
from pathlib import Path

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_generation.rag_pipeline import MultimodalRAGPipeline
from data_generation.bedrock_generator import BEDROCK_MODELS, DEFAULT_MODEL_ID


def run_cli():
    parser = argparse.ArgumentParser(description="Multimodal RAG Inference CLI powered by AWS Bedrock.")
    parser.add_argument("--question", "-q", type=str, help="Question to ask")
    parser.add_argument("--doc-id", "-d", type=str, default=None, help="Optional document ID filter (e.g. 3152577, 6002966)")
    parser.add_argument("--model-id", "-m", choices=list(BEDROCK_MODELS.keys()), default=DEFAULT_MODEL_ID, help="Bedrock model ID")
    parser.add_argument("--top-k", "-k", type=int, default=4, help="Number of retrieved chunks")
    parser.add_argument("--interactive", "-i", action="store_true", help="Launch interactive Q&A REPL")
    parser.add_argument("--bypass-cache", action="store_true", help="Bypass Redis cache")

    args = parser.parse_args()

    pipeline = MultimodalRAGPipeline(model_id=args.model_id)

    if args.interactive:
        print("\n" + "=" * 70)
        print("💬 INTERACTIVE MULTIMODAL RAG REPL (Type 'exit' or 'quit' to leave)")
        print(f"🤖 Active Model: {args.model_id}")
        print("=" * 70)

        while True:
            try:
                user_q = input("\n📝 Enter your question: ").strip()
                if not user_q:
                    continue
                if user_q.lower() in ("exit", "quit", "q"):
                    print("Goodbye!")
                    break

                doc_id_in = input("📄 Document ID filter (press ENTER for all documents): ").strip() or None

                res = pipeline.answer(
                    question=user_q,
                    doc_filter=doc_id_in,
                    top_k=args.top_k,
                    model_id=args.model_id,
                    bypass_cache=args.bypass_cache
                )

                print("\n" + "=" * 70)
                print(f"💡 ANSWER [{res['model_name']} | Latency: {res['total_latency_ms']} ms | Cache Hit: {res['cache_hit']}]:")
                print("=" * 70)
                print(res["answer"])

                if res.get("sources"):
                    print("\n📚 Sources:")
                    for idx, s in enumerate(res["sources"], 1):
                        pages = s.get("page_numbers") or []
                        p_str = f"Page {pages[0]}" if pages else "N/A"
                        print(f"  [{idx}] Doc {s.get('document_id')} ({p_str}) [Score: {s.get('score')}]")
                        if s.get("image_s3_url"):
                            print(f"      🖼️  {s['image_s3_url']}")

                usage = res.get("usage", {})
                print(f"\n🪙 Cost: ${usage.get('estimated_cost_usd', 0.0):.6f} USD ({usage.get('total_tokens', 0)} tokens)")

            except (KeyboardInterrupt, EOFError):
                print("\nSession ended.")
                break
        return

    # Single-question mode
    question = args.question or "What is the variation in revenue growth between younger and senior business founders during the early years for those in the 2013 group?"
    doc_id = args.doc_id or "3152577"

    print(f"\n[?] Asking: \"{question}\" (Filter: {doc_id or 'All'})")
    res = pipeline.answer(
        question=question,
        doc_filter=doc_id,
        top_k=args.top_k,
        model_id=args.model_id,
        bypass_cache=args.bypass_cache
    )

    print("\n" + "=" * 70)
    print(f"💡 ANSWER [{res['model_name']} | Latency: {res['total_latency_ms']} ms | Cache Hit: {res['cache_hit']}]:")
    print("=" * 70)
    print(res["answer"])

    if res.get("sources"):
        print("\n📚 Sources:")
        for idx, s in enumerate(res["sources"], 1):
            pages = s.get("page_numbers") or []
            p_str = f"Page {pages[0]}" if pages else "N/A"
            print(f"  [{idx}] Doc {s.get('document_id')} ({p_str}) [Score: {s.get('score')}]")
            if s.get("image_s3_url"):
                print(f"      🖼️  {s['image_s3_url']}")

    usage = res.get("usage", {})
    print(f"\n🪙 Cost: ${usage.get('estimated_cost_usd', 0.0):.6f} USD ({usage.get('total_tokens', 0)} tokens)")


if __name__ == "__main__":
    run_cli()
