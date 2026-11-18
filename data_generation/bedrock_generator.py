"""
AWS Bedrock LLM Generator for Multimodal RAG.

Provides cost-effective generative inference using AWS Bedrock foundation models:
- Meta Llama 3 8B Instruct (meta.llama3-8b-instruct-v1:0) - Default primary model
- Amazon Nova Micro (amazon.nova-micro-v1:0) - Ultra low-cost ($0.000035 / 1K tokens)
- Amazon Nova Lite (amazon.nova-lite-v1:0) - Fast multimodal text/vision model
- Mistral 7B Instruct v0.2 (mistral.mistral-7b-instruct-v0:2) - Efficient reasoning model

Features:
- Standardized Bedrock Converse API integration.
- Grounded contextual prompt formatting (tables, chart OCR labels, S3 figure URLs, citations).
- Per-query token tracking and cost calculation in USD.
- Automatic credentials resolution (AWS profile 'rag-dev' -> env variables -> default boto3 session).
"""

import os
import sys
import time
from typing import Dict, Any, List, Optional
import boto3
from botocore.exceptions import ClientError, BotoCoreError

# Pricing per 1,000 tokens (USD) as of 2024-2025 AWS Bedrock on-demand pricing
BEDROCK_MODELS: Dict[str, Dict[str, Any]] = {
    "meta.llama3-8b-instruct-v1:0": {
        "name": "Meta Llama 3 8B Instruct",
        "input_cost_per_1k": 0.00030,
        "output_cost_per_1k": 0.00060,
        "description": "High accuracy, robust financial reasoning, low cost.",
    },
    "amazon.nova-micro-v1:0": {
        "name": "Amazon Nova Micro",
        "input_cost_per_1k": 0.000035,
        "output_cost_per_1k": 0.00014,
        "description": "Ultra low-cost, ultra-fast text model for high-throughput RAG.",
    },
    "amazon.nova-lite-v1:0": {
        "name": "Amazon Nova Lite",
        "input_cost_per_1k": 0.00006,
        "output_cost_per_1k": 0.00024,
        "description": "Low-cost model with strong multimodal comprehension.",
    },
    "mistral.mistral-7b-instruct-v0:2": {
        "name": "Mistral 7B Instruct v0.2",
        "input_cost_per_1k": 0.00015,
        "output_cost_per_1k": 0.00020,
        "description": "Fast European open-weights model on Bedrock.",
    },
}

DEFAULT_MODEL_ID = "meta.llama3-8b-instruct-v1:0"

DEFAULT_SYSTEM_PROMPT = """You are an expert financial and document intelligence assistant for a Multimodal RAG system.
Your task is to provide clear, accurate, and concise answers based on the provided context excerpts (which include document text, financial tables, and chart descriptions).

Strict Guidelines:
1. Source Report Assumption: The provided excerpts are extracted directly from the report or document queried (e.g. if the question refers to a specific report title, author, or organization, assume the provided excerpts represent that document).
2. Grounding & Synthesis: Answer using facts, data points, and trends documented in the context excerpts. Summarize the evidence clearly and directly address the user's question.
3. Citations: Explicitly cite source documents and page numbers where information is drawn (e.g. "[Doc 3152577, Page 14]").
4. Numbers & Tables: Accurately quote exact numerical values and percentages from tables and excerpts.
5. Multimodal Charts: When referencing a visual chart, graph, or diagram, mention its caption/title and its S3 image URL if present.
6. Incomplete Info: Only if the excerpts do not contain any relevant information regarding the queried topic or metric, state: "The provided documents do not contain sufficient information to answer this question."
"""


