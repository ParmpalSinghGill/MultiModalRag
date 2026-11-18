"""
Interactive Redis Question Cache Inspector.

Features:
- View in-memory Redis cache telemetry, hit/miss rates, and memory footprint.
- List all cached questions, associated document filters, and remaining TTL.
- Deep inspect cached answers, retrieved chunks, and S3 image links.
- Flush or reset cache when needed.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional
import redis

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_ingestion.redis_question_cache import RedisQuestionCache


def format_seconds(seconds: int) -> str:
    """Formats seconds to human-readable string."""
    if seconds < 0:
        return "No expiration"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Inspect Redis Question Cache.")
    parser.add_argument("--stats", action="store_true", help="Display cache telemetry, hit rate, and memory usage")
    parser.add_argument("--list", action="store_true", help="List all cached questions and their TTLs")
    parser.add_argument("--view", type=str, default=None, help="Inspect a specific cached question by key or text snippet")
    parser.add_argument("--flush", action="store_true", help="Flush/clear all cached questions")
    parser.add_argument("--port", type=int, default=6380, help="Redis port (default: 6380)")

    args = parser.parse_args()
    cache = RedisQuestionCache(port=args.port)

    if not cache.is_available():
        print(f"[!] Redis is not reachable on localhost:{args.port}.")
        print("    Ensure the Docker container 'multiformat-rag-redis' is running.")
        sys.exit(1)

    # 1. Flush Cache
    if args.flush:
        deleted = cache.clear()
        print(f"[✓] Successfully flushed {deleted} cached question entries from Redis.")
        return

    # 2. View Specific Question
    if args.view:
        view_query = args.view.strip().lower()
        matched = []
        all_keys = cache.client.keys(f"{cache.key_prefix}:*")
        for k in all_keys:
            if "stats" in k or "all_keys" in k:
                continue
            val_str = cache.client.get(k)
            if not val_str:
                continue
            try:
                data = json.loads(val_str)
                q_text = data.get("question", "").lower()
                ans_text = str(data.get("generated_answer") or data.get("answer") or "").lower()
                if view_query in k.lower() or view_query in q_text or view_query in ans_text:
                    matched.append((k, data, cache.client.ttl(k)))
            except Exception:
                pass

        if not matched:
            print(f"[!] No cached entries matched query '{args.view}'.")
            return

        print(f"\nFound {len(matched)} matching cached question(s):")
        print("=" * 76)
        for k, data, ttl in matched:
            print(f"🔑 Key : {k}")
            print(f"⏳ TTL : {format_seconds(ttl)} ({ttl} seconds)")
            print(f"❓ Question: \"{data.get('question')}\"")
            if data.get("generated_answer") or data.get("answer"):
                ans = data.get("generated_answer") or data.get("answer")
                print(f"🎯 Answer  : \"{ans}\"")

            results = data.get("results", [])
            print(f"📦 Cached Chunks ({len(results)} chunks):")
            for idx, r in enumerate(results, 1):
                cid = r.get("chunk_id")
                score = r.get("score", 0.0)
                doc = r.get("document_id")
                page = (r.get("page_numbers") or ["N/A"])[0]
                neural = f" | {r.get('neural_class')}" if r.get("neural_class") else ""
                print(f"   #{idx} [{cid}] Doc: {doc} (Page {page}) - Score: {score:.4f}{neural}")
                if r.get("image_s3_url"):
                    print(f"       🖼️  S3 Link: {r.get('image_s3_url')}")
            print("-" * 76)
        return

    # 3. List All Questions
    if args.list:
        all_keys = sorted(cache.client.keys(f"{cache.key_prefix}:*"))
        question_keys = [k for k in all_keys if not k.endswith(":stats:hits") and not k.endswith(":stats:misses") and not k.endswith(":all_keys")]

        print("=" * 76)
        print(f"📋 REDIS CACHED QUESTIONS ({len(question_keys)} entries)")
        print("=" * 76)

        for i, k in enumerate(question_keys, 1):
            val_str = cache.client.get(k)
            ttl = cache.client.ttl(k)
            ttl_str = format_seconds(ttl)

            q_text = "Unknown"
            doc_filter = "All"
            ans_snippet = ""

            if val_str:
                try:
                    data = json.loads(val_str)
                    q_text = data.get("question", "")
                    doc_filter = data.get("doc_filter") or data.get("document_id") or "All"
                    ans = data.get("generated_answer") or data.get("answer")
                    if ans:
                        ans_snippet = f" | Ans: {ans[:45]}..."
                except Exception:
                    pass

            print(f"#{i:<2} [TTL: {ttl_str:<9}] [Doc: {doc_filter:<8}] {q_text}{ans_snippet}")
        print("=" * 76)
        return

    # Default: Show Stats
    stats = cache.get_stats()
    print("=" * 76)
    print("📊 REDIS QUESTION CACHE TELEMETRY")
    print("=" * 76)
    print(f"Connection Endpoint      : {stats.get('host')}")
    print(f"Status                   : {stats.get('status').upper()}")
    print(f"Active Cached Questions  : {stats.get('active_cached_questions')}")
    print(f"Cache Hit Rate           : {stats.get('hit_rate_pct')}%")
    print(f"Total Queries Evaluated  : {stats.get('total_queries')}")
    print(f"  • Cache Hits (⚡ <1ms)  : {stats.get('hits')}")
    print(f"  • Cache Misses (🔍 OS)  : {stats.get('misses')}")
    print(f"Memory Allocated         : {stats.get('used_memory')}")
    print("=" * 76)
    print("\nTip: Run with --list to view all questions, or --view <query> to inspect details.")


if __name__ == "__main__":
    main()
