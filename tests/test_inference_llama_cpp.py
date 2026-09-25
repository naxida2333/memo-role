"""llama-cpp-python 后端测试。

真实依赖大概率未安装，因此主要验证：
1. 缺依赖时给出清晰报错而非崩溃
2. 通过注入假 LLM 对象验证消息与参数是否正确传递
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memo_role.inference.base import (
    BackendUnavailableError,
    ChatMessage,
    GenParams,
)
from memo_role.inference.llama_cpp import INSTALL_HINT, LlamaCppBackend


class FakeLlm:
    """假 LLM：记录调用参数并返回固定结果。"""

    def __init__(self) -> None:
        self.calls = []

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter(
                [
                    {"choices": [{"delta": {"content": "你"}}]},
                    {"choices": [{"delta": {"content": "好"}}]},
                    {"choices": [{"delta": {}}]},  # 结束块无内容
                ]
            )
        return {"choices": [{"message": {"content": "完整回复"}}]}


@pytest.fixture
def model_file(tmp_path: Path) -> Path:
    path = tmp_path / "fake.gguf"
    path.write_bytes(b"GGUF")
    return path


def test_dependency_not_installed_guard() -> None:
    """当前环境未安装 llama_cpp，dependency_installed 应为 False。"""
    assert LlamaCppBackend.dependency_installed() is False


def test_is_available_requires_model_file(tmp_path: Path) -> None:
    """模型文件不存在时不可用。"""
    backend = LlamaCppBackend(tmp_path / "missing.gguf")
    assert backend.is_available() is False


def test_chat_without_dependency_raises_hint(model_file: Path) -> None:
    """未安装依赖且未注入工厂时应抛出带安装提示的错误。"""
    backend = LlamaCppBackend(model_file)
    with pytest.raises(BackendUnavailableError) as exc:
        backend.chat([ChatMessage("user", "hi")])
    assert "llama-cpp-python" in str(exc.value)
    assert INSTALL_HINT in str(exc.value)


def test_chat_with_injected_llm(model_file: Path) -> None:
    """注入假 LLM 后应能正常生成，并正确传递消息与参数。"""
    fake = FakeLlm()
    backend = LlamaCppBackend(model_file, llm_factory=lambda: fake)

    assert backend.is_available() is True
    reply = backend.chat(
        [ChatMessage("system", "人设"), ChatMessage("user", "你好", name="小明")],
        GenParams(temperature=0.4, max_tokens=16),
    )
    assert reply == "完整回复"

    kwargs = fake.calls[0]
    assert kwargs["stream"] is False
    assert kwargs["temperature"] == 0.4
    assert kwargs["max_tokens"] == 16
    assert kwargs["messages"][1] == {
        "role": "user",
        "content": "你好",
        "name": "小明",
    }


def test_stream_with_injected_llm(model_file: Path) -> None:
    fake = FakeLlm()
    backend = LlamaCppBackend(model_file, llm_factory=lambda: fake)
    assert list(backend.stream([ChatMessage("user", "hi")])) == ["你", "好"]
    assert fake.calls[0]["stream"] is True


def test_model_loaded_only_once(model_file: Path) -> None:
    """模型应只加载一次（惰性 + 缓存）。"""
    loaded = []

    def factory():
        loaded.append(1)
        return FakeLlm()

    backend = LlamaCppBackend(model_file, llm_factory=factory)
    backend.chat([ChatMessage("user", "a")])
    backend.chat([ChatMessage("user", "b")])
    assert len(loaded) == 1


def test_malformed_response_raises(model_file: Path) -> None:
    class BadLlm:
        def create_chat_completion(self, **kwargs):
            return {"nope": True}

    backend = LlamaCppBackend(model_file, llm_factory=BadLlm)
    from memo_role.inference.base import InferenceError

    with pytest.raises(InferenceError, match="无法解析"):
        backend.chat([ChatMessage("user", "hi")])


def test_describe_and_close(model_file: Path) -> None:
    backend = LlamaCppBackend(model_file, llm_factory=FakeLlm)
    info = backend.describe()
    assert info["backend"] == "llama_cpp"
    assert info["loaded"] is False
    assert info["dependency_installed"] is False
    backend.close()
    assert backend._llm is None