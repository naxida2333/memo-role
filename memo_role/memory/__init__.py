"""三层记忆系统：工作记忆 / 情节记忆 / 核心记忆，全局统一召回。"""

from __future__ import annotations

from .embedding import (
    EmbeddingProvider,
    HashingEmbedding,
    LlamaServerEmbedding,
    OpenAIEmbedding,
    build_embedding_provider,
    cosine_similarity,
)
from .extractor import (
    ExtractedMemory,
    LlmExtractor,
    MemoryExtractor,
    RuleBasedExtractor,
    build_extractor,
)
from .manager import MemoryManager, RecallBundle
from .store import (
    KIND_CORE,
    KIND_EPISODIC,
    SCOPE_ALL,
    SCOPE_GLOBAL_ONLY,
    SCOPE_SESSION,
    MemoryRecord,
    MemoryStore,
    MessageRecord,
    SessionRecord,
    SpeakerRecord,
)

__all__ = [
    "EmbeddingProvider",
    "HashingEmbedding",
    "LlamaServerEmbedding",
    "OpenAIEmbedding",
    "build_embedding_provider",
    "cosine_similarity",
    "ExtractedMemory",
    "LlmExtractor",
    "MemoryExtractor",
    "RuleBasedExtractor",
    "build_extractor",
    "MemoryManager",
    "RecallBundle",
    "KIND_CORE",
    "KIND_EPISODIC",
    "SCOPE_ALL",
    "SCOPE_GLOBAL_ONLY",
    "SCOPE_SESSION",
    "MemoryRecord",
    "MemoryStore",
    "MessageRecord",
    "SessionRecord",
    "SpeakerRecord",
]