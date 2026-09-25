"""人设管理 API：卡片增删改查 + 导入导出 + 全局默认人设。

注意「全局默认人设」与「会话绑定人设」的区别：

- 全局默认（本模块的 ``/default``）决定**新会话**一开始用哪个角色；
- 会话绑定由聊天里的 ``/persona`` 指令完成，只影响那个会话。

两者是递进关系：会话没绑定时跟随全局默认。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...persona.manager import PersonaNotFoundError
from ..errors import BadRequestError, ConflictError, NotFoundError
from ..state import AppState
from .deps import get_state

router = APIRouter(prefix="/personas", tags=["personas"])


class PersonaPayload(BaseModel):
    """人设卡字段（``PUT`` 时按「只改传入字段」处理）。"""

    id: Optional[str] = None
    name: str = ""
    avatar: str = "🙂"
    description: str = ""
    personality: str = ""
    speaking_style: str = ""
    scenario: str = ""
    greeting: str = ""
    example_dialogue: List[Any] = Field(default_factory=list)
    system_prompt: str = ""
    tags: List[str] = Field(default_factory=list)
    extra: Dict[str, Any] = Field(default_factory=dict)


class ImportPayload(BaseModel):
    """导入人设。``data`` 支持单卡 / 卡片数组 / 整体备份三种形态。"""

    data: Any
    overwrite: bool = False
    new_id: Optional[str] = None


# ----------------------------------------------------------------------
# 列表（必须声明在 /{persona_id} 之前，否则会被当成 id 吃掉）
# ----------------------------------------------------------------------
@router.get("")
def list_personas(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """全部人设摘要 + 当前全局默认。"""
    return {
        "personas": state.persona.list_summaries(),
        "default": state.persona.current_default() or "",
    }


@router.get("/export")
def export_personas(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """导出全部人设（整体备份格式，可直接喂给 ``/import``）。"""
    return {"version": 1, "personas": state.persona.export_all()}


@router.post("/import")
def import_personas(
    payload: ImportPayload, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """导入人设，返回实际写入的卡片列表。"""
    try:
        cards = state.persona.import_data(
            payload.data, overwrite=payload.overwrite, new_id=payload.new_id
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return {"personas": [c.to_dict() for c in cards], "count": len(cards)}


# ----------------------------------------------------------------------
# 增删改查
# ----------------------------------------------------------------------
@router.post("", status_code=201)
def create_persona(
    payload: PersonaPayload, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """新建人设卡；``id`` 留空则自动生成。"""
    fields = payload.model_dump(exclude={"id", "name"})
    try:
        card = state.persona.create(
            name=payload.name or "", persona_id=payload.id, **fields
        )
    except FileExistsError as exc:
        raise ConflictError(str(exc)) from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return card.to_dict()


@router.get("/{persona_id}")
def get_persona(persona_id: str, state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """单张人设卡完整内容（含提示词相关字段，供编辑页用）。"""
    return _require(state, persona_id).to_dict()


@router.put("/{persona_id}")
def update_persona(
    persona_id: str, payload: PersonaPayload, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """局部更新；``id`` 不可改（改 id 请用复制）。"""
    _require(state, persona_id)
    fields = payload.model_dump(exclude_unset=True, exclude={"id"})
    try:
        card = state.persona.update(persona_id, **fields)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return card.to_dict()


@router.delete("/{persona_id}")
def delete_persona(
    persona_id: str, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """删除人设；删掉默认人设时自动切到剩余第一张。"""
    _require(state, persona_id)
    return {"persona_id": persona_id, "deleted": state.persona.delete(persona_id)}


@router.post("/{persona_id}/default")
def set_default_persona(
    persona_id: str, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """把该人设设为全局默认（影响之后新建的会话）。"""
    try:
        card = state.persona.set_default(persona_id)
    except PersonaNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return {"default": card.id, "name": card.name}


@router.post("/{persona_id}/duplicate", status_code=201)
def duplicate_persona(
    persona_id: str, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """复制一张人设卡（新 id 为 ``<原 id>_copy``，重复时自动加序号）。"""
    try:
        card = state.persona.duplicate(persona_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return card.to_dict()


def _require(state: AppState, persona_id: str):
    card = state.persona.get_or_none(persona_id)
    if card is None:
        raise NotFoundError(f"人设 {persona_id!r} 不存在")
    return card