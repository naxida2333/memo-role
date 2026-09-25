"""向量化（embedding）测试。"""

from __future__ import annotations

import httpx
import numpy as np
import pytest

from memo_role.config import OpenAIConfig, LlamaServerConfig, load_config
from memo_role.inference.base import InferenceError
from memo_role.memory.embedding import (
    HashingEmbedding,
    LlamaServerEmbedding,
    OpenAIEmbedding,
    build_embedding_provider,
    cosine_similarity,
)


# ----------------------------------------------------------------------
# 特征哈希
# ----------------------------------------------------------------------
def test_tokenize_latin_and_cjk() -> None:
    tokens = HashingEmbedding.tokenize("Hello 世界")
    assert "hello" in tokens
    assert "世" in tokens and "界" in tokens
    assert "世界" in tokens  # CJK 二元组


def test_tokenize_empty() -> None:
    assert HashingEmbedding.tokenize("") == []
    assert HashingEmbedding.tokenize("   ，。！") == []


def test_invalid_dim_raises() -> None:
    with pytest.raises(ValueError):
        HashingEmbedding(dim=0)


def test_dim_and_signature() -> None:
    provider = HashingEmbedding(dim=64)
    assert provider.dim == 64
    assert provider.signature == "hashing:64"


def test_embedding_is_deterministic() -> None:
    """必须跨调用稳定（内部用 hashlib 而非内置 hash）。"""
    provider = HashingEmbedding(dim=128)
    first = provider.embed_one("我喜欢在雨天读小说")
    second = provider.embed_one("我喜欢在雨天读小说")
    assert np.allclose(first, second)


def test_embedding_is_normalized() -> None:
    provider = HashingEmbedding(dim=128)
    vector = provider.embed_one("测试文本")
    assert pytest.approx(float(np.linalg.norm(vector)), abs=1e-5) == 1.0


def test_empty_text_gives_zero_vector() -> None:
    provider = HashingEmbedding(dim=32)
    vector = provider.embed_one("")
    assert float(np.linalg.norm(vector)) == 0.0


def test_similar_text_scores_higher() -> None:
    """词法相近的文本相似度应高于无关文本。"""
    provider = HashingEmbedding(dim=512)
    base = provider.embed_one("我喜欢猫")
    close = provider.embed_one("我喜欢猫粮")
    far = provider.embed_one("今天股市大跌")
    assert float(base @ close) > float(base @ far)


def test_embed_batch() -> None:
    provider = HashingEmbedding(dim=64)
    vectors = provider.embed(["a", "b", "c"])
    assert len(vectors) == 3
    assert all(v.shape == (64,) for v in vectors)


def test_cosine_similarity_helper() -> None:
    matrix = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    scores = cosine_similarity(matrix, np.array([1.0, 0.0], dtype=np.float32))
    assert pytest.approx(scores[0]) == 1.0
    assert pytest.approx(scores[1]) == 0.0
    # 空矩阵返回空数组，不应报错
    assert cosine_similarity(np.zeros((0, 2), dtype=np.float32), np.ones(2, np.float32)).shape == (0,)


def test_cosine_similarity_handles_zero_vector() -> None:
    matrix = np.zeros((1, 3), dtype=np.float32)
    scores = cosine_similarity(matrix, np.zeros(3, dtype=np.float32))
    assert scores.shape == (1,)
    assert float(scores[0]) == 0.0


# ----------------------------------------------------------------------
# 远程 embedding
# ----------------------------------------------------------------------
def test_openai_embedding_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/embeddings")
        import json

        body = json.loads(request.content)
        assert body["model"] == "text-embedding-3-small"
        assert body["input"] == ["测试"]
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]}
        )

    config = OpenAIConfig(base_url="https://example.test/v1", api_keys=["sk-test"])
    client = httpx.Client(
        base_url="https://example.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAIEmbedding(config, model="text-embedding-3-small", client=client)

    vector = provider.embed_one("测试")
    assert provider.dim == 2
    assert provider.signature == "openai:text-embedding-3-small:2"
    # [3,4] 归一化后为 [0.6, 0.8]
    assert pytest.approx(vector.tolist(), abs=1e-6) == [0.6, 0.8]


def test_openai_embedding_sorts_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    config = OpenAIConfig(base_url="https://example.test/v1", api_keys=["k"])
    client = httpx.Client(
        base_url="https://example.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAIEmbedding(config, model="m", client=client)
    vectors = provider.embed(["a", "b"])
    assert pytest.approx(vectors[0].tolist()) == [1.0, 0.0]
    assert pytest.approx(vectors[1].tolist()) == [0.0, 1.0]


def test_embedding_http_error_raises() -> None:
    config = OpenAIConfig(base_url="https://example.test/v1", api_keys=["k"])
    client = httpx.Client(
        base_url="https://example.test/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")),
    )
    provider = OpenAIEmbedding(config, model="m", client=client)
    with pytest.raises(InferenceError, match="500"):
        provider.embed_one("x")


def test_embedding_malformed_response_raises() -> None:
    config = OpenAIConfig(base_url="https://example.test/v1", api_keys=["k"])
    client = httpx.Client(
        base_url="https://example.test/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1})),
    )
    provider = OpenAIEmbedding(config, model="m", client=client)
    with pytest.raises(InferenceError, match="无法解析"):
        provider.embed_one("x")


def test_llama_server_embedding_omits_model_when_empty() -> None:
    """llama-server 用自身加载的模型，model 留空时不应发送 model 字段。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    client = httpx.Client(
        base_url="http://127.0.0.1:8081", transport=httpx.MockTransport(handler)
    )
    provider = LlamaServerEmbedding(LlamaServerConfig(), model="", client=client)
    provider.embed_one("hi")
    assert "model" not in seen["body"]
    assert seen["url"].endswith("/v1/embeddings")
    assert provider.name == "llama_server"


def test_empty_batch_returns_empty() -> None:
    config = OpenAIConfig(base_url="https://example.test/v1", api_keys=["k"])
    client = httpx.Client(
        base_url="https://example.test/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []})),
    )
    provider = OpenAIEmbedding(config, model="m", client=client)
    assert provider.embed([]) == []


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------
def test_build_hashing_provider(tmp_root) -> None:
    cfg = load_config(root=tmp_root, environ={})
    provider = build_embedding_provider(cfg)
    assert isinstance(provider, HashingEmbedding)
    assert provider.dim == cfg.memory.embedding_dim


def test_build_openai_provider(tmp_root) -> None:
    cfg = load_config(
        root=tmp_root,
        environ={},
        overrides={"memory": {"embedding_backend": "openai", "embedding_model": "emb"}},
    )
    provider = build_embedding_provider(cfg)
    assert isinstance(provider, OpenAIEmbedding)
    assert provider.model == "emb"


def test_build_llama_server_provider(tmp_root) -> None:
    cfg = load_config(
        root=tmp_root, environ={}, overrides={"memory": {"embedding_backend": "llama_server"}}
    )
    assert isinstance(build_embedding_provider(cfg), LlamaServerEmbedding)


def test_build_unknown_provider_raises(tmp_root) -> None:
    cfg = load_config(
        root=tmp_root, environ={}, overrides={"memory": {"embedding_backend": "magic"}}
    )
    with pytest.raises(ValueError, match="未知的 embedding 后端"):
        build_embedding_provider(cfg)