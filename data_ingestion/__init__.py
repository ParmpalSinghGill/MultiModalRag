"""
Data Ingestion Module for Multimodal RAG Pipeline.

Exports are resolved lazily so a container can import one component (for example
the OpenSearch indexer in the API image) without installing Docling and every
other ingestion dependency. `from data_ingestion import X` works as before.
"""

from importlib import import_module

_EXPORTS = {
    "S3DocumentFetcher": ".s3_fetcher",
    "S3FigureUploader": ".s3_figure_uploader",
    "OpenSearchIndexer": ".opensearch_indexer",
    "RedisQuestionCache": ".redis_question_cache",
    "RAGQueryEngine": ".query_engine",
    "DocumentInspector": ".browse_documents",
    "DoclingProcessor": ".docling_processor",
    "LlamaParseProcessor": ".llama_parse_processor",
    "create_tokenizer": ".rag_tokenizers",
    "ensure_cudnn_ld_path": ".utils",
    "compute_file_hash": ".utils",
    "debug_pause": ".utils",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
