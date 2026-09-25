"""记忆管理 API：浏览 / 检索 / 编辑 / 删除长期记忆。

三层记忆在这里的可见范围：

- 核心记忆（``core``）与情节记忆（``episodic``）都在 ``memory_item`` 表里，
  可以浏览、改内容与重要度、删除；
- **工作记忆（消息）不在此模块**：它是会话的一部分，见
  :mod:`memo_role.web.api.dialogue` 的会话详情。

长期记忆是全局共享的：这里删掉一条，私聊群聊都会一起失去它。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..errors import BadRequestError, NotFoundError
from ..serialize import memory_dict, memory_list, speaker_dict
from ..state import AppState
from .deps import get_state

router = APIRouter(prefix="/memories", tags=["memories"])


class MemoryUpdate(BaseModel):
    """局部更新一条记忆。"""

    content: Optional[str] = None
    importance: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    kind: Optional[str] = None
    persona_id: Optional[str] = None


class MemorySearch(BaseModel):
    """向量检索请求。"""

    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=100)
    kinds: Optional[List[str]] = None
    persona_id: str = ""


# ----------------------------------------------------------------------
# 查询（``/search`` 与 ``/stats`` 需声明在 ``/{memory_id}`` 之前）
# ----------------------------------------------------------------------
@router.get("")
def list_memories(
    kind: Optional[str] = Query(None, description="core | episodic"),
    query: str = Query("", description="内容模糊匹配"),
    persona_id: str = Query("", description="按人设过滤（含未绑定人设的全局记忆）"),
    session_id: str = Query(""),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """按条件列出记忆（按重要度倒序，不做向量排序）。"""
    records = state.memory.store.list_memories(
        kinds=[kind] if kind else None,
        session_id=session_id or None,
        persona_id=persona_id or None,
        query_text=query or None,
        limit=limit,
        offset=offset,
    )
    return {
        "memories": memory_list(records),
        # 只按 kind 统计：带关键词 / 人设过滤的总数需要额外查询，代价不划算
        "total": state.memory.store.count_memories([kind] if kind else None),
    }


@router.post("/search")
def search_memories(
    payload: MemorySearch, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """向量召回：按语义相似度检索，返回结果带 ``score``。"""
    records = state.memory.recall(
        payload.query,
        top_k=payload.top_k,
        kinds=payload.kinds,
        persona_id=payload.persona_id or None,
    )
    return {"memories": memory_list(records), "query": payload.query}


@router.get("/stats")
def memory_stats(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """记忆系统统计（含向量后端 / 提取器信息，便于排查召回为空）。"""
    return state.memory.stats()


@router.get("/speakers")
def list_speakers(
    limit: int = Query(200, ge=1, le=1000), state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """发言人列表（含曾用昵称），用于按「谁说的」追溯记忆。"""
    records = state.memory.store.list_speakers(limit=limit)
    return {"speakers": [speaker_dict(s) for s in records]}


# ----------------------------------------------------------------------
# 编辑
# ----------------------------------------------------------------------
@router.put("/{memory_id}")
def update_memory(
    memory_id: int, payload: MemoryUpdate, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """局部更新记忆内容 / 重要度 / 人设归属。"""
    if state.memory.store.get_memory(memory_id) is None:
        raise NotFoundError(f"记忆 {memory_id} 不存在")

    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not fields:
        raise BadRequestError("没有需要更新的字段")
    _reject_unknown_kind(fields)

    state.memory.store.update_memory(memory_id, **fields)
    updated = state.memory.store.get_memory(memory_id)
    if updated is None:  # pragma: no cover - 刚查过存在，此处不会发生
        raise NotFoundError(f"记忆 {memory_id} 不存在")
    return memory_dict(updated)


@router.delete("/{memory_id}")
def delete_memory(
    memory_id: int, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """删除一条记忆（全局生效）。"""
    if not state.memory.store.delete_memory(memory_id):
        raise NotFoundError(f"记忆 {memory_id} 不存在")
    return {"memory_id": memory_id, "deleted": True}


def _reject_unknown_kind(fields: Dict[str, Any]) -> None:
    """``kind`` 只允许 core / episodic，写错会让记忆既不注入也召不回。"""
    kind = fields.get("kind")
    if kind is None:
        return
    allowed: Sequence[str] = ("core", "episodic")
    if kind not in allowed:
        raise BadRequestError(f"kind 只能是 {' / '.join(allowed)}，收到 {kind!r}")