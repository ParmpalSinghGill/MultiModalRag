"""
Redis Question and Query Cache for Multimodal RAG.

Features:
- Sub-millisecond (<1ms) in-memory retrieval for previously asked questions.
- Question text normalization (whitespace, case, punctuation) + SHA-256 caching key.
- Configurable TTL (default: 24 hours / 86400 seconds).
- Tracks cache telemetry: hit counts, miss counts, hit rates.
- Supports caching retrieval results, generated answers, and dataset QA pairs.
"""

import os
import sys
import json
import hashlib
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
import redis

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class RedisQuestionCache:
    """
    Manages in-memory question and query caching in Redis.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6380,
        db: int = 0,
        default_ttl: int = 86400,  # 24 hours
        key_prefix: str = "rag:qcache",
        ssl: bool = False,
        password: Optional[str] = None
    ):
        self.host = host
        self.port = port
        self.db = db
        self.default_ttl = default_ttl
        self.key_prefix = key_prefix.rstrip(":")
        self.ssl = ssl
        self.password = password

        self.client = None
        self._connect()

    def _connect(self):
        """Initializes connection to Redis."""
        try:
            self.client = redis.Redis(
                host=self.host,
                port=self.port,
                db=self.db,
                socket_timeout=5,
                decode_responses=True,
                ssl=self.ssl,
                password=self.password
            )
            self.client.ping()
            print(f"[✓] Connected to Redis Question Cache on {self.host}:{self.port} (db: {self.db})")
        except Exception as e:
            print(f"[!] Warning: Cannot connect to Redis on {self.host}:{self.port}: {e}")
            self.client = None

    def is_available(self) -> bool:
        """Returns True if Redis is reachable."""
        if self.client is None:
            return False
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def normalize_question(self, question: str) -> str:
        """Normalizes question string: lowercase, trims extra spaces and trailing punctuation."""
        clean = question.lower().strip()
        clean = re.sub(r"[^\w\s]", "", clean)
        clean = re.sub(r"\s+", " ", clean)
        return clean

    def make_cache_key(
        self,
        question: str,
        doc_filter: Optional[str] = None,
        top_k: int = 5,
        search_mode: str = "hybrid"
    ) -> str:
        """Generates deterministic cache key from normalized question and search parameters."""
        norm_q = self.normalize_question(question)
        filter_part = f"doc:{doc_filter}" if doc_filter else "doc:all"
        raw_key = f"{norm_q}|{filter_part}|top_k:{top_k}|mode:{search_mode}"
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        return f"{self.key_prefix}:{key_hash}"

    def make_generation_key(
        self,
        question: str,
        doc_filter: Optional[str] = None,
        model_id: str = "meta.llama3-8b-instruct-v1:0"
    ) -> str:
        """Generates deterministic cache key for end-to-end generated answers."""
        norm_q = self.normalize_question(question)
        filter_part = f"doc:{doc_filter}" if doc_filter else "doc:all"
        raw_key = f"gen|{norm_q}|{filter_part}|model:{model_id}"
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        return f"{self.key_prefix}:gen:{key_hash}"

    def get_generation(
        self,
        question: str,
        doc_filter: Optional[str] = None,
        model_id: str = "meta.llama3-8b-instruct-v1:0"
    ) -> Optional[Dict[str, Any]]:
        """Retrieves cached generated answer for a question and model."""
        if not self.is_available():
            return None

        cache_key = self.make_generation_key(question, doc_filter, model_id)
        try:
            cached_data = self.client.get(cache_key)
            if cached_data is not None:
                parsed = json.loads(cached_data)
                parsed["cache_hit"] = True
                parsed["cache_key"] = cache_key
                parsed["ttl_remaining"] = self.client.ttl(cache_key)
                self.client.incr(f"{self.key_prefix}:stats:hits")
                return parsed

            self.client.incr(f"{self.key_prefix}:stats:misses")
            return None
        except Exception as e:
            print(f"[!] Redis cache get_generation error: {e}")
            return None

    def set_generation(
        self,
        question: str,
        answer: str,
        sources: List[Dict[str, Any]],
        doc_filter: Optional[str] = None,
        model_id: str = "meta.llama3-8b-instruct-v1:0",
        usage: Optional[Dict[str, Any]] = None,
        latency_ms: Optional[float] = None,
        ttl: Optional[int] = None
    ) -> bool:
        """Caches end-to-end generated answer, source excerpts, and usage metadata."""
        if not self.is_available():
            return False

        cache_key = self.make_generation_key(question, doc_filter, model_id)
        expire_ttl = ttl or self.default_ttl
        payload = {
            "question": question,
            "answer": answer,
            "doc_filter": doc_filter,
            "model_id": model_id,
            "sources": sources,
            "usage": usage or {},
            "latency_ms": latency_ms,
            "ttl": expire_ttl
        }
        try:
            self.client.setex(cache_key, expire_ttl, json.dumps(payload))
            self.client.sadd(f"{self.key_prefix}:all_keys", cache_key)
            return True
        except Exception as e:
            print(f"[!] Redis cache set_generation error: {e}")
            return False

    def get(
        self,
        question: str,
        doc_filter: Optional[str] = None,
        top_k: int = 5,
        search_mode: str = "hybrid"
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieves cached search results or answer for a question.
        Returns dict with cached results and 'cache_hit': True if present, else None.
        """
        if not self.is_available():
            return None

        cache_key = self.make_cache_key(question, doc_filter, top_k, search_mode)
        try:
            cached_data = self.client.get(cache_key)
            if cached_data is not None:
                parsed = json.loads(cached_data)
                parsed["cache_hit"] = True
                parsed["cache_key"] = cache_key
                parsed["ttl_remaining"] = self.client.ttl(cache_key)

                # Increment telemetry hit counter
                self.client.incr(f"{self.key_prefix}:stats:hits")
                return parsed

            # Increment telemetry miss counter
            self.client.incr(f"{self.key_prefix}:stats:misses")
            return None
        except Exception as e:
            print(f"[!] Redis cache get error: {e}")
            return None

    def set(
        self,
        question: str,
        results: Any,
        doc_filter: Optional[str] = None,
        top_k: int = 5,
        search_mode: str = "hybrid",
        generated_answer: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        ttl: Optional[int] = None
    ) -> bool:
        """
        Caches question retrieval results and optional generated answer in Redis with TTL.
        """
        if not self.is_available():
            return False

        cache_key = self.make_cache_key(question, doc_filter, top_k, search_mode)
        expire_ttl = ttl or self.default_ttl

        payload = {
            "question": question,
            "normalized_question": self.normalize_question(question),
            "doc_filter": doc_filter,
            "top_k": top_k,
            "search_mode": search_mode,
            "results": results,
            "generated_answer": generated_answer,
            "metadata": metadata or {},
            "ttl": expire_ttl
        }

        try:
            serialized = json.dumps(payload)
            self.client.setex(cache_key, expire_ttl, serialized)

            # Record in questions set for tracking
            self.client.sadd(f"{self.key_prefix}:all_keys", cache_key)
            return True
        except Exception as e:
            print(f"[!] Redis cache set error: {e}")
            return False

    def cache_qa_pair(
        self,
        question: str,
        answer: str,
        doc_id: Optional[str] = None,
        question_idx: Optional[int] = None,
        ttl: Optional[int] = None
    ) -> bool:
        """Caches a verified QA ground truth pair or pre-computed answer."""
        if not self.is_available():
            return False

        cache_key = f"{self.key_prefix}:qa:{question_idx if question_idx is not None else hashlib.md5(question.encode()).hexdigest()[:8]}"
        expire_ttl = ttl or self.default_ttl

        payload = {
            "question_idx": question_idx,
            "question": question,
            "answer": answer,
            "document_id": doc_id,
            "source": "ground_truth_dataset"
        }

        try:
            self.client.setex(cache_key, expire_ttl, json.dumps(payload))
            # Also cache by question text for direct lookup
            self.set(
                question=question,
                results=[],
                doc_filter=doc_id,
                generated_answer=answer,
                metadata={"ground_truth": True, "question_idx": question_idx},
                ttl=expire_ttl
            )
            return True
        except Exception as e:
            print(f"[!] Failed to cache QA pair: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Returns cache telemetry: hits, misses, hit rate, and active question count."""
        if not self.is_available():
            return {"status": "unavailable"}

        try:
            hits = int(self.client.get(f"{self.key_prefix}:stats:hits") or 0)
            misses = int(self.client.get(f"{self.key_prefix}:stats:misses") or 0)
            total = hits + misses
            hit_rate = (hits / total * 100.0) if total > 0 else 0.0

            active_keys = self.client.scard(f"{self.key_prefix}:all_keys") or 0
            info = self.client.info(section="memory")
            used_mem = info.get("used_memory_human", "N/A")

            return {
                "status": "connected",
                "host": f"{self.host}:{self.port}",
                "hits": hits,
                "misses": misses,
                "total_queries": total,
                "hit_rate_pct": round(hit_rate, 2),
                "active_cached_questions": active_keys,
                "used_memory": used_mem
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def clear(self) -> int:
        """Flushes all cached questions in this namespace."""
        if not self.is_available():
            return 0

        try:
            keys = self.client.keys(f"{self.key_prefix}:*")
            if keys:
                deleted = self.client.delete(*keys)
                print(f"[✓] Cleared {deleted} keys from Redis Question Cache.")
                return deleted
            return 0
        except Exception as e:
            print(f"[!] Clear cache error: {e}")
            return 0
