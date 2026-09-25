"""模型管理 API 测试：清单、后端切换、第三方 API 试连、下载、切完真能聊。

全部离线：下载用本机临时 HTTP 服务，第三方 API 也用一个假的 OpenAI 兼容服务。
"""

from __future__ import annotations

import functools
import http.server
import json
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from memo_role.web.state import RuntimeSettings


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # noqa: D102 - 覆盖父类
        pass


@pytest.fixture
def served_file(tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "served"
    root.mkdir()
    (root / "demo.gguf").write_bytes(b"z" * 2048)

    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/demo.gguf"
    finally:
        server.shutdown()
        server.server_close()


class _FakeOpenAIHandler(http.server.BaseHTTPRequestHandler):
    """极简 OpenAI 兼容服务，够验证「切过去以后真能聊」即可。"""

    reply = "你好，我是第三方模型"

    def log_message(self, *args: object) -> None:  # noqa: D102 - 覆盖父类
        pass

    def do_POST(self) -> None:  # noqa: N802 - 父类命名
        if not self.path.startswith("/v1/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")

        stream = bool(payload.get("stream"))
        self.send_response(200)
        self.send_header(
            "Content-Type", "text/event-stream" if stream else "application/json"
        )
        self.end_headers()

        if stream:
            half = len(self.reply) // 2
            for part in (self.reply[:half], self.reply[half:]):
                chunk = json.dumps(
                    {"choices": [{"delta": {"content": part}}]}, ensure_ascii=False
                )
                self.wfile.write(f"data: {chunk}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps(
                {"choices": [{"message": {"content": self.reply}}]}, ensure_ascii=False
            )
            self.wfile.write(body.encode("utf-8"))


@pytest.fixture
def fake_openai() -> Iterator[str]:
    """返回假服务的 base_url（带 /v1）。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def real_state(tmp_root):
    """不注入假后端：切到 openai_api 后真的会发 HTTP 请求（打到上面的假服务）。"""
    from memo_role.config import load_config
    from memo_role.web.state import build_state

    return build_state(load_config(root=tmp_root, environ={}))


@pytest.fixture
def real_client(real_state):
    from fastapi.testclient import TestClient

    from memo_role.web.app import create_app

    with TestClient(create_app(real_state)) as client:
        yield client


# ----------------------------------------------------------------------
# 清单
# ----------------------------------------------------------------------
def test_list_models_reports_inference_and_download(web_client) -> None:
    data = web_client.get("/api/models").json()

    assert "llama_server" in data["inference"]["backends"]
    assert data["inference"]["openai"]["base_url"]
    # 密钥只给条数，不给明文
    assert "api_keys" not in data["inference"]["openai"]
    assert data["download"]["source"]
    assert data["download"]["task"] is None
    assert any(m["id"] == "smollm2-135m" for m in data["models"])


# ----------------------------------------------------------------------
# 后端切换
# ----------------------------------------------------------------------
def test_switch_to_openai_backend_persists(web_state, web_client) -> None:
    resp = web_client.post(
        "/api/models/backend",
        json={
            "backend": "openai_api",
            "base_url": "https://api.example.com/v1/",
            "model": "deepseek-chat",
            "api_keys": ["sk-a", "  ", "sk-b"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["openai_style"] is True

    # 立即生效：配置与当前后端都换了
    assert web_state.cfg.inference.backend == "openai_api"
    assert web_state.cfg.inference.openai.base_url == "https://api.example.com/v1"
    # 空白密钥被丢掉
    assert web_state.cfg.inference.openai.api_keys == ["sk-a", "sk-b"]
    # 落盘：重启后仍是这个后端
    saved = RuntimeSettings.load(web_state.runtime.path)
    assert saved.inference["backend"] == "openai_api"
    assert saved.inference["openai"]["model"] == "deepseek-chat"


def test_switch_to_openai_keeps_keys_when_omitted(web_state, web_client) -> None:
    """只想改模型名时不该把已保存的密钥弄丢（前端留空即不下发 api_keys）。"""
    base = {
        "backend": "openai_api",
        "base_url": "https://api.example.com/v1",
    }
    web_client.post(
        "/api/models/backend", json={**base, "model": "m1", "api_keys": ["sk-keep"]}
    )
    web_client.post("/api/models/backend", json={**base, "model": "m2"})

    assert web_state.cfg.inference.openai.api_keys == ["sk-keep"]
    assert web_state.cfg.inference.openai.model == "m2"


def test_switch_to_openai_clears_local_default_model(web_state, web_client) -> None:
    """本地模型 id 不能被带进第三方 API 的 model 字段。"""
    web_client.post("/api/models/default", json={"model_id": "qwen2.5-0.5b"})
    assert web_state.runtime.model_id == "qwen2.5-0.5b"

    web_client.post(
        "/api/models/backend",
        json={
            "backend": "openai_api",
            "base_url": "https://api.example.com/v1",
            "model": "gpt-4o-mini",
        },
    )
    assert web_state.cfg.inference.model == ""
    assert web_state.runtime.model_id == ""


def test_switch_backend_rejects_unknown(web_client) -> None:
    resp = web_client.post("/api/models/backend", json={"backend": "vllm"})
    assert resp.status_code == 400
    assert "未知的推理后端" in resp.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"backend": "openai_api", "model": "gpt-4o-mini"},  # 缺 base_url
        {"backend": "openai_api", "base_url": "ftp://x/y", "model": "m"},  # 协议不对
        {"backend": "openai_api", "base_url": "https://x/v1"},  # 缺模型名
    ],
)
def test_switch_to_openai_validates_required_fields(web_client, payload) -> None:
    assert web_client.post("/api/models/backend", json=payload).status_code == 400


# ----------------------------------------------------------------------
# 切到第三方 API 之后真的能聊
# ----------------------------------------------------------------------
def test_chat_streams_reply_from_third_party_api(
    real_state, real_client, fake_openai: str
) -> None:
    """整条链路：切换后端 → 建会话 → 流式对话拿到第三方 API 的回复。"""
    resp = real_client.post(
        "/api/models/backend",
        json={
            "backend": "openai_api",
            "base_url": fake_openai,
            "model": "fake-model",
            "api_keys": ["sk-test"],
        },
    )
    assert resp.status_code == 200

    session = real_client.post("/api/dialogue/sessions", json={}).json()["session"]
    with real_client.stream(
        "POST", f"/api/dialogue/sessions/{session['id']}/stream", json={"text": "你好"}
    ) as stream:
        assert stream.status_code == 200
        body = "".join(stream.iter_text())

    assert "你好，我是第三方模型" in body
    # 请求真的带上了密钥与模型名
    assert real_state.cfg.inference.openai.api_keys == ["sk-test"]


def test_api_test_reports_success_against_fake_service(real_client, fake_openai: str) -> None:
    """「测试连接」在能连通时给出延迟与回复片段。"""
    resp = real_client.post(
        "/api/models/api-test",
        json={"base_url": fake_openai, "model": "fake-model"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "第三方模型" in data["reply"]
    assert data["latency_ms"] >= 0


# ----------------------------------------------------------------------
# 第三方 API 试连
# ----------------------------------------------------------------------
def test_api_test_reports_connection_error(web_client) -> None:
    resp = web_client.post(
        "/api/models/api-test",
        json={"base_url": "http://127.0.0.1:1/v1", "model": "m", "timeout": 5},
    )
    assert resp.status_code == 400
    assert "连接失败" in resp.json()["detail"] or "失败" in resp.json()["detail"]


def test_api_test_validates_input(web_client) -> None:
    assert (
        web_client.post(
            "/api/models/api-test", json={"base_url": "nope", "model": "m"}
        ).status_code
        == 400
    )


# ----------------------------------------------------------------------
# 下载源
# ----------------------------------------------------------------------
def test_set_download_source_persists(web_state, web_client) -> None:
    resp = web_client.post("/api/models/source", json={"source": "https://huggingface.co"})
    assert resp.status_code == 200
    assert resp.json()["source"] == "https://huggingface.co"
    assert RuntimeSettings.load(web_state.runtime.path).model_source == (
        "https://huggingface.co"
    )

    # 清空 → 回到默认镜像
    assert web_client.post("/api/models/source", json={"source": ""}).json()["source"]


def test_set_download_source_rejects_bad_scheme(web_client) -> None:
    assert (
        web_client.post("/api/models/source", json={"source": "hf-mirror.com"}).status_code
        == 400
    )


# ----------------------------------------------------------------------
# 下载
# ----------------------------------------------------------------------
def test_download_requires_url_or_model_id(web_client) -> None:
    assert web_client.post("/api/models/download", json={}).status_code == 400


def test_download_model_without_builtin_source_is_rejected(web_client) -> None:
    resp = web_client.post("/api/models/download", json={"model_id": "qwen3.5-0.8b"})
    assert resp.status_code == 400
    assert "从链接导入" in resp.json()["detail"]


def test_download_unknown_model_is_rejected(web_client) -> None:
    resp = web_client.post("/api/models/download", json={"model_id": "no-such-model"})
    assert resp.status_code == 400


def test_download_from_url_lands_in_model_dir(
    web_state, web_client, served_file: str
) -> None:
    resp = web_client.post("/api/models/download", json={"url": served_file})
    assert resp.status_code == 202
    assert resp.json()["task"]["filename"] == "demo.gguf"

    for _ in range(100):
        task = web_client.get("/api/models/download").json()["task"]
        if task and task["status"] != "running":
            break
        time.sleep(0.02)

    assert task["status"] == "done"
    saved = Path(web_state.cfg.model_dir) / "demo.gguf"
    assert saved.stat().st_size == 2048


def test_download_rejects_non_http_url(web_client) -> None:
    resp = web_client.post("/api/models/download", json={"url": "file:///etc/passwd"})
    assert resp.status_code == 400


def test_download_filename_keeps_basename_only(web_client, served_file: str) -> None:
    resp = web_client.post(
        "/api/models/download",
        json={"url": served_file, "filename": "../../escape.gguf"},
    )
    assert resp.status_code == 202
    assert resp.json()["task"]["filename"] == "escape.gguf"
