"""推理层：三种后端共用一套 ``ChatBackend`` 接口。"""

from __future__ import annotations

from .base import (
    BackendUnavailableError,
    ChatBackend,
    ChatMessage,
    GenParams,
    InferenceError,
    ModelNotFoundError,
)
from .catalog import BUILTIN_MODELS, ModelSpec, default_model_id
from .factory import SUPPORTED_BACKENDS, build_backend, gen_params
from .llama_cpp import LlamaCppBackend
from .llama_server import LlamaServerBackend
from .openai_api import OpenAICompatBackend, mask_key
from .registry import ModelRegistry

__all__ = [
    "BackendUnavailableError",
    "ChatBackend",
    "ChatMessage",
    "GenParams",
    "InferenceError",
    "ModelNotFoundError",
    "BUILTIN_MODELS",
    "ModelSpec",
    "default_model_id",
    "SUPPORTED_BACKENDS",
    "build_backend",
    "gen_params",
    "LlamaCppBackend",
    "LlamaServerBackend",
    "OpenAICompatBackend",
    "mask_key",
    "ModelRegistry",
]