"""对话上下文组装与群聊回复策略。

本模块只做两件「纯函数式」的事，便于独立测试：

1. **该不该回**：:class:`GroupReplyPolicy` 决定群聊里是否响应某条消息
   （被 @ / 被叫到名字 / 指令前缀 / 随机概率），私聊则永远响应。
2. **怎么拼**：:func:`compose_messages` 把系统提示 + 历史 + 当前消息拼成
   后端可消费的 ``ChatMessage`` 列表。

关于群聊发言人区分
------------------

小模型（135M~1.7B）对 OpenAI 的 ``name`` 字段支持普遍不好，很多 GGUF 的
chat template 会直接忽略它。因此这里采用更稳妥的做法：**把发言人昵称写进
消息正文**（``昵称：内容``），并在系统提示里说明这条规则。所有后端都能理解。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence

from ..inference.base import ChatMessage

#: 昵称前缀与正文之间的分隔符
SPEAKER_SEPARATOR = "："

#: 去除 @提及 时，@ 与被 @ 内容之间允许的分隔符
_MENTION_TAIL = " \t\u3000，,、:："


@dataclass
class ReplyDecision:
    """一次「是否回复」的判定结果。"""

    should_reply: bool
    #: 判定原因：private / command / mentioned / called_name / random / ignored
    reason: str
    #: 清理后的文本（去掉 @机器人、指令前缀、行首称呼）
    text: str
    #: 是否为指令消息
    is_command: bool = False


# ----------------------------------------------------------------------
# 文本清理
# ----------------------------------------------------------------------
def clean_user_text(text: str, bot_names: Sequence[str] = ()) -> str:
    """去掉消息里的「@机器人」与行首称呼，避免污染记忆提取。

    只处理机器人的称呼，不动其他人的 @，因为那属于对话内容的一部分。

    边界处理：只有当称呼后紧跟分隔符 / 标点或位于句尾时才剥离，
    否则「小忆酱」「小忆同学」这类更长的名字会被误伤成「酱」「同学」。
    """
    result = text or ""
    tail_class = re.escape(_MENTION_TAIL)
    # 名字长的先替换，避免「小忆」先命中导致「小忆酱」残留
    for name in sorted({n for n in bot_names if n}, key=len, reverse=True):
        escaped = re.escape(name)
        # @称呼：后接分隔符 / 标点或结尾时才去掉
        result = re.sub(rf"@{escaped}(?=[{tail_class}]|$)", " ", result)
        # 行首称呼（可带标点 / 冒号）或整句就是称呼
        result = re.sub(rf"^\s*{escaped}(?:[{tail_class}]+|$)", "", result)
    return result.strip()


def detect_command(text: str, prefixes: Sequence[str]) -> tuple[bool, str]:
    """判断是否为指令消息；返回 ``(是否指令, 去掉前缀后的文本)``。"""
    stripped = (text or "").lstrip()
    for prefix in sorted({p for p in prefixes if p}, key=len, reverse=True):
        if stripped.startswith(prefix):
            return True, stripped[len(prefix) :].strip()
    return False, text


def contains_bot_name(text: str, bot_names: Sequence[str]) -> bool:
    """文本中是否直接叫到机器人名字。"""
    return any(name and name in (text or "") for name in bot_names)


# ----------------------------------------------------------------------
# 回复策略
# ----------------------------------------------------------------------
@dataclass
class GroupReplyPolicy:
    """群聊回复策略。"""

    #: 未 @ 时的随机回复概率
    probability: float = 0.0
    #: 被 @ / 叫到名字时是否必定回复
    reply_when_mentioned: bool = True
    #: 指令前缀
    command_prefixes: Sequence[str] = ("/",)

    @classmethod
    def from_config(cls, cfg: Any) -> "GroupReplyPolicy":
        dialogue = cfg.dialogue
        return cls(
            probability=float(dialogue.group_reply_probability),
            reply_when_mentioned=bool(dialogue.group_reply_when_mentioned),
            command_prefixes=tuple(dialogue.command_prefixes or ()),
        )

    def should_reply(
        self,
        text: str,
        *,
        session_kind: str = "group",
        is_mentioned: bool = False,
        bot_names: Sequence[str] = (),
        rng: Optional[random.Random] = None,
    ) -> ReplyDecision:
        """判定是否回复。

        :param is_mentioned: 渠道层已识别出的 @机器人（如 OneBot 的 at 段）
        :param bot_names: 机器人可能的昵称，用于纯文本里「叫名字」的识别
        :param rng: 注入的随机源，便于测试随机分支
        """
        # 指令优先：任何渠道（含私聊）都必须拦截，否则「/persona」会被当聊天发给模型
        is_command, command_text = detect_command(text, self.command_prefixes)
        if is_command:
            return ReplyDecision(True, "command", command_text, is_command=True)

        # 私聊永远回复
        if session_kind != "group":
            return ReplyDecision(True, "private", (text or "").strip())

        cleaned = clean_user_text(text, bot_names)
        if is_mentioned:
            if self.reply_when_mentioned:
                return ReplyDecision(True, "mentioned", cleaned)
        elif contains_bot_name(text, bot_names):
            if self.reply_when_mentioned:
                return ReplyDecision(True, "called_name", clean_user_text(text, bot_names))

        # 未被叫到：按概率决定是否随机接话
        source = rng or random
        if self.probability > 0 and source.random() < self.probability:
            return ReplyDecision(True, "random", cleaned)
        return ReplyDecision(False, "ignored", cleaned)


# ----------------------------------------------------------------------
# 上下文组装
# ----------------------------------------------------------------------
def _history_parts(record: Any) -> tuple[str, str, str]:
    """从历史记录对象里取出 ``(role, content, speaker_name)``。"""
    role = str(getattr(record, "role", "user"))
    content = str(getattr(record, "content", ""))
    meta = getattr(record, "meta", None) or {}
    speaker = ""
    if isinstance(meta, dict):
        speaker = str(meta.get("speaker_name") or "")
    if not speaker:
        speaker = str(getattr(record, "speaker_name", "") or "")
    return role, content, speaker


def compose_messages(
    system_prompt: str,
    history: Iterable[Any],
    user_text: str,
    *,
    user_name: str = "",
    group: bool = False,
) -> List[ChatMessage]:
    """拼装发送给模型的完整消息列表。

    :param history: 工作记忆（按时序由旧到新），元素需含 ``role``/``content``
        与可选的 ``meta['speaker_name']``
    :param user_name: 当前发言人昵称（群聊时用于加前缀）
    :param group: 是否为群聊上下文；为 ``True`` 且 ``user_name`` 非空时
        给用户消息加「昵称：」前缀
    """
    messages: List[ChatMessage] = []
    prompt = (system_prompt or "").strip()
    if prompt:
        messages.append(ChatMessage("system", prompt))

    for record in history:
        role, content, speaker = _history_parts(record)
        if not content:
            continue
        if role == "assistant":
            messages.append(ChatMessage("assistant", content))
        elif role == "system":
            messages.append(ChatMessage("system", content))
        else:
            body = f"{speaker}{SPEAKER_SEPARATOR}{content}" if (group and speaker) else content
            messages.append(ChatMessage("user", body))

    current = (
        f"{user_name}{SPEAKER_SEPARATOR}{user_text}" if (group and user_name) else user_text
    )
    if current:
        messages.append(ChatMessage("user", current))
    return messages


def group_instruction() -> str:
    """群聊场景追加给系统提示的规则，让模型明白上下文格式。"""
    return (
        "【群聊规则】\n"
        "当前是多人群聊，历史消息每行以「昵称：」开头表示不同的人说的话；"
        "请只回应最后一条消息，直接说话，不要自己添加「昵称：」前缀。"
    )