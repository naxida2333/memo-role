"""内置指令测试。"""

from __future__ import annotations

import pytest

from memo_role.dialogue.backends import BackendPool
from memo_role.dialogue.commands import COMMANDS, CommandContext, CommandHandler

from conftest import FakeBackend


@pytest.fixture
def handler(cfg, memory_manager, persona_manager, fake_backend) -> CommandHandler:
    pool = BackendPool(cfg, factory=lambda c, mid: fake_backend)
    return CommandHandler(cfg, memory_manager, persona_manager, pool)


def ctx(session_id: str = "web:1", persona_id: str = "default", kind: str = "web") -> CommandContext:
    return CommandContext(session_id=session_id, session_kind=kind, persona_id=persona_id)


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------
def test_unknown_command_returns_none(handler: CommandHandler) -> None:
    """未识别指令交回给模型，避免「/ 开头其实是聊天」被中断。"""
    assert handler.handle("不存在的指令", ctx()) is None
    assert handler.handle("foobar 参数", ctx()) is None


def test_empty_text_returns_none(handler: CommandHandler) -> None:
    assert handler.handle("", ctx()) is None
    assert handler.handle("   ", ctx()) is None


def test_help_lists_all_commands(handler: CommandHandler) -> None:
    result = handler.handle("help", ctx())
    assert result is not None and result.action == "help"
    for spec in COMMANDS:
        assert spec.name in result.reply


def test_help_aliases(handler: CommandHandler) -> None:
    for alias in ("帮助", "?", "？"):
        result = handler.handle(alias, ctx())
        assert result is not None and result.action == "help"


def test_help_uses_configured_prefix(cfg, handler: CommandHandler) -> None:
    cfg.dialogue.command_prefixes = ["!"]
    handler.prefix = "!"
    result = handler.handle("help", ctx())
    assert "!persona" in result.reply


# ----------------------------------------------------------------------
# /persona
# ----------------------------------------------------------------------
def test_persona_list(handler: CommandHandler) -> None:
    result = handler.handle("persona", ctx())
    assert result is not None and result.action == "persona_list"
    assert result.data["current"] == "default"
    assert "default" in result.data["personas"]
    assert "小忆" in result.reply


def test_persona_list_via_alias(handler: CommandHandler) -> None:
    result = handler.handle("角色 list", ctx())
    assert result is not None and result.action == "persona_list"


def test_persona_switch_binds_to_session(handler: CommandHandler, memory_manager) -> None:
    result = handler.handle("persona catgirl", ctx(session_id="group:1"))
    assert result is not None and result.action == "persona_switch"
    assert result.data["persona_id"] == "catgirl"
    # 绑定写进会话，而不是全局默认
    session = memory_manager.get_session("group:1")
    assert session is not None and session.persona_id == "catgirl"


def test_persona_not_found(handler: CommandHandler, memory_manager) -> None:
    result = handler.handle("persona 不存在", ctx())
    assert result is not None and result.action == "persona_not_found"
    assert memory_manager.get_session("web:1") is None  # 未绑定


def test_persona_switch_is_per_session(handler: CommandHandler, memory_manager) -> None:
    handler.handle("persona catgirl", ctx(session_id="group:1"))
    handler.handle("persona default", ctx(session_id="private:2"))
    assert memory_manager.get_session("group:1").persona_id == "catgirl"
    assert memory_manager.get_session("private:2").persona_id == "default"


# ----------------------------------------------------------------------
# /model
# ----------------------------------------------------------------------
def test_model_list(handler: CommandHandler) -> None:
    result = handler.handle("model", ctx())
    assert result is not None and result.action == "model_list"
    assert "smollm2-135m" in result.data["models"]
    assert "smollm2-135m" in result.reply


def test_model_switch_binds_to_session(handler: CommandHandler, memory_manager) -> None:
    result = handler.handle("model qwen2.5-0.5b", ctx())
    assert result is not None and result.action == "model_switch"
    session = memory_manager.get_session("web:1")
    assert session is not None and session.model_id == "qwen2.5-0.5b"


def test_model_not_found(handler: CommandHandler) -> None:
    result = handler.handle("model 不存在的模型", ctx())
    assert result is not None and result.action == "model_not_found"


def test_openai_backend_accepts_arbitrary_model(
    cfg, memory_manager, persona_manager, fake_backend
) -> None:
    cfg.inference.backend = "openai_api"
    handler = CommandHandler(
        cfg, memory_manager, persona_manager, BackendPool(cfg, factory=lambda c, m: fake_backend)
    )
    result = handler.handle("model gpt-4o-mini", ctx())
    assert result is not None and result.action == "model_switch"
    assert memory_manager.get_session("web:1").model_id == "gpt-4o-mini"


# ----------------------------------------------------------------------
# /reset
# ----------------------------------------------------------------------
def test_reset_clears_working_memory(handler: CommandHandler, memory_manager) -> None:
    memory_manager.ensure_session("web:1", "web")
    memory_manager.record_message("web:1", "user", "第一句")
    memory_manager.record_message("web:1", "assistant", "第二句")
    result = handler.handle("reset", ctx())
    assert result is not None and result.action == "reset"
    assert result.data["removed"] == 2
    assert memory_manager.count_messages("web:1") == 0


def test_reset_keeps_long_term_memory(handler: CommandHandler, memory_manager) -> None:
    memory_manager.extract_and_store("我叫小明", session_id="web:1")
    handler.handle("reset", ctx())
    assert memory_manager.store.count_memories() == 1


# ----------------------------------------------------------------------
# /memory
# ----------------------------------------------------------------------
def test_memory_empty(handler: CommandHandler) -> None:
    result = handler.handle("memory", ctx())
    assert result is not None and result.action == "memory"
    assert "还没有长期记忆" in result.reply


def test_memory_lists_core(handler: CommandHandler, memory_manager) -> None:
    memory_manager.extract_and_store("我叫小明", session_id="web:1")
    result = handler.handle("memory", ctx())
    assert "小明" in result.reply


def test_memory_recall_by_keyword(handler: CommandHandler, memory_manager) -> None:
    memory_manager.extract_and_store("我喜欢下雨天", session_id="web:1")
    result = handler.handle("memory 下雨天", ctx())
    assert result is not None
    assert "下雨天" in result.reply
    assert result.data["count"] >= 1


def test_memory_recall_no_match(handler: CommandHandler) -> None:
    result = handler.handle("memory 完全不存在的词", ctx())
    assert "没有找到" in result.reply


# ----------------------------------------------------------------------
# /status
# ----------------------------------------------------------------------
def test_status(handler: CommandHandler, memory_manager) -> None:
    memory_manager.ensure_session("web:1", "web")
    memory_manager.record_message("web:1", "user", "你好")
    result = handler.handle("status", ctx())
    assert result is not None and result.action == "status"
    assert "运行状态" in result.reply
    assert "会话消息数：1" in result.reply