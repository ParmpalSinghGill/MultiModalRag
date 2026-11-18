"""
OpenSearch Indexer and Multimodal Vector Store.

Features:
- Connects to local OpenSearch Docker container (http://localhost:9200).
- Uses local SentenceTransformer ('sentence-transformers/all-MiniLM-L6-v2', 384 dim).
- Creates k-NN HNSW indices with cosine similarity and rich metadata schema:
  - Text prose, Markdown tables, OCR text, neural figure classes, and deduplicated S3 URLs.
- Bulk indexing with deterministic chunk IDs for idempotent upserts (no duplicate docs).
- Vector k-NN search, BM25 keyword search, and Hybrid search.
"""

import sys
import os
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Union

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from opensearchpy import OpenSearch, helpers
from sentence_transformers import SentenceTransformer

from data_ingestion.opensearch_schema import build_index_body


class OpenSearchIndexer:
    """
    Manages indexing and retrieval of multimodal document chunks in OpenSearch.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 9200,
        index_name: str = "multimodal_rag_finance",
        embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        device: str = "cuda",  # falls back to cpu if cuda unavailable
        client: Optional[OpenSearch] = None  # pre-built client, e.g. SigV4 for Amazon OpenSearch Service
    ):
        self.host = host
        self.port = port
        self.index_name = index_name
        self.embedding_model_name = embedding_model_name
        self.managed_cluster = client is not None

        # Connect to OpenSearch
        self.client = client or OpenSearch(
            hosts=[{"host": host, "port": port}],
            http_compress=True,
            use_ssl=False,
            verify_certs=False,
            timeout=30
        )

        # Load embedding model
        print(f"[*] Loading SentenceTransformer model '{embedding_model_name}'...")
        try:
            self.model = SentenceTransformer(embedding_model_name, device=device)
        except Exception:
            print("[!] Fallback to CPU for SentenceTransformer")
            self.model = SentenceTransformer(embedding_model_name, device="cpu")

        if hasattr(self.model, "get_embedding_dimension"):
            self.dimension = self.model.get_embedding_dimension()
        else:
            self.dimension = self.model.get_sentence_embedding_dimension()
        print(f"[✓] SentenceTransformer ready (Embedding dimension: {self.dimension})")

    def _ensure_cluster_ready(self):
        """Disables disk threshold blocks for smooth local development."""
        if self.managed_cluster:
            return
        try:
            self.client.cluster.put_settings(
                body={
                    "persistent": {
                        "cluster.blocks.create_index": None,
                        "cluster.routing.allocation.disk.threshold_enabled": False
                    }
                }
            )
        except Exception as e:
            pass

    def ping(self) -> bool:
        """Verifies OpenSearch cluster availability."""
        try:
            self._ensure_cluster_ready()
            info = self.client.info()
            print(f"[✓] OpenSearch Connected: {info.get('cluster_name')} (v{info.get('version', {}).get('number')})")
            return True
        except Exception as e:
            print(f"[!] OpenSearch connection failed: {e}")
            return False

    def create_index(self, recreate: bool = False) -> bool:
        """
        Creates k-NN index with HNSW cosine similarity mapping.
        """
        self._ensure_cluster_ready()
        if self.client.indices.exists(index=self.index_name):
            if recreate:
                print(f"[*] Deleting existing index '{self.index_name}'...")
                self.client.indices.delete(index=self.index_name)
            else:
                print(f"[*] Index '{self.index_name}' already exists.")
                return True

        index_body = build_index_body(dimension=self.dimension)

        self.client.indices.create(index=self.index_name, body=index_body)
        print(f"[✓] Created OpenSearch k-NN index '{self.index_name}' with {self.dimension}-dim vectors.")
        return True

    def embed_texts(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """Encodes texts into normalized vectors."""
        embeddings = self.model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=len(texts) > 20,
            normalize_embeddings=True
        )
        return embeddings.tolist()

    def index_chunks(
        self,
        chunks: List[Dict[str, Any]],
        batch_size: int = 50
    ) -> Dict[str, Any]:
        """
        Computes embeddings and bulk indexes chunks with deterministic IDs.
        Upserting chunks with identical `chunk_id` updates in place with no duplicates.
        """
        if not chunks:
            print("[!] No chunks provided to index.")
            return {"indexed": 0, "errors": 0}

        print(f"[*] Generating embeddings for {len(chunks)} chunks with {self.embedding_model_name}...")
        texts_to_embed = [c.get("text", "") for c in chunks]
        embeddings = self.embed_texts(texts_to_embed, batch_size=batch_size)

        actions = []
        for i, chunk in enumerate(chunks):
            doc_body = dict(chunk)
            doc_body["embedding"] = embeddings[i]

            # Flatten page_numbers if needed
            if "page_numbers" in doc_body and not isinstance(doc_body["page_numbers"], list):
                doc_body["page_numbers"] = [doc_body["page_numbers"]]

            action = {
                "_index": self.index_name,
                "_id": chunk.get("chunk_id", f"chunk_{i}"),
                "_source": doc_body
            }
            actions.append(action)

        print(f"[*] Bulk ingesting {len(actions)} documents into OpenSearch index '{self.index_name}'...")
        success_count, failed_items = helpers.bulk(
            self.client,
            actions,
            chunk_size=batch_size,
            raise_on_error=False,
            stats_only=False
        )

        # Refresh index so documents are immediately searchable
        self.client.indices.refresh(index=self.index_name)

        print(f"[✓] OpenSearch Ingestion Complete: {success_count} indexed, {len(failed_items)} failed.")
        return {"indexed": success_count, "errors": len(failed_items), "failed_items": failed_items}

    def search_knn(
        self,
        query_text: str,
        top_k: int = 5,
        doc_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Performs vector k-NN search against OpenSearch."""
        query_vector = self.model.encode(query_text, normalize_embeddings=True).tolist()

        knn_clause = {
            "knn": {
                "embedding": {
                    "vector": query_vector,
                    "k": top_k
                }
            }
        }

        if doc_filter:
            search_query = {
                "size": top_k,
                "query": {
                    "bool": {
                        "must": [knn_clause],
                        "filter": [{"term": {"document_id": doc_filter}}]
                    }
                }
            }
        else:
            search_query = {
                "size": top_k,
                "query": knn_clause
            }

        res = self.client.search(index=self.index_name, body=search_query)
        hits = res.get("hits", {}).get("hits", [])
        return [
            {
                "score": h["_score"],
                "chunk_id": h["_id"],
                "document_id": h["_source"].get("document_id"),
                "chunk_type": h["_source"].get("chunk_type"),
                "text": h["_source"].get("text"),
                "page_numbers": h["_source"].get("page_numbers"),
                "has_image": h["_source"].get("has_image"),
                "image_s3_url": h["_source"].get("image_s3_url"),
                "image_s3_uri": h["_source"].get("image_s3_uri"),
                "image_file": h["_source"].get("image_file"),
                "neural_class": h["_source"].get("neural_class"),
                "caption": h["_source"].get("caption")
            }
            for h in hits
        ]

    def search_bm25(
        self,
        query_text: str,
        top_k: int = 5,
        doc_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Performs BM25 keyword text search."""
        must_clause = {
            "multi_match": {
                "query": query_text,
                "fields": ["text^2", "caption^3", "ocr_text", "headings"]
            }
        }

        if doc_filter:
            search_query = {
                "size": top_k,
                "query": {
                    "bool": {
                        "must": [must_clause],
                        "filter": [{"term": {"document_id": doc_filter}}]
                    }
                }
            }
        else:
            search_query = {
                "size": top_k,
                "query": must_clause
            }

        res = self.client.search(index=self.index_name, body=search_query)
        hits = res.get("hits", {}).get("hits", [])
        return [
            {
                "score": h["_score"],
                "chunk_id": h["_id"],
                "document_id": h["_source"].get("document_id"),
                "chunk_type": h["_source"].get("chunk_type"),
                "text": h["_source"].get("text"),
                "page_numbers": h["_source"].get("page_numbers"),
                "has_image": h["_source"].get("has_image"),
                "image_s3_url": h["_source"].get("image_s3_url"),
                "image_s3_uri": h["_source"].get("image_s3_uri"),
                "image_file": h["_source"].get("image_file"),
                "neural_class": h["_source"].get("neural_class"),
                "caption": h["_source"].get("caption")
            }
            for h in hits
        ]

    def search_hybrid(
        self,
        query_text: str,
        top_k: int = 5,
        doc_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Combines neural k-NN vector search and BM25 full-text keyword search.
        """
        query_vector = self.model.encode(query_text, normalize_embeddings=True).tolist()

        search_query = {
            "size": top_k,
            "query": {
                "bool": {
                    "should": [
                        {
                            "knn": {
                                "embedding": {
                                    "vector": query_vector,
                                    "k": top_k
                                }
                            }
                        },
                        {
                            "multi_match": {
                                "query": query_text,
                                "fields": ["text^2", "caption^3", "ocr_text"],
                                "boost": 0.5
                            }
                        }
                    ]
                }
            }
        }

        if doc_filter:
            search_query["query"]["bool"]["filter"] = [{"term": {"document_id": doc_filter}}]

        res = self.client.search(index=self.index_name, body=search_query)
        hits = res.get("hits", {}).get("hits", [])
        return [
            {
                "score": h["_score"],
                "chunk_id": h["_id"],
                "document_id": h["_source"].get("document_id"),
                "chunk_type": h["_source"].get("chunk_type"),
                "text": h["_source"].get("text"),
                "page_numbers": h["_source"].get("page_numbers"),
                "has_image": h["_source"].get("has_image"),
                "image_s3_url": h["_source"].get("image_s3_url"),
                "image_s3_uri": h["_source"].get("image_s3_uri"),
                "image_file": h["_source"].get("image_file"),
                "neural_class": h["_source"].get("neural_class"),
                "caption": h["_source"].get("caption")
            }
            for h in hits
        ]
