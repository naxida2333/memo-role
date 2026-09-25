"""把内部记录对象转成可直接 JSON 序列化的字典。

放在独立模块的原因：同一份记录会被多个路由复用（例如会话摘要既出现在列表里，
也出现在发送消息的响应里），集中定义能避免字段名在各处漂移。

``meta`` / ``extra`` 这类 JSON 字段在库里是以字符串存的，这里统一解析成对象，
让前端不必自己 ``JSON.parse``。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..dialogue.engine import TurnResult
from ..memory.store import MemoryRecord, MessageRecord, SessionRecord, SpeakerRecord


def _loads(value: Any) -> Dict[str, Any]:
    """把库里的 JSON 字符串解析成字典；解析失败返回空字典。"""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def session_dict(record: SessionRecord) -> Dict[str, Any]:
    return {
        "id": record.id,
        "kind": record.kind,
        "platform": record.platform,
        "peer_id": record.peer_id,
        "title": record.title,
        "persona_id": record.persona_id,
        "model_id": record.model_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def message_dict(record: MessageRecord) -> Dict[str, Any]:
    return {
        "id": record.id,
        "session_id": record.session_id,
        "role": record.role,
        "content": record.content,
        "speaker_id": record.speaker_id,
        "speaker_name": _loads(record.meta).get("speaker_name", ""),
        "created_at": record.created_at,
    }


def speaker_dict(record: SpeakerRecord) -> Dict[str, Any]:
    return {
        "id": record.id,
        "platform": record.platform,
        "platform_uid": record.platform_uid,
        "display_name": record.display_name,
        "aliases": list(record.aliases),
    }


def memory_dict(record: MemoryRecord) -> Dict[str, Any]:
    return {
        "id": record.id,
        "kind": record.kind,
        "scope": record.scope,
        "content": record.content,
        "importance": record.importance,
        "session_id": record.session_id,
        "speaker_id": record.speaker_id,
        "speaker_name": record.speaker_name,
        "persona_id": record.persona_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "access_count": record.access_count,
        "last_access_at": record.last_access_at,
        "score": record.score,
        "meta": record.meta if isinstance(record.meta, dict) else _loads(record.meta),
    }


def memory_list(records: List[MemoryRecord]) -> List[Dict[str, Any]]:
    return [memory_dict(r) for r in records]


def turn_dict(result: TurnResult) -> Dict[str, Any]:
    """一次对话结果的对外结构。

    ``messages``（实际发给模型的消息）只在调试时需要，且体积可观，
    因此这里**不**下发；需要排查时看日志即可。
    """
    return {
        "reply": result.reply,
        "persona_id": result.persona_id,
        "model_id": result.model_id,
        "new_memories": memory_list(result.new_memories),
        "skipped": result.skipped,
        "is_command": result.is_command,
        "action": result.action,
        "command_data": result.command_data,
        "reason": result.decision.reason if result.decision else "",
    }


def error_body(message: str, *, code: Optional[str] = None) -> Dict[str, Any]:
    """统一错误体，便于前端按 ``code`` 分支处理。"""
    return {"detail": message, "code": code or "error"}