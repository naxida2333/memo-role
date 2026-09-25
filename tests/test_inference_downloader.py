"""模型下载器测试：URL 拼接、进度、失败与取消。

用本机临时 HTTP 服务当下载源，完全不依赖外网。
"""

from __future__ import annotations

import functools
import http.server
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from memo_role.inference.catalog import ModelSpec
from memo_role.inference.downloader import (
    DownloadManager,
    download_url,
    known_source_options,
)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """默认实现会把每个请求打到 stderr，测试输出会很脏。"""

    def log_message(self, *args: object) -> None:  # noqa: D102 - 覆盖父类
        pass


@pytest.fixture
def served_file(tmp_path: Path) -> Iterator[str]:
    """本机 HTTP 服务，提供一个 4 KiB 的文件，返回它的 URL。"""
    root = tmp_path / "served"
    root.mkdir()
    (root / "model.gguf").write_bytes(b"g" * 4096)

    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/model.gguf"
    finally:
        server.shutdown()
        server.server_close()


def _wait(task, timeout: float = 10.0):
    """等到下载结束。"""
    deadline = time.time() + timeout
    while task.status == "running" and time.time() < deadline:
        time.sleep(0.02)
    return task


# ----------------------------------------------------------------------
# URL 拼接
# ----------------------------------------------------------------------
def test_download_url_uses_source_and_repo() -> None:
    spec = ModelSpec(
        id="m",
        name="M",
        params="1B",
        quant="Q4_K_M",
        approx_size_mb=1,
        filename="m.gguf",
        hf_repo="owner/repo",
        hf_file="m-q4_k_m.gguf",
    )
    assert (
        download_url("https://hf-mirror.com/", spec)
        == "https://hf-mirror.com/owner/repo/resolve/main/m-q4_k_m.gguf"
    )
    # 不传源时用默认值（镜像）
    assert download_url("", spec).startswith("https://hf-mirror.com/")


def test_known_sources_include_official_and_mirror() -> None:
    values = {item["value"] for item in known_source_options()}
    assert "https://huggingface.co" in values
    assert "https://hf-mirror.com" in values


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({"Content-Length": "1700"}, 1700),
        # HuggingFace 的 CDN 有时只给 Content-Range
        ({"Content-Length": None, "Content-Range": "bytes 0-15/105454432"}, 105454432),
        ({"Content-Range": "bytes 0-15/*"}, 0),
        ({}, 0),
        ({"Content-Length": "abc"}, 0),
    ],
)
def test_total_size_prefers_length_then_range(headers, expected: int) -> None:
    from memo_role.inference.downloader import _total_size

    assert _total_size(headers) == expected


# ----------------------------------------------------------------------
# 下载
# ----------------------------------------------------------------------
def test_download_writes_file_and_reports_progress(served_file: str, tmp_path: Path) -> None:
    manager = DownloadManager()
    dest = tmp_path / "models" / "demo.gguf"
    task = manager.start(served_file, dest)

    _wait(task)
    assert task.status == "done"
    assert task.done == 4096
    assert task.total == 4096
    assert dest.read_bytes() == b"g" * 4096
    # 临时文件必须清掉，否则注册表会把 .part 也当成模型文件
    assert not (tmp_path / "models" / "demo.gguf.part").exists()

    info = manager.describe()
    assert info is not None and info["status"] == "done" and info["ratio"] == 1.0
    assert manager.is_running() is False


def test_download_failure_cleans_partial_file(tmp_path: Path) -> None:
    manager = DownloadManager()
    dest = tmp_path / "bad.gguf"
    # 端口 1 上不会有服务，连接必然失败
    task = manager.start("http://127.0.0.1:1/nope.gguf", dest)

    _wait(task)
    assert task.status == "failed"
    assert task.error
    assert not dest.exists()
    assert not dest.with_name("bad.gguf.part").exists()


def test_download_404_reports_http_status(served_file: str, tmp_path: Path) -> None:
    manager = DownloadManager()
    missing = served_file.replace("model.gguf", "not-here.gguf")
    task = _wait(manager.start(missing, tmp_path / "x.gguf"))

    assert task.status == "failed"
    assert "404" in task.error


def test_download_can_be_cancelled(served_file: str, tmp_path: Path) -> None:
    manager = DownloadManager()
    dest = tmp_path / "cancel.gguf"
    task = manager.start(served_file, dest)
    manager.cancel()

    _wait(task)
    assert task.status in {"canceled", "done"}  # 极小文件可能正好下完
    if task.status == "canceled":
        assert not dest.exists()


def test_second_download_is_rejected_while_running(served_file: str, tmp_path: Path) -> None:
    manager = DownloadManager()
    task = manager.start(served_file, tmp_path / "one.gguf")
    try:
        with pytest.raises(RuntimeError):
            manager.start(served_file, tmp_path / "two.gguf")
    finally:
        _wait(task)
    # 结束后可以再开一个
    assert manager.start(served_file, tmp_path / "three.gguf") is not None
