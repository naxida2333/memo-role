"""对话 API：会话管理与消息收发（含 SSE 流式）。

为什么流式要单独一条路由
------------------------

同步接口 :meth:`DialogueEngine.reply` 在低配设备上可能要等十几秒，浏览器里
「点了发送什么都不动」体验很差。流式版本逐字回传，用户至少能看到模型在动。

实现上有个必须绕开的坑：引擎的 ``stream_reply`` 是**同步生成器**（内部是阻塞
的推理调用），直接在事件循环里迭代会把整个 Web 服务卡住。因此这里把它放到
工作线程里跑，通过 ``call_soon_threadsafe`` 把片段塞回事件循环的队列 ——
WebSocket 事件、静态资源请求才不会跟着一起卡住。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import uuid
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...dialogue.commands import COMMANDS
from ...dialogue.engine import TurnRequest
from ...logging_setup import get_logger
from ...memory.store import SessionRecord
from ..errors import BadRequestError, NotFoundError
from ..serialize import message_dict, session_dict, turn_dict
from ..state import AppState
from .deps import get_state

logger = get_logger(__name__)

router = APIRouter(prefix="/dialogue", tags=["dialogue"])

#: Web 会话 id 允许的字符（会作为 URL 片段与数据库主键，需严格校验）
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

#: 会话详情默认返回的消息条数
DEFAULT_MESSAGE_LIMIT = 100

#: 会话标题长度上限（超出截断，避免侧栏被一行长文本撑开）
TITLE_MAX = 60


# ----------------------------------------------------------------------
# 请求模型
# ----------------------------------------------------------------------
class SessionCreate(BaseModel):
    """新建会话。"""

    session_id: Optional[str] = None
    title: str = ""
    persona_id: str = ""
    model_id: str = ""


class SessionUpdate(BaseModel):
    """更新会话元信息（目前只有标题）。"""

    title: str = ""


class SendRequest(BaseModel):
    """发送一条消息。"""

    text: str = Field(min_length=1, description="用户消息内容；以指令前缀开头则被拦截")
    speaker_name: str = "我"
    #: 仅本次请求生效；留空则跟随会话绑定
    persona_id: str = ""
    model_id: str = ""


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------
@router.get("/sessions")
def list_sessions(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """会话列表（按最近更新时间倒序），附带消息条数便于前端展示。"""
    records = state.memory.store.list_sessions(limit=limit, offset=offset)
    return {
        "sessions": [
            {**session_dict(r), "message_count": state.memory.count_messages(r.id)}
            for r in records
        ]
    }


@router.post("/sessions", status_code=201)
def create_session(
    payload: SessionCreate, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """新建会话；未指定 id 时自动生成 ``web:<随机>``。"""
    session_id = (payload.session_id or "").strip() or _new_session_id()
    _validate_session_id(session_id)

    state.memory.ensure_session(
        session_id,
        "web",
        platform="web",
        title=payload.title,
        persona_id=payload.persona_id,
        model_id=payload.model_id,
    )
    return {"session": session_dict(_require_session(state, session_id)), "messages": []}


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    message_limit: int = Query(DEFAULT_MESSAGE_LIMIT, ge=1, le=1000),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """会话详情 + 最近消息（按时间正序，便于前端直接铺成气泡）。"""
    session = _require_session(state, session_id)
    messages = state.memory.store.recent_messages(session_id, limit=message_limit)
    return {
        "session": session_dict(session),
        "messages": [message_dict(m) for m in messages],
        "total_messages": state.memory.count_messages(session_id),
    }


@router.patch("/sessions/{session_id}")
def update_session(
    session_id: str, payload: SessionUpdate, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """更新会话元信息（重命名）。

    只管标题：人设 / 模型请走 ``/persona`` ``/model`` 指令 —— 那边同时要处理
    「后端是否已装载」等副作用，这里再开一条写入路径会出现两个真相来源。
    """
    session = _require_session(state, session_id)
    state.memory.ensure_session(
        session_id,
        session.kind,
        platform=session.platform,
        peer_id=session.peer_id,
        title=_clean_title(payload.title),
        # 留空表示保留原绑定（upsert_session 只补非空字段）
    )
    return {"session": session_dict(_require_session(state, session_id))}


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """删除会话（连带其消息）。长期记忆是全局共享的，不受影响。"""
    _require_session(state, session_id)
    state.memory.store.delete_session(session_id)
    return {"session_id": session_id, "deleted": True}


@router.post("/sessions/{session_id}/reset")
def reset_session(
    session_id: str, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """清空当前会话的工作记忆（长期记忆保留）。"""
    _require_session(state, session_id)
    removed = state.memory.clear_working(session_id)
    return {"session_id": session_id, "removed": removed}


# ----------------------------------------------------------------------
# 消息
# ----------------------------------------------------------------------
@router.post("/sessions/{session_id}/messages")
def send_message(
    session_id: str, payload: SendRequest, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """发送消息并等待完整回复。"""
    _require_session(state, session_id)
    result = state.engine.reply(_turn_request(session_id, payload))
    return turn_dict(result)


@router.post("/sessions/{session_id}/stream")
def stream_message(
    session_id: str, payload: SendRequest, state: AppState = Depends(get_state)
) -> StreamingResponse:
    """发送消息并以 SSE 逐段回传。

    事件类型：``delta``（增量文本）/ ``done``（完整结果）/ ``error``（生成失败）。
    """
    _require_session(state, session_id)
    request = _turn_request(session_id, payload)
    return StreamingResponse(
        _sse_events(state, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反向代理（如 nginx）会缓冲响应，导致流式退化成「一次性」返回
            "X-Accel-Buffering": "no",
        },
    )


# ----------------------------------------------------------------------
# 指令清单
# ----------------------------------------------------------------------
@router.get("/commands")
def list_commands(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """内置指令清单（供聊天页展示快捷按钮，避免前端硬编码一份）。"""
    return {
        "prefix": state.engine.commands.prefix,
        "help": state.engine.commands.help_text(),
        "commands": [
            {
                "name": spec.name,
                "aliases": list(spec.aliases),
                "usage": spec.usage,
                "description": spec.description,
            }
            for spec in COMMANDS
        ],
    }


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _new_session_id() -> str:
    return f"web:{uuid.uuid4().hex[:12]}"


def _clean_title(text: Any) -> str:
    """单行化 + 截断标题；空标题直接报错。

    换行在会话列表里会把一项撑成两行，所以先压成单行再限制长度。
    """
    title = " ".join(str(text or "").split())
    if not title:
        raise BadRequestError("会话标题不能为空")
    return title[:TITLE_MAX]


def _validate_session_id(session_id: str) -> None:
    if not _SESSION_ID_PATTERN.match(session_id):
        raise BadRequestError(
            f"会话 id {session_id!r} 非法：只允许字母 / 数字 / . _ : -，最长 128 字符"
        )


def _require_session(state: AppState, session_id: str) -> SessionRecord:
    """取会话；不存在时报错（由异常处理器转 404）。"""
    session = state.memory.get_session(session_id)
    if session is None:
        raise NotFoundError(f"会话 {session_id!r} 不存在")
    return session


def _turn_request(session_id: str, payload: SendRequest) -> TurnRequest:
    return TurnRequest(
        session_id=session_id,
        user_text=payload.text,
        session_kind="web",
        platform="web",
        speaker_uid="web:local",
        speaker_name=payload.speaker_name or "我",
        persona_id=payload.persona_id,
        model_id=payload.model_id,
    )


def _sse(event: str, data: Dict[str, Any]) -> str:
    """拼一帧 SSE 报文。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _sse_events(state: AppState, request: TurnRequest) -> AsyncIterator[str]:
    """把同步生成器桥接成异步事件流。

    线程与事件循环之间只通过 ``asyncio.Queue`` 通信，且用
    ``call_soon_threadsafe`` 入队（``Queue.put_nowait`` 不是线程安全的）。
    """
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Tuple[str, Any]]" = asyncio.Queue()

    def emit(kind: str, payload: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

    def worker() -> None:
        try:
            for piece in state.engine.stream_reply(
                request,
                on_complete=lambda result: emit("done", turn_dict(result)),
            ):
                if piece:
                    emit("delta", {"text": piece})
        except Exception as exc:  # noqa: BLE001 - 推理失败要告知前端而不是静默断开
            logger.exception("流式生成失败：%s", exc)
            emit("error", {"detail": str(exc)})
        finally:
            emit("end", None)

    # 先发一帧注释，让浏览器立刻认为连接已建立（否则首字之前一直「转圈」）
    yield ": connected\n\n"

    threading.Thread(target=worker, name="memo-role-stream", daemon=True).start()

    finished = False
    while not finished:
        kind, payload = await queue.get()
        if kind == "end":
            finished = True
            yield _sse("end", {})
        else:
            yield _sse(kind, payload)