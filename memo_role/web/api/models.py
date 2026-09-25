"""模型管理 API：清单、全局默认模型切换、后端状态。

三种「用哪个模型」的层级（从低到高）：

1. 配置里的 ``inference.model`` —— 启动默认值
2. ``data/runtime.json`` 里的全局默认 —— 管理后台改的，重启仍生效
3. 会话绑定（``session.model_id``，由聊天里的 ``/model`` 指令写入）—— 只影响该会话

本模块负责第 2 层。切换是**立即生效**的：低配设备上模型是「卸载旧的再装载新的」，
若只改配置不重建后端，下一条消息仍会用旧模型，后台显示与实际不一致。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..state import AppState
from .deps import get_state

router = APIRouter(prefix="/models", tags=["models"])


class ModelDefault(BaseModel):
    """设置全局默认模型；``model_id`` 传空串表示恢复「跟随注册表默认」。"""

    model_id: str = ""


@router.get("")
def list_models(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """可用模型清单（含本地是否已下载）+ 当前后端状态。"""
    return {
        "backend": state.cfg.inference.backend,
        "default_model": state.cfg.inference.model,
        "openai_style": state.is_openai_backend(),
        "models": state.backends.available_models(),
        "active": state.backends.describe(),
    }


@router.post("/default")
def set_default_model(
    payload: ModelDefault, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """切换全局默认模型并立即重载后端。

    模型不存在或装载失败时回滚配置，避免留下「配置指向坏模型」的状态。
    """
    state.set_default_model(payload.model_id)
    return {
        "default_model": state.cfg.inference.model,
        "active": state.backends.describe(),
    }