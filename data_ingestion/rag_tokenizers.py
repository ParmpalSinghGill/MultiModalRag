"""
Pluggable Tokenizer Implementations for Document Chunking.

Supports target embedding windows:
1. 'text-embedding-3-small' / 'openai': OpenAI BPE tokenizer (tiktoken cl100k_base, 8,191 max window) - Default
2. 'amazon-titan-v2': Amazon Titan Text Embeddings V2 (BPE gpt2, 8,192 max window)
3. 'gemini': Google Gemini / Gemma SentencePiece tokenizer (256k vocab, 2,048 max window)
4. 'sentence-transformers': all-MiniLM-L6-v2 (220 tokens safe limit)
"""

from typing import Any
from pydantic import ConfigDict

try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False

from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer


class OpenAITiktokenTokenizer(BaseTokenizer):
    """
    OpenAI BPE tokenizer using tiktoken (cl100k_base or o200k_base).
    Used by: text-embedding-3-small, text-embedding-3-large, gpt-4o, gpt-4.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)
    encoding_name: str = "cl100k_base"
    max_tokens: int = 1000

    def count_tokens(self, text: str) -> int:
        if not TIKTOKEN_AVAILABLE:
            return len(text.split())
        enc = tiktoken.get_encoding(self.encoding_name)
        return len(enc.encode(text, disallowed_special=()))

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self) -> Any:
        if TIKTOKEN_AVAILABLE:
            return tiktoken.get_encoding(self.encoding_name)
        return None


class GeminiTokenizer(BaseTokenizer):
    """
    Google Gemini / Gemma SentencePiece tokenizer (256k vocabulary).
    Used by: Gemini 1.5, Gemini 2.0, Gemma 2.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)
    model_name: str = "google/gemma-2-2b"
    max_tokens: int = 1000

    def count_tokens(self, text: str) -> int:
        tok = self.get_tokenizer()
        if tok is not None:
            return len(tok.tokenize(text))
        return len(text.split())

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self) -> Any:
        if not hasattr(self, "_tok") or self._tok is None:
            try:
                from transformers import AutoTokenizer
                self._tok = AutoTokenizer.from_pretrained(self.model_name)
            except Exception:
                self._tok = None
        return self._tok


class AmazonTitanTokenizer(BaseTokenizer):
    """
    Amazon Titan Text Embeddings V2 tokenizer.
    Uses BPE tokenizer (gpt2 encoding in tiktoken), matching Amazon Titan's 8,192 token window.
    Used by: amazon.titan-embed-text-v2:0, amazon.titan-embed-text-v1.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)
    encoding_name: str = "gpt2"
    max_tokens: int = 1000

    def count_tokens(self, text: str) -> int:
        if TIKTOKEN_AVAILABLE:
            enc = tiktoken.get_encoding(self.encoding_name)
            return len(enc.encode(text, disallowed_special=()))
        return len(text.split())

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self) -> Any:
        if TIKTOKEN_AVAILABLE:
            return tiktoken.get_encoding(self.encoding_name)
        return None


def create_tokenizer(tokenizer_type: str = "text-embedding-3-small", max_tokens: int = 1000) -> BaseTokenizer:
    """
    Factory creating the selected tokenizer instance.
    Options:
        - 'text-embedding-3-small' / 'openai' (Default): OpenAI cl100k_base (8,191 token window)
        - 'amazon-titan-v2' / 'titan': Amazon Titan Text Embeddings V2 (8,192 token window)
        - 'gemini' / 'gemma': Google Gemini SentencePiece tokenizer (2,048 token window)
        - 'sentence-transformers': all-MiniLM-L6-v2 (220 tokens safe limit)
    """
    t_type = tokenizer_type.lower().strip()
    if t_type in ("openai", "open8", "text-embedding-3-small", "text-embedding-3-large", "chatgpt", "tiktoken"):
        print(f"[*] Configured OpenAI Tokenizer (text-embedding-3-small | tiktoken cl100k_base) | Max Tokens: {max_tokens}")
        return OpenAITiktokenTokenizer(encoding_name="cl100k_base", max_tokens=max_tokens)
    elif t_type in ("amazon-titan-v2", "amazon-titan", "amazon", "titan", "titanic", "amazon-titanic"):
        print(f"[*] Configured Amazon Titan Tokenizer (amazon.titan-embed-text-v2:0 | BPE) | Max Tokens: {max_tokens}")
        return AmazonTitanTokenizer(encoding_name="gpt2", max_tokens=max_tokens)
    elif t_type in ("gemini", "gemite", "google", "gemma", "text-embedding-004"):
        print(f"[*] Configured Gemini Tokenizer (Gemma 256k vocab) | Max Tokens: {max_tokens}")
        return GeminiTokenizer(max_tokens=max_tokens)
    elif t_type in ("sentence-transformers", "minilm", "slm", "all-minilm-l6-v2"):
        safe_max = min(max_tokens, 220)
        print(f"[*] Configured Sentence-Transformers Tokenizer (all-MiniLM-L6-v2) | Max Tokens: {safe_max} (capped to 256 trained limit)")
        from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
        return HuggingFaceTokenizer.from_pretrained(
            model_name="sentence-transformers/all-MiniLM-L6-v2",
            max_tokens=safe_max
        )
    else:
        print(f"[!] Unknown tokenizer '{tokenizer_type}', defaulting to OpenAI text-embedding-3-small (cl100k_base)")
        return OpenAITiktokenTokenizer(max_tokens=max_tokens)
