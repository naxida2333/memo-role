"""系统管理 API：状态总览、配置查看、日志、对话策略热更。

「热更」只开放少数几个字段（见 ``state.DIALOGUE_OVERRIDABLE``）：群聊回复概率、
是否被叫到必回、是否给群聊发言加昵称前缀。这几项调参频繁且改动安全 ——
其余配置（后端、模型路径、指令前缀）改了必须重启，热更反而会造成前后不一致。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ... import __version__
from ..errors import BadRequestError
from ..state import AppState
from .deps import get_state

router = APIRouter(prefix="/system", tags=["system"])

#: API 密钥在配置展示里的掩码（后台不应回显明文）
_MASK = "********"

#: 日志接口单次最多返回的行数
MAX_LOG_LINES = 2000


class DialogueSettings(BaseModel):
    """可热更的对话策略字段。"""

    group_reply_probability: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    group_reply_when_mentioned: Optional[bool] = None
    label_group_speakers: Optional[bool] = None


@router.get("/status")
def status(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """服务总体状态：引擎 / 记忆 / 后端 / 人设 / NapCat / 文件沙箱。"""
    return {
        "version": __version__,
        "server_time": time.time(),
        **state.describe(),
    }


@router.get("/config")
def get_config(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """当前生效配置（API 密钥已掩码）。"""
    return _sanitize(state.cfg.to_dict())


@router.put("/dialogue")
def update_dialogue(
    payload: DialogueSettings, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """热更对话策略并持久化到 ``data/runtime.json``。"""
    values = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not values:
        raise BadRequestError("没有需要更新的字段")
    state.update_dialogue(values)
    return {"dialogue": state.runtime.describe()["dialogue"]}


@router.get("/logs")
def tail_logs(
    lines: int = Query(200, ge=1, le=MAX_LOG_LINES),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """读取日志文件末尾若干行（未配置文件日志时返回空列表）。"""
    path: Optional[Path] = state.cfg.log_file
    if path is None or not path.exists():
        return {"path": str(path) if path else "", "lines": [], "exists": False}
    return {
        "path": str(path),
        "lines": _tail(path, lines),
        "exists": True,
        "size": path.stat().st_size,
    }


def _sanitize(data: Dict[str, Any]) -> Dict[str, Any]:
    """掩码敏感字段，避免管理后台把密钥回显到浏览器 / 日志。"""
    result = dict(data)
    inference = result.get("inference")
    if isinstance(inference, dict):
        openai = inference.get("openai")
        if isinstance(openai, dict) and openai.get("api_keys"):
            openai = dict(openai)
            keys = openai["api_keys"]
            openai["api_keys"] = [_MASK for _ in keys]
            openai["api_key_count"] = len(keys)
            inference = dict(inference)
            inference["openai"] = openai
            result["inference"] = inference
    napcat = result.get("napcat")
    if isinstance(napcat, dict) and napcat.get("access_token"):
        napcat = dict(napcat)
        napcat["access_token"] = _MASK
        result["napcat"] = napcat
    return result


def _tail(path: Path, lines: int) -> List[str]:
    """读文件末尾 ``lines`` 行。

    日志文件可能很大（滚动上限 1MB × 份数），整读进内存不合适；这里从
    文件尾按块回退读取，读到足够的换行或到达文件头为止。
    """
    block = 8192
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        buffer = b""
        while size > 0 and buffer.count(b"\n") <= lines:
            step = min(block, size)
            size -= step
            handle.seek(size)
            buffer = handle.read(step) + buffer
    text = buffer.decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]