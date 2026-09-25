"""推理层接口（base）测试。"""

from __future__ import annotations

import pytest

from memo_role.inference.base import (
    ChatBackend,
    ChatMessage,
    GenParams,
    messages_to_payload,
)


class EchoBackend(ChatBackend):
    """只实现 chat 的最小后端，用于验证默认实现。"""

    name = "echo"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, params=None) -> str:
        self.calls += 1
        return "echo:" + messages[-1].content

    def is_available(self) -> bool:
        return True


def test_chat_message_to_dict_omits_empty_name() -> None:
    assert ChatMessage("user", "hi").to_dict() == {"role": "user", "content": "hi"}
    assert ChatMessage("user", "hi", name="小明").to_dict() == {
        "role": "user",
        "content": "hi",
        "name": "小明",
    }


def test_gen_params_to_payload() -> None:
    params = GenParams(temperature=0.5, top_p=0.8, max_tokens=64)
    assert params.to_payload() == {
        "temperature": 0.5,
        "top_p": 0.8,
        "max_tokens": 64,
    }
    # stop 为空时不应出现该字段（部分服务对空数组会报错）
    assert "stop" not in params.to_payload()
    assert GenParams(stop=["\n"]).to_payload()["stop"] == ["\n"]


def test_messages_to_payload() -> None:
    payload = messages_to_payload([ChatMessage("system", "s"), ChatMessage("user", "u")])
    assert payload == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]


def test_default_stream_falls_back_to_chat() -> None:
    """未实现 stream 的后端应一次性产出完整结果。"""
    backend = EchoBackend()
    assert list(backend.stream([ChatMessage("user", "hi")])) == ["echo:hi"]
    assert backend.supports_stream is False


async def test_default_achat_and_astream() -> None:
    """默认异步实现应能正常工作（内部走线程池）。"""
    backend = EchoBackend()
    assert await backend.achat([ChatMessage("user", "a")]) == "echo:a"
    chunks = [c async for c in backend.astream([ChatMessage("user", "b")])]
    assert chunks == ["echo:b"]


def test_describe_default_contains_no_secrets() -> None:
    info = EchoBackend().describe()
    assert info["backend"] == "echo"
    assert info["available"] is True


def test_abstract_backend_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        ChatBackend()  # type: ignore[abstract]