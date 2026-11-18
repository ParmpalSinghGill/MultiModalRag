"""
Multimodal RAG Pipeline Orchestrator.

Combines:
1. Redis Question & Answer Cache (<1ms hit response).
2. OpenSearch Hybrid Vector + BM25 Retrieval across multimodal chunks.
3. AWS Bedrock Foundation Model Generative Synthesis (Meta Llama 3 8B, Nova Micro, etc.).

Ensures every response is factually grounded in retrieved tables, prose,
neural chart classifications, and S3 figure URLs.
"""

import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_ingestion.redis_question_cache import RedisQuestionCache
from data_ingestion.opensearch_indexer import OpenSearchIndexer
from data_generation.bedrock_generator import (
    BedrockGenerator,
    DEFAULT_MODEL_ID,
    BEDROCK_MODELS,
)


class MultimodalRAGPipeline:
    """
    End-to-end Multimodal RAG Pipeline uniting Caching, Retrieval, and Bedrock LLM Generation.
    """

    def __init__(
        self,
        opensearch_host: str = "localhost",
        opensearch_port: int = 9200,
        index_name: str = "multimodal_rag_finance",
        redis_host: str = "localhost",
        redis_port: int = 6380,
        redis_db: int = 0,
        cache_ttl: int = 86400,  # 24 hours
        model_id: str = DEFAULT_MODEL_ID,
        profile_name: str = "rag-dev",
        region_name: str = "us-east-1",
        default_temperature: float = 0.1,
        default_max_tokens: int = 512,
        opensearch_client: Optional[Any] = None,
        redis_ssl: bool = False,
        redis_password: Optional[str] = None,
    ):
        print("=" * 76)
        print("🚀 INITIALIZING MULTIMODAL RAG PIPELINE")
        print("=" * 76)

        # 1. Redis Cache
        self.cache = RedisQuestionCache(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            default_ttl=cache_ttl,
            ssl=redis_ssl,
            password=redis_password,
        )

        # 2. OpenSearch Indexer & Retriever
        self.retriever = OpenSearchIndexer(
            host=opensearch_host,
            port=opensearch_port,
            index_name=index_name,
            client=opensearch_client,
        )

        # 3. AWS Bedrock Generator
        self.generator = BedrockGenerator(
            model_id=model_id,
            profile_name=profile_name,
            region_name=region_name,
            default_temperature=default_temperature,
            default_max_tokens=default_max_tokens,
        )

        print(f"[✓] Primary Bedrock Model: {self.generator.model_id} ({BEDROCK_MODELS.get(self.generator.model_id, {}).get('name', 'Custom')})")
        print(f"[✓] OpenSearch Index: {index_name} on {opensearch_host}:{opensearch_port}")
        print(f"[✓] Redis Cache: {redis_host}:{redis_port} (db: {redis_db})")
        print("=" * 76)

    def answer(
        self,
        question: str,
        doc_filter: Optional[str] = None,
        top_k: int = 4,
        search_mode: str = "hybrid",
        model_id: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        bypass_cache: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes end-to-end RAG question answering:
        1. Checks Redis cache for existing generated answer.
        2. On cache miss, executes OpenSearch hybrid retrieval.
        3. Formats multimodal context and synthesizes answer using AWS Bedrock.
        4. Caches response in Redis for subsequent instant recall.
        """
        overall_start = time.time()
        active_model = model_id or self.generator.model_id

        # ---------------------------------------------------------
        # Step 1: Redis Question/Answer Cache Check
        # ---------------------------------------------------------
        if not bypass_cache and self.cache.is_available():
            cached_gen = self.cache.get_generation(
                question=question,
                doc_filter=doc_filter,
                model_id=active_model,
            )
            if cached_gen is not None:
                elapsed_ms = (time.time() - overall_start) * 1000
                print(f"[⚡ REDIS ANSWER HIT ({elapsed_ms:.2f} ms)] Serving cached generation for: \"{question}\"")
                return {
                    "question": question,
                    "answer": cached_gen.get("answer", ""),
                    "model_id": active_model,
                    "model_name": BEDROCK_MODELS.get(active_model, {}).get("name", active_model),
                    "cache_hit": True,
                    "retrieval_latency_ms": 0.0,
                    "generation_latency_ms": 0.0,
                    "total_latency_ms": round(elapsed_ms, 2),
                    "ttl_remaining": cached_gen.get("ttl_remaining"),
                    "sources": cached_gen.get("sources", []),
                    "usage": cached_gen.get("usage", {}),
                    "doc_filter": doc_filter,
                }

        # ---------------------------------------------------------
        # Step 2: OpenSearch Hybrid Retrieval
        # ---------------------------------------------------------
        retrieval_start = time.time()
        print(f"[🔍 OPENSEARCH RETRIEVAL] Searching for: \"{question}\" (Filter: {doc_filter or 'All'}, Mode: {search_mode}, Top-K: {top_k})...")

        if search_mode == "knn":
            chunks = self.retriever.search_knn(query_text=question, top_k=top_k, doc_filter=doc_filter)
        elif search_mode == "bm25":
            chunks = self.retriever.search_bm25(query_text=question, top_k=top_k, doc_filter=doc_filter)
        else:
            chunks = self.retriever.search_hybrid(query_text=question, top_k=top_k, doc_filter=doc_filter)

        retrieval_latency_ms = (time.time() - retrieval_start) * 1000
        print(f"[✓] Retrieved {len(chunks)} chunks in {retrieval_latency_ms:.1f} ms.")

        # ---------------------------------------------------------
        # Step 3: AWS Bedrock LLM Answer Synthesis
        # ---------------------------------------------------------
        print(f"[🤖 BEDROCK GENERATION] Invoking model '{active_model}' with {len(chunks)} context chunks...")
        gen_result = self.generator.generate_answer(
            question=question,
            context_chunks=chunks,
            model_id=active_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        generation_latency_ms = gen_result.get("latency_ms", 0.0)
        total_latency_ms = (time.time() - overall_start) * 1000

        usage_info = {
            "input_tokens": gen_result.get("input_tokens", 0),
            "output_tokens": gen_result.get("output_tokens", 0),
            "total_tokens": gen_result.get("total_tokens", 0),
            "estimated_cost_usd": gen_result.get("estimated_cost_usd", 0.0),
        }

        # ---------------------------------------------------------
        # Step 4: Cache Generation in Redis
        # ---------------------------------------------------------
        if self.cache.is_available() and gen_result.get("error") is None:
            self.cache.set_generation(
                question=question,
                answer=gen_result.get("answer", ""),
                sources=gen_result.get("sources", []),
                doc_filter=doc_filter,
                model_id=active_model,
                usage=usage_info,
                latency_ms=round(total_latency_ms, 2),
            )
            # Also cache retrieval chunks
            self.cache.set(
                question=question,
                results=chunks,
                doc_filter=doc_filter,
                top_k=top_k,
                search_mode=search_mode,
            )
            print(f"[💾 REDIS CACHED] Answer and context stored in Redis (TTL: {self.cache.default_ttl}s).")

        return {
            "question": question,
            "answer": gen_result.get("answer", ""),
            "model_id": active_model,
            "model_name": gen_result.get("model_name", active_model),
            "cache_hit": False,
            "retrieval_latency_ms": round(retrieval_latency_ms, 2),
            "generation_latency_ms": round(generation_latency_ms, 2),
            "total_latency_ms": round(total_latency_ms, 2),
            "sources": gen_result.get("sources", []),
            "usage": usage_info,
            "doc_filter": doc_filter,
            "error": gen_result.get("error"),
        }
