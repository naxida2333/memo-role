"""向量化（embedding）提供者。

三种实现，通过 ``memory.embedding_backend`` 选择：

==================  ==========================================================
``hashing``         零依赖的**词法**向量（特征哈希）。不加载任何模型，
                    对中英文做词/字 + 二元组哈希。它捕捉的是**字面重叠**，
                    不是真正的语义相似 —— 但在超低配设备上无需额外模型，
                    且能覆盖「同词/近词召回」这类主要场景。
``llama_server``    调用 llama-server 的 ``/v1/embeddings``，使用真实
                    embedding 模型，语义召回效果最好（需服务已加载模型）。
``openai``          调用 OpenAI 兼容的 ``/embeddings`` 接口。
==================  ==========================================================

重要说明：不同 provider 产出的向量维度与语义空间不同，**不可混用**。
每条记忆的向量都会记录 ``signature``（provider 标识 + 维度 + 模型名），
召回时只比较 signature 相同的向量，避免把不同来源的向量拿去算余弦相似度。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Sequence

import numpy as np

from ..logging_setup import get_logger

logger = get_logger(__name__)

#: CJK 统一表意文字范围
_CJK_START = "\u4e00"
_CJK_END = "\u9fff"


class EmbeddingProvider(ABC):
    """向量化接口。"""

    #: 提供者标识（hashing / llama_server / openai）
    name: str = "base"

    @property
    @abstractmethod
    def signature(self) -> str:
        """向量空间标识：signature 不同的向量不可互相比较。"""

    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度。"""

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> List[np.ndarray]:
        """批量向量化，返回单位向量列表。"""

    def embed_one(self, text: str) -> np.ndarray:
        """单条向量化。"""
        return self.embed([text])[0]

    def describe(self) -> Dict[str, Any]:
        """供管理后台展示。"""
        return {"backend": self.name, "dim": self.dim, "signature": self.signature}


# ----------------------------------------------------------------------
# 零依赖：特征哈希
# ----------------------------------------------------------------------
class HashingEmbedding(EmbeddingProvider):
    """基于特征哈希的词法向量。

    做法：

    1. 抽取 token：拉丁词（连续字母数字） + CJK 单字 + CJK 相邻二元组
    2. 每个 token 用 ``md5`` 映射到固定维度（**必须用 hashlib**，
       Python 内置 ``hash()`` 每次进程启动都会加盐，无法跨重启复现）
    3. 用另一字节决定正负号（signed hashing），减轻哈希碰撞带来的偏差
    4. 词频做 sublinear 缩放（``1 + log(tf)``）后 L2 归一化
    """

    name = "hashing"

    def __init__(self, dim: int = 256) -> None:
        if dim <= 0:
            raise ValueError("向量维度必须为正整数")
        self._dim = int(dim)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def signature(self) -> str:
        return f"hashing:{self._dim}"

    # -- token 抽取 -----------------------------------------------------
    @staticmethod
    def tokenize(text: str) -> List[str]:
        """把文本切成 token 列表。"""
        if not text:
            return []
        lowered = text.lower()
        tokens: List[str] = []

        buf: List[str] = []  # 累积中的拉丁词
        cjk_run: List[str] = []  # 累积中的 CJK 连续段

        def flush_latin() -> None:
            if buf:
                tokens.append("".join(buf))
                buf.clear()

        def flush_cjk() -> None:
            if cjk_run:
                # 单字 + 相邻二元组，兼顾「词」与「字」级别的匹配
                tokens.extend(cjk_run)
                tokens.extend(
                    cjk_run[i] + cjk_run[i + 1] for i in range(len(cjk_run) - 1)
                )
                cjk_run.clear()

        for ch in lowered:
            if ch.isalnum() and not (_CJK_START <= ch <= _CJK_END):
                flush_cjk()
                buf.append(ch)
            elif _CJK_START <= ch <= _CJK_END:
                flush_latin()
                cjk_run.append(ch)
            else:
                flush_latin()
                flush_cjk()
        flush_latin()
        flush_cjk()
        return tokens

    @classmethod
    def _hash_token(cls, token: str) -> tuple[int, float]:
        """返回 ``(维度下标, 符号)``。"""
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big")
        sign = 1.0 if digest[4] & 1 else -1.0
        return index, sign

    def embed(self, texts: Sequence[str]) -> List[np.ndarray]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> np.ndarray:
        vector = np.zeros(self._dim, dtype=np.float32)
        tokens = self.tokenize(text)
        if not tokens:
            return vector

        counts: Dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1

        for token, count in counts.items():
            index, sign = self._hash_token(token)
            # sublinear tf：避免长文本中高频词过度主导
            weight = (1.0 + float(np.log(count))) * sign
            vector[index % self._dim] += weight

        return _l2_normalize(vector)


