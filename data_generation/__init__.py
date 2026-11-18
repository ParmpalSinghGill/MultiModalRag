"""
Multimodal RAG Data Generation Package.

Components:
- BedrockGenerator: AWS Bedrock inference client with low-cost LLMs (Meta Llama 3 8B, Amazon Nova Micro/Lite, Mistral 7B).
- MultimodalRAGPipeline: End-to-end RAG orchestrator uniting OpenSearch hybrid retrieval, Redis caching, and Bedrock synthesis.
- Evaluation & CLI scripts for answering questions and evaluating against benchmarks.
"""

from data_generation.bedrock_generator import BedrockGenerator, BEDROCK_MODELS
from data_generation.rag_pipeline import MultimodalRAGPipeline

__all__ = [
    "BedrockGenerator",
    "MultimodalRAGPipeline",
    "BEDROCK_MODELS",
]
