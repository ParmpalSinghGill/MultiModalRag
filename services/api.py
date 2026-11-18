"""FastAPI query service. Wraps MultimodalRAGPipeline without changing its logic."""

import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from data_generation.bedrock_generator import BEDROCK_MODELS, DEFAULT_MODEL_ID
from data_generation.rag_pipeline import MultimodalRAGPipeline
from services.common import index_name, opensearch_client, region

PRESIGN_SECONDS = 900


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    doc_filter: Optional[str] = Field(default=None, max_length=256)
    top_k: int = Field(default=4, ge=1, le=20)
    search_mode: Literal["hybrid", "knn", "bm25"] = "hybrid"
    model_id: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=1)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=4096)
    bypass_cache: bool = False


def build_pipeline() -> MultimodalRAGPipeline:
    redis_host = os.environ.get("REDIS_HOST", "")
    return MultimodalRAGPipeline(
        opensearch_host=os.environ["OPENSEARCH_ENDPOINT"],
        opensearch_port=443,
        opensearch_client=opensearch_client(),
        index_name=index_name(),
        # With Redis disabled the existing cache class fails fast and stays inactive.
        redis_host=redis_host or "127.0.0.1",
        redis_port=int(os.environ.get("REDIS_PORT", "6379")),
        redis_ssl=bool(redis_host),
        redis_password=os.environ.get("REDIS_AUTH_TOKEN") or None,
        model_id=os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        region_name=os.environ.get("BEDROCK_REGION", region()),
    )


def add_figure_links(sources: List[Dict[str, Any]], s3_client, bucket: str) -> List[Dict[str, Any]]:
    """The bucket is private, so figures are returned as short-lived signed links."""
    for source in sources:
        parsed = urlparse(source.get("image_s3_url") or "")
        if parsed.hostname and parsed.hostname.split(".")[0] == bucket:
            source["image_presigned_url"] = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": parsed.path.lstrip("/")},
                ExpiresIn=PRESIGN_SECONDS,
            )
    return sources


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pipeline = build_pipeline()
    app.state.s3 = boto3.client(
        "s3", region_name=region(), config=Config(signature_version="s3v4")
    )
    yield


app = FastAPI(title="MultiFormat RAG API", lifespan=lifespan)


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness only, so a restore in progress does not cycle the containers."""
    return {"status": "ok"}


@app.get("/ready")
def ready(request: Request) -> Dict[str, Any]:
    retriever = request.app.state.pipeline.retriever
    try:
        exists = retriever.client.indices.exists(index=retriever.index_name)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"OpenSearch unavailable: {type(exc).__name__}")
    if not exists:
        raise HTTPException(status_code=503, detail="Search index is not available")
    return {"status": "ready", "index": retriever.index_name}


# Declared as a plain function so FastAPI runs the blocking pipeline in its thread pool.
@app.post("/query")
def query(body: QueryRequest, request: Request) -> Dict[str, Any]:
    if body.model_id and body.model_id not in BEDROCK_MODELS:
        raise HTTPException(status_code=400, detail="Unsupported model_id")
    result = request.app.state.pipeline.answer(
        question=body.question,
        doc_filter=body.doc_filter,
        top_k=body.top_k,
        search_mode=body.search_mode,
        model_id=body.model_id,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        bypass_cache=body.bypass_cache,
    )
    if result.get("error"):
        raise HTTPException(status_code=502, detail="Answer generation failed")
    result["sources"] = add_figure_links(
        result.get("sources", []), request.app.state.s3, os.environ["DOCUMENTS_BUCKET"]
    )
    return result