# ----------------------------------------------------------------------
# 远程 embedding 服务
# ----------------------------------------------------------------------
class _RemoteEmbedding(EmbeddingProvider):
    """远程 embedding 的公共逻辑（签名缓存 + 维度探测）。"""

    def __init__(self, model: str, client: Any) -> None:
        self.model = model
        self._client = client
        self._dim: int = 0

    @property
    def dim(self) -> int:
        # 维度在首次真实请求后才能确定；未确定前返回 0
        return self._dim

    @property
    def signature(self) -> str:
        return f"{self.name}:{self.model or 'default'}:{self._dim}"

    def _post_embeddings(self, texts: Sequence[str]) -> List[np.ndarray]:
        from ..inference.base import InferenceError

        payload: Dict[str, Any] = {"input": list(texts)}
        if self.model:
            payload["model"] = self.model
        try:
            resp = self._client.post("/v1/embeddings", json=payload)
            if resp is None:  # pragma: no cover - 防御性
                raise InferenceError("embedding 服务未返回响应")
            if resp.status_code != 200:
                raise InferenceError(
                    f"embedding 服务返回 {resp.status_code}：{resp.text[:200]}"
                )
            data = resp.json()
        except InferenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络异常统一包装
            raise InferenceError(f"请求 embedding 服务失败：{exc}") from exc

        try:
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            vectors = [np.asarray(item["embedding"], dtype=np.float32) for item in items]
        except (KeyError, TypeError) as exc:
            raise InferenceError(f"无法解析 embedding 响应：{data}") from exc

        if vectors:
            self._dim = int(vectors[0].shape[0])
        return [_l2_normalize(v) for v in vectors]

    def embed(self, texts: Sequence[str]) -> List[np.ndarray]:
        if not texts:
            return []
        return self._post_embeddings(texts)


class LlamaServerEmbedding(_RemoteEmbedding):
    """通过 llama-server 的 ``/v1/embeddings`` 做向量化。

    llama-server 需以 ``--embeddings`` 启动；``model`` 留空时使用其已加载模型。
    """

    name = "llama_server"

    def __init__(self, config: Any, model: str = "", client: Any = None) -> None:
        import httpx

        if client is None:
            client = httpx.Client(
                base_url=f"http://{config.host}:{config.port}",
                timeout=httpx.Timeout(120.0, connect=5.0),
            )
        super().__init__(model=model, client=client)


class OpenAIEmbedding(_RemoteEmbedding):
    """通过 OpenAI 兼容的 ``/embeddings`` 做向量化。"""

    name = "openai"

    def __init__(self, config: Any, model: str = "", client: Any = None) -> None:
        import httpx

        if client is None:
            key = config.api_keys[0] if config.api_keys else ""
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            client = httpx.Client(
                base_url=config.base_url,
                headers=headers,
                timeout=httpx.Timeout(config.timeout, connect=5.0),
            )
        elif model and config.api_keys:
            # 注入客户端时（测试用）把密钥放进请求头，保证与生产一致
            client.headers.setdefault("Authorization", f"Bearer {config.api_keys[0]}")
        super().__init__(model=model, client=client)


# ----------------------------------------------------------------------
# 工具与工厂
# ----------------------------------------------------------------------
def _l2_normalize(vector: np.ndarray) -> np.ndarray:
    """L2 归一化；零向量原样返回。"""
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return vector
    return (vector / norm).astype(np.float32)


def cosine_similarity(matrix: np.ndarray, vector: np.ndarray) -> np.ndarray:
    """矩阵与单向量逐行余弦相似度。

    输入向量已 L2 归一化，因此等价于点积；此处仍做一次除法以兼容未归一化的输入。
    """
    if matrix.size == 0:
        return np.zeros((0,), dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0.0] = 1.0
    vec_norm = float(np.linalg.norm(vector)) or 1.0
    return (matrix @ vector) / (norms * vec_norm)


def build_embedding_provider(cfg: Any, *, client: Any = None) -> EmbeddingProvider:
    """按 ``memory.embedding_backend`` 构造向量化提供者。"""
    backend = (cfg.memory.embedding_backend or "hashing").strip().lower()

    if backend == "hashing":
        return HashingEmbedding(dim=cfg.memory.embedding_dim)

    if backend == "llama_server":
        logger.info("使用 llama-server embedding（模型：%s）", cfg.memory.embedding_model or "已加载模型")
        return LlamaServerEmbedding(
            cfg.inference.llama_server, model=cfg.memory.embedding_model, client=client
        )

    if backend == "openai":
        return OpenAIEmbedding(
            cfg.inference.openai, model=cfg.memory.embedding_model, client=client
        )

    raise ValueError(
        f"未知的 embedding 后端 {backend!r}，可选：hashing / llama_server / openai"
    )