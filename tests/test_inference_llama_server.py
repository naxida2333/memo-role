"""llama-server 后端测试。

不依赖真实的 llama-server：子进程用假对象替代，HTTP 用 httpx.MockTransport。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from memo_role.config import LlamaServerConfig
from memo_role.inference.base import (
    BackendUnavailableError,
    ChatMessage,
    GenParams,
    InferenceError,
)
from memo_role.inference.llama_server import LlamaServerBackend, _parse_sse_line


class FakeProcess:
    """假子进程：可控制 poll() 返回值。"""

    def __init__(self, exit_code=None) -> None:
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 0

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):
        return self.exit_code


def make_client(handler) -> httpx.Client:
    """构造带 MockTransport 的 httpx 客户端。"""
    return httpx.Client(
        base_url="http://127.0.0.1:8081", transport=httpx.MockTransport(handler)
    )


def make_backend(handler, model_path="/models/x.gguf", **kwargs) -> LlamaServerBackend:
    return LlamaServerBackend(
        LlamaServerConfig(),
        model_path=model_path,
        client=make_client(handler),
        **kwargs,
    )


def chat_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": "你好呀"}}]}
    )


# ----------------------------------------------------------------------
# 启动参数
# ----------------------------------------------------------------------
def test_build_launch_args() -> None:
    """启动参数应包含模型、上下文、线程等关键项。"""
    cfg = LlamaServerConfig(bin_path="/opt/llama-server", extra_args=["-fa"])
    backend = LlamaServerBackend(
        cfg,
        model_path="/models/a.gguf",
        n_ctx=1024,
        n_threads=3,
        n_gpu_layers=0,
        client=make_client(chat_ok),
    )
    args = backend.build_launch_args()
    assert args[0] == "/opt/llama-server"
    assert "-m" in args and "/models/a.gguf" in args
    assert args[args.index("-c") + 1] == "1024"
    assert args[args.index("-t") + 1] == "3"
    assert "-ngl" in args
    assert args[-1] == "-fa"  # extra_args 追加在末尾


def test_build_launch_args_without_model_raises() -> None:
    backend = LlamaServerBackend(LlamaServerConfig(), model_path=None)
    with pytest.raises(BackendUnavailableError, match="未指定模型文件"):
        backend.build_launch_args()


def test_resolve_binary_missing_gives_hint() -> None:
    backend = LlamaServerBackend(
        LlamaServerConfig(bin_path="definitely-not-a-real-binary-xyz"), model_path="a.gguf"
    )
    with pytest.raises(BackendUnavailableError, match="未找到可执行文件"):
        backend.resolve_binary()


def test_resolve_binary_absolute_missing() -> None:
    backend = LlamaServerBackend(
        LlamaServerConfig(bin_path="/nope/llama-server"), model_path="a.gguf"
    )
    with pytest.raises(BackendUnavailableError, match="llama-server 不存在"):
        backend.resolve_binary()


# ----------------------------------------------------------------------
# 子进程生命周期
# ----------------------------------------------------------------------
def test_start_uses_process_factory(tmp_path: Path) -> None:
    """start() 应调用注入的进程工厂，并用可执行文件替换 args[0]。"""
    captured = {}

    def factory(args):
        captured["args"] = args
        return FakeProcess()

    binary = tmp_path / "llama-server"
    binary.write_text("", encoding="utf-8")

    backend = LlamaServerBackend(
        LlamaServerConfig(bin_path=str(binary)),
        model_path="a.gguf",
        client=make_client(chat_ok),
        process_factory=factory,
    )
    backend.start()
    assert captured["args"][0] == str(binary)
    # 重复 start 不应再拉起新进程
    backend.start()
    backend.stop()
    assert backend._process is None


def test_start_failure_surfaces_exit_code() -> None:
    """子进程启动即退出时，wait_ready 应报出退出码。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    backend = LlamaServerBackend(
        LlamaServerConfig(bin_path=sys.executable),  # 用真实存在的可执行文件占位
        model_path="a.gguf",
        client=make_client(handler),
        process_factory=lambda args: FakeProcess(exit_code=9),
        auto_start=True,
    )
    backend.start()
    with pytest.raises(BackendUnavailableError, match="退出码 9"):
        backend.wait_ready(timeout=1)


