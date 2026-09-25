"""llama-cpp-python 进程内后端。

直接在 Python 进程内加载 GGUF，省去一层 HTTP，但代价是：

- 需要在目标设备上编译 ``llama-cpp-python``，安卓 Termux / PRoot 下容易失败
- 模型常驻主进程内存，低配设备内存压力大
- 切换模型需要重新加载（耗时且会占用双份内存）

因此在低配 / 安卓场景下推荐使用 :mod:`memo_role.inference.llama_server`，
本后端作为「有条件时的更优选择」保留。

``llama_cpp`` 采用延迟导入：未安装时模块本身仍可导入，只在真正调用时抛出
``BackendUnavailableError``，避免拖垮整个应用启动。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Sequence

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

#: 未安装依赖时的统一提示
INSTALL_HINT = (
    "未安装 llama-cpp-python。请执行 `pip install llama-cpp-python`，"
    "或改用 inference.backend=llama_server（安卓 / 低配设备推荐）。"
)


class LlamaCppBackend(ChatBackend):
    """进程内 GGUF 推理。"""

    name = "llama_cpp"
    supports_stream = True

    def __init__(
        self,
        model_path: Optional[Path | str] = None,
        *,
        n_ctx: int = 2048,
        n_threads: int = 2,
        n_gpu_layers: int = 0,
        chat_format: Optional[str] = None,
        default_params: Optional[GenParams] = None,
        llm_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        """
        :param model_path: GGUF 模型文件路径
        :param chat_format: 覆盖 llama.cpp 自动推断的对话模板
        :param llm_factory: 注入的模型加载函数（测试用），
            返回对象需实现 ``create_chat_completion(**kwargs)``
        """
        self.model_path = Path(model_path) if model_path else None
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.n_gpu_layers = n_gpu_layers
        self.chat_format = chat_format
        self.default_params = default_params or GenParams()

        self._llm_factory = llm_factory
        self._llm: Optional[Any] = None

    # ------------------------------------------------------------------
    # 模型加载
    # ------------------------------------------------------------------
    @staticmethod
    def dependency_installed() -> bool:
        """检查 ``llama_cpp`` 是否可导入。"""
        return importlib.util.find_spec("llama_cpp") is not None

    def _load_llm(self) -> Any:
        """加载模型（惰性，只做一次）。"""
        if self._llm is not None:
            return self._llm

        if self._llm_factory is not None:
            self._llm = self._llm_factory()
            return self._llm

        if not self.dependency_installed():
            raise BackendUnavailableError(INSTALL_HINT)
        if self.model_path is None:
            raise BackendUnavailableError("未指定模型文件：请先在模型目录下载 GGUF")

        from llama_cpp import Llama  # 延迟导入，避免启动即失败

        logger.info("进程内加载模型：%s", self.model_path)
        self._llm = Llama(
            model_path=str(self.model_path),
            n_ctx=self.n_ctx,
            n_threads=self.n_threads,
            n_gpu_layers=self.n_gpu_layers,
            chat_format=self.chat_format,
            verbose=False,
        )
        return self._llm

    def is_available(self) -> bool:
        """模型文件存在且依赖可用（或已注入工厂）。"""
        if self.model_path is None:
            return False
        if not self.model_path.exists():
            return False
        return self._llm_factory is not None or self.dependency_installed()

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": self.name,
            "model_path": str(self.model_path) if self.model_path else None,
            "dependency_installed": self.dependency_installed(),
            "loaded": self._llm is not None,
            "n_ctx": self.n_ctx,
            "n_threads": self.n_threads,
            "n_gpu_layers": self.n_gpu_layers,
        }

    def close(self) -> None:
        """释放模型占用。"""
        self._llm = None

    # ------------------------------------------------------------------
    # 推理
    # ------------------------------------------------------------------
    def _completion_kwargs(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams],
        stream: bool,
    ) -> Dict[str, Any]:
        effective = params or self.default_params
        kwargs: Dict[str, Any] = {
            "messages": messages_to_payload(messages),
            "stream": stream,
        }
        kwargs.update(effective.to_payload())
        return kwargs

    def chat(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams] = None,
    ) -> str:
        """同步对话补全。"""
        llm = self._load_llm()
        try:
            result = llm.create_chat_completion(
                **self._completion_kwargs(messages, params, stream=False)
            )
        except Exception as exc:  # noqa: BLE001 - 统一包装成推理错误
            raise InferenceError(f"llama_cpp 推理失败：{exc}") from exc
        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InferenceError(f"无法解析 llama_cpp 响应：{result}") from exc

    def stream(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams] = None,
    ) -> Iterator[str]:
        """流式对话补全。"""
        llm = self._load_llm()
        try:
            for chunk in llm.create_chat_completion(
                **self._completion_kwargs(messages, params, stream=True)
            ):
                try:
                    delta = chunk["choices"][0].get("delta") or {}
                except (KeyError, IndexError, TypeError):
                    continue
                content = delta.get("content")
                if content:
                    yield content
        except Exception as exc:  # noqa: BLE001
            raise InferenceError(f"llama_cpp 流式推理失败：{exc}") from exc