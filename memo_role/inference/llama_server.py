"""llama-server 后端。

把 llama.cpp 的 ``llama-server`` 作为子进程拉起，通过 HTTP 调用。
相比进程内加载，这种方式的优势：

- 进程隔离：模型崩溃不会带崩 Web / NapCat 主进程
- 切换模型只需重启子进程，主进程无需重新分配内存
- 不需要在安卓 PRoot 上编译 C++ 扩展

注意：本后端只负责「管理子进程 + 发请求」，模型文件需用户自行下载到
``inference.model_dir``。
"""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404 - 需要主动拉起本地推理子进程
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

import httpx

from ..logging_setup import get_logger
from .base import (
    BackendUnavailableError,
    ChatBackend,
    ChatMessage,
    GenParams,
    InferenceError,
    messages_to_payload,
)

logger = get_logger(__name__)


class LlamaServerBackend(ChatBackend):
    """llama-server 子进程 + HTTP 调用。"""

    name = "llama_server"
    supports_stream = True

    def __init__(
        self,
        config: Any,
        model_path: Optional[Path | str] = None,
        *,
        n_ctx: int = 2048,
        n_threads: int = 2,
        n_gpu_layers: int = 0,
        default_params: Optional[GenParams] = None,
        auto_start: bool = True,
        client: Optional[httpx.Client] = None,
        process_factory: Optional[Callable[[List[str]], Any]] = None,
    ) -> None:
        """
        :param config: ``config.LlamaServerConfig``
        :param model_path: GGUF 模型文件路径
        :param client: 注入的 httpx 客户端（测试用；为 None 时按需创建）
        :param process_factory: 注入的进程创建函数（测试用）
        """
        self.config = config
        self.model_path = Path(model_path) if model_path else None
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.n_gpu_layers = n_gpu_layers
        self.default_params = default_params or GenParams()
        self.auto_start = auto_start

        self._client = client
        self._process_factory = process_factory or (
            lambda args: subprocess.Popen(args)  # noqa: S603
        )
        self._process: Optional[Any] = None
        self._ready = False

    # ------------------------------------------------------------------
    # 地址与客户端
    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}"

    def _get_client(self) -> httpx.Client:
        """惰性创建 HTTP 客户端（复用连接）。"""
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(300.0, connect=5.0),
            )
        return self._client

    # ------------------------------------------------------------------
    # 子进程管理
    # ------------------------------------------------------------------
    def build_launch_args(self) -> List[str]:
        """构造 llama-server 启动参数（纯函数，便于测试）。"""
        if self.model_path is None:
            raise BackendUnavailableError(
                "未指定模型文件：请先在模型目录下载 GGUF 或在配置中选择模型"
            )
        binary = self.config.bin_path or "llama-server"
        args = [
            binary,
            "-m",
            str(self.model_path),
            "--host",
            str(self.config.host),
            "--port",
            str(self.config.port),
            "-c",
            str(self.n_ctx),
            "-t",
            str(self.n_threads),
            "-ngl",
            str(self.n_gpu_layers),
        ]
        args.extend(list(self.config.extra_args))
        return args

    def resolve_binary(self) -> str:
        """定位 llama-server 可执行文件，找不到时给出安装提示。"""
        binary = self.config.bin_path or "llama-server"
        if Path(binary).is_absolute():
            if not Path(binary).exists():
                raise BackendUnavailableError(f"llama-server 不存在：{binary}")
            return binary
        found = shutil.which(binary)
        if not found:
            raise BackendUnavailableError(
                f"未找到可执行文件 {binary}。请安装 llama.cpp 并在配置中填写 "
                "inference.llama_server.bin_path，或改用 openai_api 后端。"
            )
        return found

    def start(self) -> None:
        """拉起 llama-server 子进程（已在运行则直接返回）。"""
        if self._process is not None and self._process.poll() is None:
            return
        args = self.build_launch_args()
        args[0] = self.resolve_binary()
        logger.info("启动 llama-server：%s", " ".join(args))
        self._process = self._process_factory(args)
        self._ready = False

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """轮询 ``/health`` 直到服务就绪。"""
        deadline = time.monotonic() + (
            self.config.startup_timeout if timeout is None else timeout
        )
        while time.monotonic() < deadline:
            if self._check_health():
                self._ready = True
                logger.info("llama-server 已就绪：%s", self.base_url)
                return True
            # 子进程意外退出就没必要继续等
            if self._process is not None and self._process.poll() is not None:
                raise BackendUnavailableError(
                    f"llama-server 启动失败，退出码 {self._process.poll()}"
                )
            time.sleep(0.5)
        raise BackendUnavailableError(f"等待 llama-server 就绪超时（{timeout}s）")

    def _check_health(self) -> bool:
        """探测 ``/health``，任何异常都视为未就绪。"""
        try:
            resp = self._get_client().get("/health", timeout=2.0)
        except Exception:  # noqa: BLE001 - 探测失败即未就绪
            return False
        return resp.status_code == 200

    def ensure_ready(self) -> None:
        """确保服务可用；未启动且允许自动启动时拉起子进程。"""
        if self._ready:
            return
        if self._check_health():
            self._ready = True
            return
        if not self.auto_start:
            raise BackendUnavailableError(
                f"llama-server 未运行：{self.base_url}（已禁用自动启动）"
            )
        self.start()
        self.wait_ready()

    def stop(self) -> None:
        """终止子进程。"""
        if self._process is not None and self._process.poll() is None:
            logger.info("停止 llama-server 子进程")
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except Exception:  # noqa: BLE001 - 超时则强杀
                self._process.kill()
        self._process = None
        self._ready = False

    def close(self) -> None:
        """释放子进程与 HTTP 连接。"""
        self.stop()
        if self._client is not None:
            self._client.close()
            self._client = None

    def is_available(self) -> bool:
        """服务是否可用（含自动启动）。"""
        if self._ready or self._check_health():
            return True
        if not self.auto_start:
            return False
        try:
            self.ensure_ready()
            return True
        except BackendUnavailableError:
            logger.warning("llama-server 不可用：%s", self.base_url)
            return False

    def describe(self) -> Dict[str, Any]:
        """供管理后台展示的后端状态。"""
        return {
            "backend": self.name,
            "base_url": self.base_url,
            "model_path": str(self.model_path) if self.model_path else None,
            "n_ctx": self.n_ctx,
            "n_threads": self.n_threads,
            "n_gpu_layers": self.n_gpu_layers,
            "running": self._process is not None and self._process.poll() is None,
            "auto_start": self.auto_start,
        }

    # ------------------------------------------------------------------
    # 推理
    # ------------------------------------------------------------------
    def _build_payload(
        self, messages: Sequence[ChatMessage], params: Optional[GenParams], stream: bool
    ) -> Dict[str, Any]:
        effective = params or self.default_params
        payload: Dict[str, Any] = {"messages": messages_to_payload(messages)}
        payload.update(effective.to_payload())
        payload["stream"] = stream
        return payload

    def chat(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams] = None,
    ) -> str:
        """非流式对话补全。"""
        payload = self._build_payload(messages, params, stream=False)
        try:
            resp = self._get_client().post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise InferenceError(f"请求 llama-server 失败：{exc}") from exc
        if resp.status_code != 200:
            raise InferenceError(
                f"llama-server 返回 {resp.status_code}：{resp.text[:200]}"
            )
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InferenceError(f"无法解析 llama-server 响应：{data}") from exc

    def stream(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams] = None,
    ) -> Iterator[str]:
        """流式对话补全（SSE）。"""
        payload = self._build_payload(messages, params, stream=True)
        try:
            with self._get_client().stream(
                "POST", "/v1/chat/completions", json=payload
            ) as resp:
                if resp.status_code != 200:
                    resp.read()
                    raise InferenceError(
                        f"llama-server 返回 {resp.status_code}：{resp.text[:200]}"
                    )
                for line in resp.iter_lines():
                    chunk = _parse_sse_line(line)
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise InferenceError(f"请求 llama-server 失败：{exc}") from exc


def _parse_sse_line(line: str) -> str:
    """解析单行 SSE，返回增量文本（非内容行返回空串）。"""
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