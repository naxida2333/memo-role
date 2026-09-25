"""推理后端池：支持运行时手动切换模型。

低配设备不可能同时把多个本地模型读进内存，所以策略是「**同一时刻只保留一个
活跃后端**」：切换模型时先释放旧的（llama-server 会随之退出子进程、归还内存），
再按新模型重建。这样切换是「卸载 → 装载」，而不是越切越占内存。

对于 ``openai_api`` 后端，切换模型只是改请求里的 ``model`` 字段，不涉及本地资源。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..inference.base import ChatBackend
from ..inference.factory import build_backend
from ..inference.registry import ModelRegistry
from ..logging_setup import get_logger

logger = get_logger(__name__)

#: 后端工厂签名：``(cfg, model_id) -> ChatBackend``
BackendFactory = Callable[[Any, Optional[str]], ChatBackend]


def default_backend_factory(cfg: Any, model_id: Optional[str]) -> ChatBackend:
    """默认工厂：按配置构造后端；openai 后端支持用 ``model_id`` 覆盖模型名。"""
    if (cfg.inference.backend or "") == "openai_api" and model_id:
        cfg = replace(
            cfg,
            inference=replace(
                cfg.inference,
                openai=replace(cfg.inference.openai, model=model_id),
            ),
        )
        return build_backend(cfg)
    return build_backend(cfg, model_id=model_id)


class BackendPool:
    """单活跃后端的缓存与切换。"""

    def __init__(
        self,
        cfg: Any,
        *,
        registry: Optional[ModelRegistry] = None,
        factory: Optional[BackendFactory] = None,
    ) -> None:
        self.cfg = cfg
        self.registry = registry
        self._factory = factory or default_backend_factory
        self._backend: Optional[ChatBackend] = None
        #: 当前后端的标识 ``(backend_name, model_id)``，用于判断是否需要重建
        self._key: Optional[Tuple[str, str]] = None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def current(self) -> Optional[ChatBackend]:
        """当前活跃后端；尚未构造过则为 ``None``。"""
        return self._backend

    @property
    def current_key(self) -> Optional[Tuple[str, str]]:
        return self._key

    def _resolve_key(
        self, model_id: Optional[str], backend_name: Optional[str]
    ) -> Tuple[str, str]:
        return (
            backend_name or self.cfg.inference.backend or "",
            model_id or "",
        )

    # ------------------------------------------------------------------
    # 获取 / 切换
    # ------------------------------------------------------------------
    def get(
        self, *, model_id: Optional[str] = None, backend: Optional[str] = None
    ) -> ChatBackend:
        """取指定模型的后端；与当前一致则复用，否则重建。"""
        key = self._resolve_key(model_id, backend)
        if self._backend is not None and self._key == key:
            return self._backend
        return self.switch(model_id=model_id, backend=backend)

    def switch(
        self, *, model_id: Optional[str] = None, backend: Optional[str] = None
    ) -> ChatBackend:
        """切换模型：先卸载旧后端，再装载新的。"""
        key = self._resolve_key(model_id, backend)
        self.close()

        cfg = self.cfg
        if backend and backend != self.cfg.inference.backend:
            cfg = replace(cfg, inference=replace(cfg.inference, backend=backend))

        logger.info("切换推理后端：%s（模型 %s）", key[0], model_id or "默认")
        self._backend = self._factory(cfg, model_id)
        self._key = key
        return self._backend

    # ------------------------------------------------------------------
    # 释放与展示
    # ------------------------------------------------------------------
    def close(self) -> None:
        """释放当前后端（子进程 / HTTP 连接）。"""
        if self._backend is None:
            return
        try:
            self._backend.close()
        except Exception as exc:  # noqa: BLE001 - 释放失败不应中断主流程
            logger.warning("释放推理后端失败：%s", exc)
        finally:
            self._backend = None
            self._key = None

    def describe(self) -> Dict[str, Any]:
        """后端状态摘要，供管理后台展示（不含密钥明文）。"""
        return {
            "configured_backend": self.cfg.inference.backend,
            "configured_model": self.cfg.inference.model,
            "active_key": list(self._key) if self._key else None,
            "active": self._backend.describe() if self._backend else None,
        }

    def available_models(self) -> list:
        """可用模型清单（含本地是否已下载）。"""
        return self.registry_instance().describe()

    def registry_instance(self) -> ModelRegistry:
        """懒加载并缓存模型注册表（避免每轮对话都读盘）。"""
        if self.registry is None:
            self.registry = ModelRegistry.load(self.cfg)
        return self.registry

    def model_ids(self) -> List[str]:
        """本地可用模型 id 列表。"""
        return self.registry_instance().ids()