"""
OpenSearch index definition shared by the indexer and the AWS operations Lambda.

This module must stay dependency-free: it is packaged on its own into the
operations Lambda, which creates the index when no snapshot exists.
"""

from typing import Any, Dict

DEFAULT_INDEX_NAME = "multimodal_rag_finance"
EMBEDDING_DIMENSION = 384


def build_index_body(dimension: int = EMBEDDING_DIMENSION, replicas: int = 0) -> Dict[str, Any]:
    """Returns the k-NN index settings and mappings."""
    return {
        "settings": {
            "index": {
                "knn": True,
                "knn.algo_param.ef_search": 100,
                "number_of_shards": 1,
                "number_of_replicas": replicas,
            }
        },
        "mappings": {
            "properties": {
                "chunk_id": {"type": "keyword"},
                "chunk_index": {"type": "integer"},
                "document_id": {"type": "keyword"},
                "chunk_type": {"type": "keyword"},
                "text": {"type": "text"},
                "token_count": {"type": "integer"},
                "headings": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "page_numbers": {"type": "integer"},
                "has_table": {"type": "boolean"},
                "has_image": {"type": "boolean"},
                "image_file": {"type": "keyword"},
                "image_s3_uri": {"type": "keyword"},
                "image_s3_url": {"type": "keyword"},
                "image_sha256": {"type": "keyword"},
                "caption": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "ocr_text": {"type": "text"},
                "figure_type": {"type": "keyword"},
                "neural_class": {"type": "keyword"},
                "classification_confidence": {"type": "float"},
                "source_file": {"type": "keyword"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dimension,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "nmslib",
                        "parameters": {"ef_construction": 128, "m": 16},
                    },
                },
            }
        },
    }
