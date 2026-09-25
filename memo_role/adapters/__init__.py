"""外部渠道适配层。

当前只实现 NapCat（OneBot v11 反向 WebSocket）：

- :mod:`memo_role.adapters.onebot` 协议原语（事件解析 / 消息段 / API 报文），纯函数无 IO
- :mod:`memo_role.adapters.hub` 反向连接管理（登记连接、按 echo 关联 API 响应）
- :mod:`memo_role.adapters.napcat` 与 :class:`~memo_role.dialogue.engine.DialogueEngine` 对接

分层的原因：协议解析与连接管理不需要跑模型即可单测，渠道业务（会话 id、
@识别、回复发送）也能用假连接覆盖，避免测试必须启动 NapCat 与推理后端。
"""

from __future__ import annotations

from .hub import ConnectionHub, NotConnectedError, OneBotConnection, OneBotTransport
from .napcat import NapCatAdapter
from .onebot import (
    ApiResponse,
    MessageSegment,
    OneBotEvent,
    build_api_request,
    parse_segments,
    render_text_message,
    text_segment,
)

__all__ = [
    "ApiResponse",
    "ConnectionHub",
    "MessageSegment",
    "NapCatAdapter",
    "NotConnectedError",
    "OneBotConnection",
    "OneBotEvent",
    "OneBotTransport",
    "build_api_request",
    "parse_segments",
    "render_text_message",
    "text_segment",
]