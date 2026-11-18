"""
Common Ingestion Utilities.

Shared across all document processors (Docling, LlamaParse, etc.) and pipelines.
- cuDNN Linux runtime dynamic link resolution
- Cryptographic SHA-256 content hashing for caching
- Interactive deep debugging pause helpers
- Safe JSON serialization helpers
"""

import sys
import os
import json
import hashlib
from typing import Any, Dict, Optional


def ensure_cudnn_ld_path():
    """
    Self-healing Linux cuDNN dynamic library resolution for PyTorch + CUDA.
    Ensures NVIDIA wheel runtime libs take precedence over system paths.
    """
    env_lib = os.path.join(
        sys.prefix,
        "lib",
        f"python{sys.version_info.major}.{sys.version_info.minor}",
        "site-packages",
        "nvidia",
    )
    if os.path.exists(env_lib):
        cudnn_paths = [
            os.path.join(env_lib, "cudnn", "lib"),
            os.path.join(env_lib, "cublas", "lib"),
            os.path.join(env_lib, "cuda_runtime", "lib"),
        ]
        valid_paths = [p for p in cudnn_paths if os.path.isdir(p)]
        current_ld = os.environ.get("LD_LIBRARY_PATH", "")
        missing_paths = [p for p in valid_paths if p not in current_ld.split(":")]
        if missing_paths and os.name == "posix":
            new_ld = ":".join(missing_paths + ([current_ld] if current_ld else []))
            os.environ["LD_LIBRARY_PATH"] = new_ld
            if not os.environ.get("_CUDNN_PATCHED"):
                os.environ["_CUDNN_PATCHED"] = "1"
                os.execv(sys.executable, [sys.executable] + sys.argv)


def compute_file_hash(file_path: str) -> str:
    """Computes SHA-256 hash of file content to detect any document modifications."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Cannot compute hash, file not found: {file_path}")
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def debug_pause(stage_name: str, message: str = "", tag: str = "DEEP DEBUG") -> bool:
    """
    Interactive checkpoint prompt for step-by-step debugging.
    Returns:
        True: User pressed ENTER to continue debugging.
        False: User typed 'q' to disable further debug pauses.
    """
    prompt_msg = f"\n[{tag}] ⏸️ Paused at: {stage_name}"
    if message:
        prompt_msg += f"\n  ℹ️ {message}"
    prompt_msg += "\n>>> Press [ENTER] to process next (or type 'q' to disable debug pause): "
    try:
        user_in = input(prompt_msg)
        if user_in.strip().lower() == "q":
            print(f"[*] [{tag}] Pauses disabled for remaining steps in this session.")
            return False
        return True
    except (EOFError, KeyboardInterrupt):
        print(f"\n[*] [{tag}] Pauses bypassed.")
        return False


def save_json(data: Any, file_path: str, indent: int = 2) -> str:
    """Safely saves a Python dict/list as UTF-8 formatted JSON, ensuring directory exists."""
    os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    return os.path.abspath(file_path)


def load_json(file_path: str) -> Optional[Dict[str, Any]]:
    """Safely loads a JSON file if it exists, else returns None."""
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
