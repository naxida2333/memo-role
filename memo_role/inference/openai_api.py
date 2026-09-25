"""OpenAI 兼容 API 后端。

用于接入第三方 / 自建的 OpenAI 兼容服务（vLLM、one-api、Ollama 的兼容层等），
核心特性是**多密钥自动轮询**：多个 key 按顺序轮转，遇到鉴权失败、限流或
服务端错误时自动换下一个 key 重试，避免单个 key 被打爆或额度耗尽就整体不可用。

密钥仅保存在本地配置中，``describe()`` 只返回掩码，不会泄漏明文。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Sequence

import httpx

from ..logging_setup import get_logger
from .base import (
    ChatBackend,
    ChatMessage,
    GenParams,
    InferenceError,
    messages_to_payload,
)

logger = get_logger(__name__)

#: 遇到这些状态码时切换下一个密钥重试
RETRY_STATUS = {401, 403, 408, 409, 429, 500, 502, 503, 504}


def mask_key(key: str) -> str:
    """把密钥打码，用于日志与后台展示。"""
    if not key:
        return ""
    if len(key) <= 10:
        return "***"
    return f"{key[:4]}***{key[-4:]}"


class OpenAICompatBackend(ChatBackend):
    """OpenAI 兼容 API 调用（含多密钥轮询）。"""

    name = "openai_api"
    supports_stream = True

    def __init__(
        self,
        config: Any,
        *,
        default_params: Optional[GenParams] = None,
        client: Optional[httpx.Client] = None,
        api_keys: Optional[Sequence[str]] = None,
    ) -> None:
        """
        :param config: ``config.OpenAIConfig``
        :param client: 注入的 httpx 客户端（测试用）
        :param api_keys: 覆盖配置中的密钥列表（测试用）
        """
        self.config = config
        # 过滤空串，避免配置里留下占位符导致请求头为空
        keys = list(config.api_keys if api_keys is None else api_keys)
        self.api_keys: List[str] = [k for k in keys if k]
        self.default_params = default_params or GenParams()

        self._key_index = 0
        self._client = client

    # ------------------------------------------------------------------
    # 客户端与密钥
    # ------------------------------------------------------------------
    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.config.base_url,
                timeout=httpx.Timeout(self.config.timeout, connect=5.0),
            )
        return self._client

    def _max_attempts(self) -> int:
        """单次请求最多尝试的密钥数。"""
        total = len(self.api_keys)
        if total == 0:
            return 1  # 无密钥场景（如本地免鉴权服务）只尝试一次
        configured = self.config.max_key_attempts or total
        return max(1, min(configured, total))

    def _next_key(self) -> Optional[str]:
        """取下一个密钥并推进轮询指针（真正的轮转，均衡分摊额度）。"""
        if not self.api_keys:
            return None
        idx = self._key_index % len(self.api_keys)
        self._key_index = (idx + 1) % len(self.api_keys)
        return self.api_keys[idx]

    def _headers(self, key: Optional[str]) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        """配置了 base_url 即认为「可用」（真实连通性在调用时才知道）。"""
        return bool(self.config.base_url)

    def describe(self) -> Dict[str, Any]:
        active = self.api_keys[self._key_index % len(self.api_keys)] if self.api_keys else None
        return {
            "backend": self.name,
            "base_url": self.config.base_url,
            "model": self.config.model,
            "key_count": len(self.api_keys),
            "active_key": mask_key(active or ""),
        }

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # 请求构造
    # ------------------------------------------------------------------
    def _build_payload(
        self, messages: Sequence[ChatMessage], params: Optional[GenParams], stream: bool
    ) -> Dict[str, Any]:
        effective = params or self.default_params
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages_to_payload(messages),
            "stream": stream,
        }
        payload.update(effective.to_payload())
        return payload

    # ------------------------------------------------------------------
    # 推理
    # ------------------------------------------------------------------
    def chat(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams] = None,
    ) -> str:
        """非流式对话补全，失败时自动轮换密钥重试。"""
        client = self._get_client()
        payload = self._build_payload(messages, params, stream=False)

        last_error: Optional[Exception] = None
        for _ in range(self._max_attempts()):
            key = self._next_key()
            try:
                resp = client.post(
                    "/chat/completions", json=payload, headers=self._headers(key)
                )
            except httpx.HTTPError as exc:
                last_error = InferenceError(f"请求 API 失败：{exc}")
                logger.warning("密钥 %s 请求异常，尝试下一个密钥", mask_key(key or ""))
                continue

            if resp.status_code == 200:
                return self._extract_content(resp.json())

            last_error = InferenceError(
                f"API 返回 {resp.status_code}：{resp.text[:200]}"
            )
            if resp.status_code in RETRY_STATUS:
                logger.warning(
                    "密钥 %s 返回 %s，切换下一个密钥",
                    mask_key(key or ""),
                    resp.status_code,
                )
                continue
            break  # 其它错误（如 400 参数错误）换密钥也没用

        raise last_error or InferenceError("没有可用的 API 密钥")

    def stream(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams] = None,
    ) -> Iterator[str]:
        """流式对话补全（SSE），在开始输出前会轮换密钥重试。"""
        client = self._get_client()
        payload = self._build_payload(messages, params, stream=True)

        last_error: Optional[Exception] = None
        for _ in range(self._max_attempts()):
            key = self._next_key()
            try:
                with client.stream(
                    "POST",
                    "/chat/completions",
                    json=payload,
                    headers=self._headers(key),
                ) as resp:
                    if resp.status_code != 200:
                        resp.read()
                        last_error = InferenceError(
                            f"API 返回 {resp.status_code}：{resp.text[:200]}"
                        )
                        if resp.status_code in RETRY_STATUS:
                            logger.warning(
                                "密钥 %s 返回 %s，切换下一个密钥",
                                mask_key(key or ""),
                                resp.status_code,
                            )
                            continue
                        break
                    for line in resp.iter_lines():
                        chunk = _parse_stream_line(line)
                        if chunk:
                            yield chunk
                    return
            except httpx.HTTPError as exc:
                last_error = InferenceError(f"请求 API 失败：{exc}")
                continue

        raise last_error or InferenceError("没有可用的 API 密钥")

    @staticmethod
    def _extract_content(data: Dict[str, Any]) -> str:
        """从 OpenAI 风格响应中取出文本。"""
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InferenceError(f"无法解析 API 响应：{data}") from exc


def _parse_stream_line(line: str) -> str:
    """解析 SSE 行，返回增量文本。"""
    if not line:
        return ""
    text = line.strip()
    if not text.startswith("data:"):
        return ""
    data = text[len("data:") :].strip()
    if not data or data == "[DONE]":
        return ""
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return ""
    try:
        delta = payload["choices"][0].get("delta") or {}
    except (KeyError, IndexError, TypeError):
        return ""
    return delta.get("content") or ""