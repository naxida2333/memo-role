"""OpenAI 兼容后端测试，重点验证多密钥轮询。"""

from __future__ import annotations

import httpx
import pytest

from memo_role.config import OpenAIConfig
from memo_role.inference.base import ChatMessage, GenParams, InferenceError
from memo_role.inference.openai_api import OpenAICompatBackend, mask_key


def make_config(**kwargs) -> OpenAIConfig:
    base = {"base_url": "https://example.test/v1", "model": "test-model"}
    base.update(kwargs)
    return OpenAIConfig(**base)


def make_backend(handler, config=None, **kwargs) -> OpenAICompatBackend:
    client = httpx.Client(base_url="https://example.test/v1", transport=httpx.MockTransport(handler))
    return OpenAICompatBackend(config or make_config(), client=client, **kwargs)


def ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "回复"}}]})


# ----------------------------------------------------------------------
# 密钥掩码
# ----------------------------------------------------------------------
def test_mask_key() -> None:
    assert mask_key("") == ""
    assert mask_key("short") == "***"
    assert mask_key("sk-1234567890abcdef") == "sk-1***cdef"
    # 掩码结果不得包含完整密钥
    assert "567890" not in mask_key("sk-1234567890abcdef")


# ----------------------------------------------------------------------
# 单密钥
# ----------------------------------------------------------------------
def test_chat_single_key() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return ok_response(request)

    backend = make_backend(handler, make_config(api_keys=["sk-aaaaaaaaaaaa"]))
    assert backend.chat([ChatMessage("user", "hi")]) == "回复"
    assert seen["auth"] == "Bearer sk-aaaaaaaaaaaa"


def test_no_key_omits_authorization_header() -> None:
    """无密钥场景（本地免鉴权服务）不应发送空的 Authorization。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return ok_response(request)

    backend = make_backend(handler, make_config(api_keys=[]))
    backend.chat([ChatMessage("user", "hi")])
    assert seen["auth"] is None


def test_payload_contains_model_and_params() -> None:
    import json

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return ok_response(request)

    backend = make_backend(handler, make_config(api_keys=["k"]))
    backend.chat([ChatMessage("user", "hi")], GenParams(temperature=0.2, max_tokens=8))
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["temperature"] == 0.2
    assert seen["body"]["max_tokens"] == 8
    assert seen["body"]["stream"] is False


# ----------------------------------------------------------------------
# 多密钥轮询
# ----------------------------------------------------------------------
def test_rotates_key_on_401() -> None:
    """第一个密钥 401 时应自动换第二个密钥重试。"""
    used = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        used.append(auth)
        if "key-one" in auth:
            return httpx.Response(401, json={"error": "bad key"})
        return ok_response(request)

    backend = make_backend(
        handler, make_config(api_keys=["key-one-xxxxxxx", "key-two-yyyyyyy"])
    )
    assert backend.chat([ChatMessage("user", "hi")]) == "回复"
    assert used == ["Bearer key-one-xxxxxxx", "Bearer key-two-yyyyyyy"]


def test_rotation_is_round_robin_across_calls() -> None:
    """两次调用应从不同密钥开始，实现额度分摊。"""
    first_keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        first_keys.append(request.headers.get("authorization"))
        return ok_response(request)

    backend = make_backend(
        handler, make_config(api_keys=["key-a-1111111", "key-b-2222222"])
    )
    backend.chat([ChatMessage("user", "1")])
    backend.chat([ChatMessage("user", "2")])
    assert first_keys[0] == "Bearer key-a-1111111"
    assert first_keys[1] == "Bearer key-b-2222222"


def test_all_keys_failed_raises() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("authorization"))
        return httpx.Response(429, text="rate limited")

    backend = make_backend(
        handler, make_config(api_keys=["k1-11111111", "k2-22222222", "k3-33333333"])
    )
    with pytest.raises(InferenceError, match="429"):
        backend.chat([ChatMessage("user", "hi")])
    assert len(calls) == 3  # 三个密钥都试过


def test_max_key_attempts_limits_retries() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="down")

    backend = make_backend(
        handler,
        make_config(api_keys=["k1-11111111", "k2-22222222"], max_key_attempts=1),
    )
    with pytest.raises(InferenceError, match="503"):
        backend.chat([ChatMessage("user", "hi")])
    assert len(calls) == 1


def test_non_retry_status_does_not_rotate() -> None:
    """400 这类参数错误换密钥也没用，应立即失败。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad request")

    backend = make_backend(
        handler, make_config(api_keys=["k1-11111111", "k2-22222222"])
    )
    with pytest.raises(InferenceError, match="400"):
        backend.chat([ChatMessage("user", "hi")])
    assert len(calls) == 1


def test_network_error_rotates_key() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("authorization"))
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")
        return ok_response(request)

    backend = make_backend(
        handler, make_config(api_keys=["k1-11111111", "k2-22222222"])
    )
    assert backend.chat([ChatMessage("user", "hi")]) == "回复"
    assert len(calls) == 2


# ----------------------------------------------------------------------
# 流式
# ----------------------------------------------------------------------
def test_stream_success() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse.encode()
        )

    backend = make_backend(handler, make_config(api_keys=["k1-11111111"]))
    assert list(backend.stream([ChatMessage("user", "hi")])) == ["a", "b"]


def test_stream_rotates_key_before_output() -> None:
    used = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        used.append(auth)
        if "k1" in auth:
            return httpx.Response(403, text="forbidden")
        sse = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse.encode()
        )

    backend = make_backend(
        handler, make_config(api_keys=["k1-11111111", "k2-22222222"])
    )
    assert list(backend.stream([ChatMessage("user", "hi")])) == ["ok"]
    assert used == ["Bearer k1-11111111", "Bearer k2-22222222"]


# ----------------------------------------------------------------------
# 状态
# ----------------------------------------------------------------------
def test_describe_masks_keys() -> None:
    backend = make_backend(ok_response, make_config(api_keys=["sk-1234567890abcdef"]))
    info = backend.describe()
    assert info["key_count"] == 1
    assert info["active_key"] == "sk-1***cdef"
    # 描述信息中不得出现完整密钥
    assert "567890abcdef" not in str(info)


def test_is_available_requires_base_url() -> None:
    assert make_backend(ok_response, make_config(base_url="")).is_available() is False
    assert make_backend(ok_response).is_available() is True


def test_malformed_response_raises() -> None:
    backend = make_backend(
        lambda r: httpx.Response(200, json={"nope": 1}), make_config(api_keys=["k"])
    )
    with pytest.raises(InferenceError, match="无法解析"):
        backend.chat([ChatMessage("user", "hi")])