class BedrockGenerator:
    """
    AWS Bedrock Generator utilizing Converse API with low-cost LLMs.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        profile_name: str = "rag-dev",
        region_name: str = "us-east-1",
        default_temperature: float = 0.1,
        default_max_tokens: int = 512,
    ):
        self.model_id = model_id
        self.profile_name = profile_name
        self.region_name = region_name
        self.default_temperature = default_temperature
        self.default_max_tokens = default_max_tokens

        self.client = self._init_client()

    def _init_client(self):
        """Initializes Bedrock Runtime client with fallback mechanisms."""
        # 1. Try named profile
        if self.profile_name:
            try:
                session = boto3.Session(profile_name=self.profile_name)
                client = session.client("bedrock-runtime", region_name=self.region_name)
                return client
            except Exception as e:
                pass

        # 2. Try default session (env vars / EC2 instance role)
        try:
            session = boto3.Session()
            client = session.client("bedrock-runtime", region_name=self.region_name)
            return client
        except Exception as e:
            raise RuntimeError(
                f"Failed to initialize AWS Bedrock Runtime client: {e}. "
                f"Please ensure AWS credentials or profile '{self.profile_name}' are configured."
            )

    def calculate_cost(
        self, input_tokens: int, output_tokens: int, model_id: Optional[str] = None
    ) -> float:
        """Calculates estimated AWS Bedrock cost in USD based on token counts."""
        mid = model_id or self.model_id
        meta = BEDROCK_MODELS.get(mid, {})
        in_rate = meta.get("input_cost_per_1k", 0.00030)
        out_rate = meta.get("output_cost_per_1k", 0.00060)

        in_cost = (input_tokens / 1000.0) * in_rate
        out_cost = (output_tokens / 1000.0) * out_rate
        return round(in_cost + out_cost, 6)

    def format_context(self, context_chunks: List[Dict[str, Any]]) -> str:
        """
        Formats retrieved OpenSearch chunks into a structured context string
        with clear document, page, and S3 image citations.
        """
        if not context_chunks:
            return "No relevant context found in documents."

        formatted_parts = []
        for i, chunk in enumerate(context_chunks, 1):
            doc_id = chunk.get("document_id", "Unknown")
            pages = chunk.get("page_numbers") or []
            page_str = f"Page {pages[0]}" if pages else "Page N/A"
            c_type = chunk.get("chunk_type") or chunk.get("content_type") or "text"
            neural_cls = chunk.get("neural_class")
            cls_info = f" | {neural_cls}" if neural_cls else ""

            header = f"--- [EXCERPT {i}] Document: {doc_id} | {page_str} | Type: {c_type}{cls_info} ---"

            text = chunk.get("text", "").strip()

            multimodal_links = []
            if chunk.get("caption"):
                multimodal_links.append(f"Caption: {chunk['caption']}")
            if chunk.get("image_s3_url"):
                multimodal_links.append(f"Figure Image Link: {chunk['image_s3_url']}")

            block = f"{header}\n{text}"
            if multimodal_links:
                block += "\n" + "\n".join(multimodal_links)

            formatted_parts.append(block)

        return "\n\n".join(formatted_parts)

    def generate_answer(
        self,
        question: str,
        context_chunks: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_id: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Synthesizes an answer using AWS Bedrock Converse API.

        Returns:
            Dict containing:
            - answer: generated answer text
            - model_id: model used
            - latency_ms: API round-trip time in milliseconds
            - input_tokens: input token count
            - output_tokens: generated token count
            - total_tokens: sum of tokens
            - estimated_cost_usd: calculated cost in dollars
            - sources_used: chunk metadata summaries
        """
        active_model = model_id or self.model_id
        sys_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        temp = temperature if temperature is not None else self.default_temperature
        max_tok = max_tokens if max_tokens is not None else self.default_max_tokens

        # Format context
        context_str = self.format_context(context_chunks)

        user_content = (
            f"Here are the relevant excerpts retrieved from the document store:\n\n"
            f"{context_str}\n\n"
            f"Question: {question.strip()}\n\n"
            f"Answer:"
        )

        messages = [
            {
                "role": "user",
                "content": [{"text": user_content}],
            }
        ]

        system_config = [{"text": sys_prompt}]

        inference_config = {
            "temperature": temp,
            "maxTokens": max_tok,
        }

        start_time = time.time()
        try:
            response = self.client.converse(
                modelId=active_model,
                system=system_config,
                messages=messages,
                inferenceConfig=inference_config,
            )
            elapsed_ms = (time.time() - start_time) * 1000

            # Extract generated text
            answer_text = response["output"]["message"]["content"][0]["text"].strip()

            # Extract token usage
            usage = response.get("usage", {})
            input_tokens = usage.get("inputTokens", 0)
            output_tokens = usage.get("outputTokens", 0)
            total_tokens = usage.get("totalTokens", input_tokens + output_tokens)

            cost = self.calculate_cost(input_tokens, output_tokens, active_model)

            # Extract source summaries
            sources = []
            for c in context_chunks:
                sources.append({
                    "chunk_id": c.get("chunk_id"),
                    "document_id": c.get("document_id"),
                    "page_numbers": c.get("page_numbers"),
                    "content_type": c.get("chunk_type") or c.get("content_type") or "text",
                    "neural_class": c.get("neural_class"),
                    "caption": c.get("caption"),
                    "image_s3_url": c.get("image_s3_url"),
                    "score": round(c.get("score", 0.0), 4),
                })

            return {
                "answer": answer_text,
                "model_id": active_model,
                "model_name": BEDROCK_MODELS.get(active_model, {}).get("name", active_model),
                "latency_ms": round(elapsed_ms, 2),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": cost,
                "sources": sources,
                "error": None,
            }

        except ClientError as ce:
            elapsed_ms = (time.time() - start_time) * 1000
            err_code = ce.response.get("Error", {}).get("Code", "ClientError")
            err_msg = ce.response.get("Error", {}).get("Message", str(ce))
            print(f"[!] Bedrock API Error ({err_code}): {err_msg}")
            return {
                "answer": f"Error communicating with AWS Bedrock ({err_code}): {err_msg}",
                "model_id": active_model,
                "latency_ms": round(elapsed_ms, 2),
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "sources": [],
                "error": str(ce),
            }
        except Exception as e:
            elapsed_ms = (time.time() - start_time) * 1000
            print(f"[!] Bedrock invocation error: {e}")
            return {
                "answer": f"Error invoking model: {e}",
                "model_id": active_model,
                "latency_ms": round(elapsed_ms, 2),
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "sources": [],
                "error": str(e),
            }
