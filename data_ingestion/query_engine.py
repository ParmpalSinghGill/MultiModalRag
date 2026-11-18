"""
Multimodal RAG Query Engine with Redis Question Caching and OpenSearch Hybrid Retrieval.

Workflow:
1. Receives user or benchmark question (e.g. from selected_questions_2.json).
2. Normalizes question text and checks Redis Question Cache.
   - ⚡ CACHE HIT (<1ms): Returns cached multimodal context & S3 links immediately.
3. If CACHE MISS:
   - 🔍 OPENSEARCH RETRIEVAL: Executes hybrid k-NN vector + BM25 keyword search.
   - Caches the question & retrieved chunks in Redis (with TTL).
4. Returns multimodal context (prose snippets, tables, neural figure classes, and S3 image links).
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_ingestion.redis_question_cache import RedisQuestionCache
from data_ingestion.opensearch_indexer import OpenSearchIndexer


class RAGQueryEngine:
    """
    Unified Query Engine connecting OpenSearch k-NN Vector Search with Redis Question Cache.
    """

    def __init__(
        self,
        opensearch_host: str = "localhost",
        opensearch_port: int = 9200,
        index_name: str = "multimodal_rag_finance",
        redis_host: str = "localhost",
        redis_port: int = 6380,
        redis_db: int = 0,
        cache_ttl: int = 86400  # 24 hours
    ):
        print("=" * 76)
        print("🧠 INITIALIZING MULTIMODAL RAG QUERY ENGINE")
        print("=" * 76)

        # 1. Redis Question Cache
        self.cache = RedisQuestionCache(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            default_ttl=cache_ttl
        )

        # 2. OpenSearch Vector Store
        self.indexer = OpenSearchIndexer(
            host=opensearch_host,
            port=opensearch_port,
            index_name=index_name
        )

    def ask(
        self,
        question: str,
        doc_filter: Optional[str] = None,
        top_k: int = 3,
        search_mode: str = "hybrid",
        bypass_cache: bool = False
    ) -> Dict[str, Any]:
        """
        Asks a question: checks Redis cache first, falls back to OpenSearch on miss.
        """
        start_time = time.time()

        # Step 1: Check Redis Question Cache
        if not bypass_cache and self.cache.is_available():
            cached_result = self.cache.get(
                question=question,
                doc_filter=doc_filter,
                top_k=top_k,
                search_mode=search_mode
            )
            if cached_result is not None:
                elapsed_ms = (time.time() - start_time) * 1000
                print(f"[⚡ REDIS CACHE HIT ({elapsed_ms:.2f} ms)] Serving cached response for question: \"{question}\"")
                return {
                    "question": question,
                    "source": "redis_cache",
                    "cache_hit": True,
                    "latency_ms": round(elapsed_ms, 2),
                    "ttl_remaining": cached_result.get("ttl_remaining"),
                    "results": cached_result.get("results", [])
                }

        # Step 2: Cache Miss -> Query OpenSearch
        print(f"[🔍 OPENSEARCH RETRIEVAL (Cache Miss)] Executing {search_mode.upper()} search on index '{self.indexer.index_name}'...")
        if search_mode == "knn":
            search_hits = self.indexer.search_knn(query_text=question, top_k=top_k, doc_filter=doc_filter)
        elif search_mode == "bm25":
            search_hits = self.indexer.search_bm25(query_text=question, top_k=top_k, doc_filter=doc_filter)
        else:
            search_hits = self.indexer.search_hybrid(query_text=question, top_k=top_k, doc_filter=doc_filter)

        elapsed_ms = (time.time() - start_time) * 1000

        # Step 3: Populate Redis Cache for Future Queries
        if self.cache.is_available():
            self.cache.set(
                question=question,
                results=search_hits,
                doc_filter=doc_filter,
                top_k=top_k,
                search_mode=search_mode
            )
            print(f"[💾 REDIS CACHED] Stored question and {len(search_hits)} results with TTL {self.cache.default_ttl}s.")

        return {
            "question": question,
            "source": "opensearch",
            "cache_hit": False,
            "latency_ms": round(elapsed_ms, 2),
            "results": search_hits
        }

    def preload_dataset_questions(self, questions_json_path: str):
        """
        Preloads and answers dataset benchmark questions, caching them in Redis.
        """
        path_obj = Path(questions_json_path)
        if not path_obj.exists():
            print(f"[!] Question dataset file not found: {questions_json_path}")
            return

        with open(path_obj, "r", encoding="utf-8") as f:
            data = json.load(f)

        questions = data.get("questions", [])
        print(f"\n[*] Pre-caching {len(questions)} benchmark questions into Redis...")

        for item in questions:
            q_idx = item.get("question_idx")
            q_text = item.get("question")
            doc_id = item.get("document_id")
            gt_ans = item.get("answer")

            print(f"\n--- [QID {q_idx}] {q_text} (Doc: {doc_id}) ---")
            # Query and cache
            resp = self.ask(question=q_text, doc_filter=doc_id, top_k=3, search_mode="hybrid")

            # Also cache ground truth answer
            if gt_ans:
                self.cache.cache_qa_pair(
                    question=q_text,
                    answer=gt_ans,
                    doc_id=doc_id,
                    question_idx=q_idx
                )
                print(f"    • Ground truth answer cached: {gt_ans[:70]}...")

        stats = self.cache.get_stats()
        print(f"\n[✓] Preloading complete! Active Redis cached questions: {stats.get('active_cached_questions')}")


def main():
    parser = argparse.ArgumentParser(description="Multimodal RAG Query Engine with Redis Question Caching.")
    parser.add_argument("--question", "-q", type=str, help="Question to ask the RAG system")
    parser.add_argument("--doc-id", type=str, default=None, help="Optional document ID filter (e.g. 3152577 or 6002966)")
    parser.add_argument("--top-k", type=int, default=3, help="Number of chunks to retrieve")
    parser.add_argument("--mode", choices=["hybrid", "knn", "bm25"], default="hybrid", help="Search mode")
    parser.add_argument("--bypass-cache", action="store_true", help="Force fresh OpenSearch query without reading cache")
    parser.add_argument("--preload-dataset", action="store_true", help="Pre-load and cache benchmark questions from dataset")
    parser.add_argument("--stats", action="store_true", help="Show Redis cache telemetry stats")
    parser.add_argument("--clear-cache", action="store_true", help="Clear all cached questions in Redis")

    args = parser.parse_args()

    engine = RAGQueryEngine()

    if args.clear_cache:
        engine.cache.clear()
        return

    if args.stats:
        print("\n📊 REDIS QUESTION CACHE TELEMETRY:")
        print(json.dumps(engine.cache.get_stats(), indent=2))
        return

    if args.preload_dataset:
        meta_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "raw_documents",
            "metadata"
        )
        qa_file = os.path.join(meta_dir, "selected_questions_5.json")
        if not os.path.exists(qa_file):
            qa_file = os.path.join(meta_dir, "selected_questions_2.json")
        engine.preload_dataset_questions(qa_file)
        return

    question = args.question or "What is the predicted firm exit probability for 30-year-old founders?"

    print(f"\n[?] Asking: \"{question}\" (Filter: {args.doc_id or 'All Documents'}, Top-K: {args.top_k})")
    response = engine.ask(
        question=question,
        doc_filter=args.doc_id,
        top_k=args.top_k,
        search_mode=args.mode,
        bypass_cache=args.bypass_cache
    )

    print("\n" + "=" * 76)
    print(f"📋 QUERY RESULT [Source: {response['source'].upper()} | Latency: {response['latency_ms']} ms]")
    print("=" * 76)

    for i, r in enumerate(response["results"], 1):
        score = r.get("score", 0.0)
        chunk_id = r.get("chunk_id")
        doc_id = r.get("document_id")
        pages = r.get("page_numbers") or []
        page_str = f"Page {pages[0]}" if pages else "N/A"
        neural_cls = f" | Neural Class: {r['neural_class']}" if r.get("neural_class") else ""

        print(f"#{i} [{chunk_id}] (Score: {score:.4f}) - Doc {doc_id} ({page_str}){neural_cls}")

        if r.get("image_s3_url"):
            print(f"   🖼️  S3 Image URL : {r['image_s3_url']}")
            print(f"   📁 S3 URI       : {r.get('image_s3_uri')}")
            print(f"   💾 Local Image  : {r.get('image_file')}")

        clean_text = " ".join(r.get("text", "").split()[:40])
        print(f"   📝 Text Snippet : {clean_text}...\n")


if __name__ == "__main__":
    main()
