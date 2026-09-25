"""OneBot v11 协议原语。

只做「报文 ↔ 对象」的转换，不碰网络与模型，因此可以纯函数式单测。

覆盖范围（够用就好，不做完整协议实现）：

- **接收**：``message`` 事件解析成 :class:`OneBotEvent`；``message`` 字段兼容
  段数组、CQ 码字符串、纯字符串三种形态
- **发送**：把文本渲染成消息段数组（而非 CQ 码字符串），从根上绕开 CQ 转义问题
- **调用**：``action`` 请求与响应的构造 / 解析，``echo`` 用于把响应关联回请求

关于 ``@机器人``：被 @ 的事实由 :attr:`OneBotEvent.mentioned_self` 表达，
``@机器人`` 段不会进入正文，否则会被当成聊天内容提取进记忆。
其他人被 @ 则渲染成 ``@QQ号``，保留对话语义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
#: 渠道标识，与 :class:`~memo_role.dialogue.engine.TurnRequest` 的 platform 对应
PLATFORM = "qq"

#: 会话 id 前缀，私聊按用户、群聊按群隔离工作记忆
PRIVATE_PREFIX = "private:"
GROUP_PREFIX = "group:"

#: 消息段类型
SEG_TEXT = "text"
SEG_AT = "at"
SEG_IMAGE = "image"
SEG_FACE = "face"
SEG_REPLY = "reply"

#: ``@全体成员`` 在 OneBot 中用 ``qq=all`` 表示
AT_ALL_QQ = "all"

#: 发送消息时用的动作名
ACTION_SEND_PRIVATE = "send_private_msg"
ACTION_SEND_GROUP = "send_group_msg"

#: CQ 码匹配（``[CQ:type,k=v,...]``）
_CQ_PATTERN = re.compile(r"\[CQ:([A-Za-z0-9_]+)((?:,[^\]]*)?)\]")

#: CQ 码里的转义（按顺序替换，``&amp;`` 必须最后处理）
_CQ_ESCAPES = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))


def _unescape_cq(text: str) -> str:
    """还原 CQ 码中的转义字符。"""
    for escaped, raw in _CQ_ESCAPES:
        text = text.replace(escaped, raw)
    return text


# ----------------------------------------------------------------------
# 消息段
# ----------------------------------------------------------------------
@dataclass
class MessageSegment:
    """一个消息段（OneBot 的 ``{type, data}``）。"""

    type: str
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> "MessageSegment":
        """从原始字典构造；类型缺失时视为纯文本段。"""
        if not isinstance(raw, dict):
            return cls(SEG_TEXT, {"text": str(raw)})
        seg_type = str(raw.get("type") or SEG_TEXT)
        data = raw.get("data")
        return cls(seg_type, dict(data) if isinstance(data, dict) else {})

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "data": dict(self.data)}

    @property
    def text(self) -> str:
        """文本段的内容；其它类型返回空串。"""
        return str(self.data.get("text", "")) if self.type == SEG_TEXT else ""

    @property
    def at_qq(self) -> str:
        """``at`` 段指向的 QQ 号；其它类型返回空串。"""
        return str(self.data.get("qq", "")) if self.type == SEG_AT else ""


def text_segment(text: str) -> MessageSegment:
    """构造一个文本段。"""
    return MessageSegment(SEG_TEXT, {"text": text})


def render_text_message(text: str) -> List[Dict[str, Any]]:
    """把纯文本渲染成 OneBot 发送用的消息段数组。"""
    return [text_segment(text).to_dict()]


def parse_segments(message: Any) -> List[MessageSegment]:
    """把 OneBot 的 ``message`` 字段规范成消息段列表。

    兼容三种形态：

    - 段数组：``[{"type": "text", "data": {"text": "hi"}}]``（新版推荐）
    - CQ 码：``"[CQ:at,qq=123] hi"``（老版 / 部分实现仍会发）
    - 纯字符串：``"hi"``
    """
    if message is None:
        return []
    if isinstance(message, str):
        return _parse_cq_string(message)
    if isinstance(message, dict):
        return [MessageSegment.from_dict(message)]
    if isinstance(message, (list, tuple)):
        return [MessageSegment.from_dict(m) for m in message]
    return [text_segment(str(message))]


def _parse_cq_string(text: str) -> List[MessageSegment]:
    """把 CQ 码字符串拆成消息段（无 CQ 码时退化为单个文本段）。"""
    segments: List[MessageSegment] = []
    cursor = 0
    for match in _CQ_PATTERN.finditer(text):
        if match.start() > cursor:
            plain = _unescape_cq(text[cursor : match.start()])
            if plain:
                segments.append(text_segment(plain))
        params: Dict[str, Any] = {}
        for pair in match.group(2).lstrip(",").split(","):
            if "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            params[key.strip()] = _unescape_cq(value)
        segments.append(MessageSegment(match.group(1), params))
        cursor = match.end()
    tail = _unescape_cq(text[cursor:])
    if tail:
        segments.append(text_segment(tail))
    return segments


# ----------------------------------------------------------------------
# 事件
# ----------------------------------------------------------------------
@dataclass
class OneBotEvent:
    """一个 OneBot v11 事件（本模块只关心消息事件，其余字段原样保留）。"""

    post_type: str = ""
    message_type: str = ""
    sub_type: str = ""
    message_id: int = 0
    #: 消息发送者 QQ（字符串，与记忆系统的 speaker_uid 对齐）
    user_id: str = ""
    #: 群号（私聊为空）
    group_id: str = ""
    #: 机器人自身 QQ
    self_id: str = ""
    message: List[MessageSegment] = field(default_factory=list)
    raw_message: str = ""
    sender: Dict[str, Any] = field(default_factory=dict)
    time: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, payload: Any) -> "OneBotEvent":
        """从原始报文构造事件（缺字段用默认值补齐，不抛异常）。"""
        if not isinstance(payload, dict):
            return cls(raw={})
        sender = payload.get("sender")
        return cls(
            post_type=str(payload.get("post_type") or ""),
            message_type=str(payload.get("message_type") or ""),
            sub_type=str(payload.get("sub_type") or ""),
            message_id=_as_int(payload.get("message_id")),
            user_id=_as_id(payload.get("user_id")),
            group_id=_as_id(payload.get("group_id")),
            self_id=_as_id(payload.get("self_id")),
            message=parse_segments(payload.get("message")),
            raw_message=str(payload.get("raw_message") or ""),
            sender=dict(sender) if isinstance(sender, dict) else {},
            time=_as_int(payload.get("time")),
            raw=dict(payload),
        )

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    @property
    def is_message(self) -> bool:
        """是否为收到的聊天消息（``message_sent`` 是机器人自己发的，不算）。"""
        return self.post_type == "message" and self.message_type in {"private", "group"}

    @property
    def is_private(self) -> bool:
        return self.message_type == "private"

    @property
    def is_group(self) -> bool:
        return self.message_type == "group"

    @property
    def session_kind(self) -> str:
        """传给对话引擎的会话类型：``private`` | ``group`` | ``web``。"""
        if self.is_private:
            return "private"
        if self.is_group:
            return "group"
        return "web"

    @property
    def session_id(self) -> str:
        """会话 id：私聊按用户、群聊按群隔离工作记忆；其它消息类型返回空串。

        只看 ``message_type`` 而不看 ``post_type``，因为 ``message_sent``
        （机器人自己发出去的消息）也属于同一个会话。
        """
        if self.is_private:
            return f"{PRIVATE_PREFIX}{self.user_id}"
        if self.is_group:
            return f"{GROUP_PREFIX}{self.group_id}"
        return ""

    @property
    def peer_id(self) -> str:
        """会话对象：私聊是对方 QQ，群聊是群号。"""
        return self.group_id if self.is_group else self.user_id

    @property
    def nickname(self) -> str:
        """发言人昵称：群聊优先用群名片，否则用 QQ 昵称。"""
        card = str(self.sender.get("card") or "").strip()
        nick = str(self.sender.get("nickname") or "").strip()
        if self.is_group and card:
            return card
        return nick or card

    # ------------------------------------------------------------------
    # 内容
    # ------------------------------------------------------------------
    @property
    def text(self) -> str:
        """可读正文：拼接文本段，并把非文本段转成占位符。

        - ``@机器人`` 段被丢弃（该事实由 :attr:`mentioned_self` 表达）
        - ``@全体成员`` → ``@全体成员``，其它 ``@`` → ``@QQ号``
        - 图片 / 表情 → ``[图片]`` / ``[表情]``，避免只剩空串而丢失消息
        """
        parts: List[str] = []
        for seg in self.message:
            if seg.type == SEG_TEXT:
                parts.append(seg.text)
            elif seg.type == SEG_AT:
                qq = seg.at_qq
                if qq == AT_ALL_QQ:
                    parts.append("@全体成员")
                elif not (self.self_id and qq == self.self_id):
                    parts.append(f"@{qq}")
            elif seg.type == SEG_IMAGE:
                parts.append("[图片]")
            elif seg.type == SEG_FACE:
                parts.append("[表情]")
        text = "".join(parts).strip()
        # 段数组缺失时（部分实现只给 raw_message）回退到原始文本
        return text or self.raw_message.strip()

    @property
    def mention_qs(self) -> List[str]:
        """本条消息 @ 到的 QQ 号列表。"""
        return [seg.at_qq for seg in self.message if seg.type == SEG_AT and seg.at_qq]

    @property
    def mentioned_self(self) -> bool:
        """是否 @ 了机器人。"""
        return bool(self.self_id) and self.self_id in self.mention_qs

    @property
    def at_all(self) -> bool:
        """是否 ``@全体成员``。"""
        return AT_ALL_QQ in self.mention_qs


def _as_int(value: Any) -> int:
    """宽松取整数（失败返回 0）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_id(value: Any) -> str:
    """把 QQ 号 / 群号统一成字符串；空值返回空串。"""
    if value is None or value == "":
        return ""
    return str(value).strip()


# ----------------------------------------------------------------------
# API 调用
# ----------------------------------------------------------------------
def build_api_request(
    action: str, params: Optional[Dict[str, Any]] = None, echo: str = ""
) -> Dict[str, Any]:
    """构造一条 OneBot API 请求报文。"""
    payload: Dict[str, Any] = {"action": action, "params": dict(params or {})}
    if echo:
        payload["echo"] = echo
    return payload


@dataclass
class ApiResponse:
    """OneBot API 响应。"""

    status: str = ""
    retcode: int = -1
    data: Dict[str, Any] = field(default_factory=dict)
    echo: str = ""
    message: str = ""

    @classmethod
    def from_dict(cls, payload: Any) -> "ApiResponse":
        if not isinstance(payload, dict):
            return cls()
        data = payload.get("data")
        return cls(
            status=str(payload.get("status") or ""),
            retcode=_as_int(payload.get("retcode")),
            data=dict(data) if isinstance(data, dict) else {},
            echo=str(payload.get("echo") or ""),
            message=str(payload.get("message") or payload.get("wording") or ""),
        )

    @property
    def ok(self) -> bool:
        """是否调用成功：``retcode`` 0 为同步成功、1 为异步已受理。"""
        return self.status == "ok" and self.retcode in (0, 1)