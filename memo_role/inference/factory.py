"""后端工厂。

根据配置的 ``inference.backend`` 构造对应后端实例，并统一把生成参数
（temperature / top_p / max_tokens）打包成 ``GenParams``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..logging_setup import get_logger
from .base import ChatBackend, GenParams, InferenceError
from .catalog import ModelSpec
from .llama_cpp import LlamaCppBackend
from .llama_server import LlamaServerBackend
from .openai_api import OpenAICompatBackend
from .registry import ModelRegistry

logger = get_logger(__name__)

#: 支持的推理后端
SUPPORTED_BACKENDS = ("llama_server", "llama_cpp", "openai_api")


def gen_params(cfg: Any) -> GenParams:
    """从配置提取生成参数。"""
    inf = cfg.inference
    return GenParams(
        temperature=inf.temperature,
        top_p=inf.top_p,
        max_tokens=inf.max_tokens,
    )


def build_backend(
    cfg: Any,
    *,
    registry: Optional[ModelRegistry] = None,
    spec: Optional[ModelSpec] = None,
    model_id: Optional[str] = None,
    model_path: Optional[Path | str] = None,
    **backend_kwargs: Any,
) -> ChatBackend:
    """构造推理后端。

    ``llama_server`` / ``llama_cpp`` 需要模型文件路径，解析顺序为：

    1. 显式传入的 ``model_path``
    2. 显式传入的 ``spec``
    3. 由 ``model_id``（或 ``cfg.inference.model``）经注册表解析
    4. 注册表默认模型

    :param backend_kwargs: 透传给具体后端构造函数的额外参数（测试注入用）
    """
    name = (cfg.inference.backend or "").strip()
    if name not in SUPPORTED_BACKENDS:
        raise InferenceError(
            f"未知的推理后端 {name!r}，可选：{', '.join(SUPPORTED_BACKENDS)}"
        )

    params = gen_params(cfg)

    # --- 纯 API 后端，不需要本地模型文件 ---
    if name == "openai_api":
        return OpenAICompatBackend(cfg.inference.openai, default_params=params, **backend_kwargs)

    # --- 本地后端：需要解析模型路径 ---
    resolved_path: Optional[Path] = Path(model_path) if model_path else None
    if resolved_path is None:
        active_spec = spec
        if active_spec is None:
            reg = registry or ModelRegistry.load(cfg)
            active_spec, resolved_path = reg.resolve(model_id or cfg.inference.model or None)
        else:
            reg = registry or ModelRegistry.load(cfg)
            resolved_path = reg.path_of(active_spec)

    if name == "llama_server":
        logger.info("使用 llama_server 后端，模型：%s", resolved_path)
        return LlamaServerBackend(
            cfg.inference.llama_server,
            model_path=resolved_path,
            n_ctx=cfg.inference.n_ctx,
            n_threads=cfg.inference.n_threads,
            n_gpu_layers=cfg.inference.n_gpu_layers,
            default_params=params,
            **backend_kwargs,
        )

    logger.info("使用 llama_cpp 后端，模型：%s", resolved_path)
    return LlamaCppBackend(
        model_path=resolved_path,
        n_ctx=cfg.inference.n_ctx,
        n_threads=cfg.inference.n_threads,
        n_gpu_layers=cfg.inference.n_gpu_layers,
        default_params=params,
        **backend_kwargs,
    )