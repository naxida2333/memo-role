"""对话编排：人设 + 三层记忆 + 模型 → 一次回复。

- :class:`DialogueEngine` 负责主流程（回复判定、指令拦截、上下文组装、记忆落库）
- :class:`BackendPool` 负责推理后端缓存与运行时模型切换
- :class:`CommandHandler` 负责内置指令（切换人设 / 模型、清空上下文等）
- 上下文组装与群聊策略见 :mod:`memo_role.dialogue.context`
"""

from __future__ import annotations

from .backends import BackendFactory, BackendPool, default_backend_factory
from .commands import (
    COMMANDS,
    CommandContext,
    CommandHandler,
    CommandResult,
    CommandSpec,
)
from .context import (
    GroupReplyPolicy,
    ReplyDecision,
    clean_user_text,
    compose_messages,
    contains_bot_name,
    detect_command,
    group_instruction,
)
from .engine import DialogueEngine, TurnRequest, TurnResult

__all__ = [
    "BackendFactory",
    "BackendPool",
    "default_backend_factory",
    "COMMANDS",
    "CommandContext",
    "CommandHandler",
    "CommandResult",
    "CommandSpec",
    "GroupReplyPolicy",
    "ReplyDecision",
    "clean_user_text",
    "compose_messages",
    "contains_bot_name",
    "detect_command",
    "group_instruction",
    "DialogueEngine",
    "TurnRequest",
    "TurnResult",
]