def test_ensure_ready_skips_when_health_ok() -> None:
    """health 正常时不应启动子进程。"""
    started = []
    backend = LlamaServerBackend(
        LlamaServerConfig(),
        model_path="a.gguf",
        client=make_client(lambda r: httpx.Response(200)),
        process_factory=lambda args: started.append(args) or FakeProcess(),
    )
    backend.ensure_ready()
    assert started == []


def test_auto_start_disabled_raises() -> None:
    backend = LlamaServerBackend(
        LlamaServerConfig(),
        model_path="a.gguf",
        client=make_client(lambda r: httpx.Response(503)),
        auto_start=False,
    )
    with pytest.raises(BackendUnavailableError, match="未运行"):
        backend.ensure_ready()
    assert backend.is_available() is False


def test_wait_ready_success() -> None:
    backend = make_backend(lambda r: httpx.Response(200))
    assert backend.wait_ready(timeout=1) is True


# ----------------------------------------------------------------------
# 推理
# ----------------------------------------------------------------------
def test_chat_success_and_payload() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return chat_ok(request)

    backend = make_backend(handler)
    reply = backend.chat(
        [ChatMessage("system", "你是猫娘"), ChatMessage("user", "在吗")],
        GenParams(temperature=0.3, max_tokens=32),
    )
    assert reply == "你好呀"
    assert seen["url"].endswith("/v1/chat/completions")

    body = json.loads(seen["body"])
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 32
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "你是猫娘"}
    assert body["messages"][1]["content"] == "在吗"


def test_chat_http_error_raises_inference_error() -> None:
    backend = make_backend(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(InferenceError, match="500"):
        backend.chat([ChatMessage("user", "hi")])


def test_chat_malformed_response_raises() -> None:
    backend = make_backend(lambda r: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(InferenceError, match="无法解析"):
        backend.chat([ChatMessage("user", "hi")])


def test_stream_yields_chunks() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"你"}}]}\n'
        "\n"
        'data: {"choices":[{"delta":{"content":"好"}}]}\n'
        "\n"
        "data: [DONE]\n"
        "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse.encode()
        )

    backend = make_backend(handler)
    assert list(backend.stream([ChatMessage("user", "hi")])) == ["你", "好"]


def test_stream_http_error_raises() -> None:
    backend = make_backend(lambda r: httpx.Response(503, text="unavailable"))
    with pytest.raises(InferenceError, match="503"):
        list(backend.stream([ChatMessage("user", "hi")]))


def test_parse_sse_line() -> None:
    assert _parse_sse_line("") == ""
    assert _parse_sse_line("event: ping") == ""
    assert _parse_sse_line("data: [DONE]") == ""
    assert _parse_sse_line("data: not-json") == ""
    assert _parse_sse_line('data: {"choices":[{"delta":{"content":"x"}}]}') == "x"
    # 只有 role 没有 content 的起始块
    assert _parse_sse_line('data: {"choices":[{"delta":{"role":"assistant"}}]}') == ""


def test_describe_hides_nothing_sensitive() -> None:
    backend = make_backend(chat_ok, model_path="/models/x.gguf")
    info = backend.describe()
    assert info["backend"] == "llama_server"
    assert info["base_url"] == "http://127.0.0.1:8081"
    assert info["running"] is False


def test_close_releases_client() -> None:
    backend = make_backend(chat_ok)
    backend.close()
    assert backend._client is None


def test_default_python_is_used_as_placeholder_binary() -> None:
    """smoke：用 python 可执行文件当假 llama-server，验证参数拼装不报错。"""
    backend = LlamaServerBackend(
        LlamaServerConfig(bin_path=sys.executable),
        model_path="a.gguf",
        client=make_client(chat_ok),
    )
    assert backend.resolve_binary() == sys.executable