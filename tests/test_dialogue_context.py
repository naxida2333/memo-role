"""对话上下文组装与群聊回复策略测试。"""

from __future__ import annotations

import random
from types import SimpleNamespace

from memo_role.dialogue.context import (
    GroupReplyPolicy,
    clean_user_text,
    compose_messages,
    contains_bot_name,
    detect_command,
    group_instruction,
)


# ----------------------------------------------------------------------
# 文本清理
# ----------------------------------------------------------------------
def test_clean_removes_at_mention() -> None:
    assert clean_user_text("@小忆 你好呀", ["小忆"]) == "你好呀"


def test_clean_removes_leading_name_with_punctuation() -> None:
    assert clean_user_text("小忆：你好", ["小忆"]) == "你好"
    assert clean_user_text("小忆，在吗", ["小忆"]) == "在吗"
    assert clean_user_text("小忆", ["小忆"]) == ""


def test_clean_keeps_longer_names_intact() -> None:
    """「小忆酱」不是对「小忆」的称呼，不能被截成「酱」。"""
    assert clean_user_text("小忆酱在吗", ["小忆"]) == "小忆酱在吗"


def test_clean_keeps_other_people_mentions() -> None:
    """别人的 @ 属于对话内容，应保留。"""
    assert clean_user_text("@小忆 你认识@小明吗", ["小忆"]) == "你认识@小明吗"


def test_clean_prefers_longer_bot_name() -> None:
    assert clean_user_text("@小忆酱 早", ["小忆", "小忆酱"]) == "早"


def test_clean_without_names_returns_stripped() -> None:
    assert clean_user_text("  你好  ") == "你好"


# ----------------------------------------------------------------------
# 指令识别
# ----------------------------------------------------------------------
def test_detect_command() -> None:
    assert detect_command("/reset", ["/"]) == (True, "reset")
    assert detect_command("  /help 参数", ["/"]) == (True, "help 参数")
    assert detect_command("你好", ["/"]) == (False, "你好")
    assert detect_command("/x", []) == (False, "/x")


def test_contains_bot_name() -> None:
    assert contains_bot_name("小忆在吗", ["小忆"]) is True
    assert contains_bot_name("在吗", ["小忆"]) is False


# ----------------------------------------------------------------------
# 回复策略
# ----------------------------------------------------------------------
def make_policy(**kwargs) -> GroupReplyPolicy:
    defaults = {"probability": 0.0, "reply_when_mentioned": True, "command_prefixes": ("/",)}
    defaults.update(kwargs)
    return GroupReplyPolicy(**defaults)


def test_private_always_replies() -> None:
    decision = make_policy().should_reply("随便说点什么", session_kind="private")
    assert decision.should_reply is True
    assert decision.reason == "private"


def test_private_command_is_detected() -> None:
    """私聊里的指令同样要被拦截，否则「/reset」会被当成聊天内容发给模型。"""
    decision = make_policy().should_reply("/reset", session_kind="private")
    assert decision.should_reply is True
    assert decision.is_command is True
    assert decision.reason == "command"
    assert decision.text == "reset"


def test_group_mentioned_replies() -> None:
    decision = make_policy().should_reply("早上好", is_mentioned=True)
    assert decision.should_reply is True
    assert decision.reason == "mentioned"


def test_group_called_name_replies() -> None:
    decision = make_policy().should_reply("小忆你觉得呢", bot_names=["小忆"])
    assert decision.should_reply is True
    assert decision.reason == "called_name"
    assert decision.text == "小忆你觉得呢"


def test_group_command_replies_and_strips_prefix() -> None:
    decision = make_policy(command_prefixes=("/",)).should_reply("/reset 全部")
    assert decision.should_reply is True
    assert decision.is_command is True
    assert decision.reason == "command"
    assert decision.text == "reset 全部"


def test_group_silent_when_probability_zero() -> None:
    decision = make_policy().should_reply("今天天气不错")
    assert decision.should_reply is False
    assert decision.reason == "ignored"


def test_group_random_reply_when_probability_hits() -> None:
    rng = random.Random()
    rng.random = lambda: 0.0  # 必定命中
    decision = make_policy(probability=0.5).should_reply("闲聊一句", rng=rng)
    assert decision.should_reply is True
    assert decision.reason == "random"


def test_group_random_reply_when_probability_misses() -> None:
    rng = random.Random()
    rng.random = lambda: 0.99  # 必定不命中
    decision = make_policy(probability=0.5).should_reply("闲聊一句", rng=rng)
    assert decision.should_reply is False


def test_mention_ignored_when_config_disables() -> None:
    """关闭「被 @ 必回」后，被 @ 也走随机分支（概率 0 → 不回）。"""
    decision = make_policy(reply_when_mentioned=False).should_reply(
        "在吗", is_mentioned=True
    )
    assert decision.should_reply is False


def test_policy_from_config(cfg) -> None:
    cfg.dialogue.group_reply_probability = 0.7
    cfg.dialogue.group_reply_when_mentioned = False
    cfg.dialogue.command_prefixes = ["!", "/"]
    policy = GroupReplyPolicy.from_config(cfg)
    assert policy.probability == 0.7
    assert policy.reply_when_mentioned is False
    assert set(policy.command_prefixes) == {"!", "/"}


# ----------------------------------------------------------------------
# 上下文组装
# ----------------------------------------------------------------------
def hist(role: str, content: str, speaker: str = "") -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, meta={"speaker_name": speaker})


def test_compose_private_no_prefix() -> None:
    messages = compose_messages("系统提示", [hist("user", "你好")], "在吗")
    assert [m.role for m in messages] == ["system", "user", "user"]
    assert messages[0].content == "系统提示"
    assert messages[-1].content == "在吗"


def test_compose_group_prefixes_speakers() -> None:
    history = [hist("user", "你好", "小明"), hist("assistant", "你好呀")]
    messages = compose_messages(
        "系统提示", history, "在吗", user_name="小红", group=True
    )
    assert messages[1].content == "小明：你好"  # 群聊发言标注发言人
    assert messages[2].content == "你好呀"  # 助手回复不加前缀
    assert messages[3].content == "小红：在吗"


def test_compose_group_without_name_no_prefix() -> None:
    messages = compose_messages("提示", [], "在吗", user_name="", group=True)
    assert messages[-1].content == "在吗"


def test_compose_skips_empty_system() -> None:
    messages = compose_messages("", [], "你好")
    assert [m.role for m in messages] == ["user"]


def test_compose_skips_empty_history_content() -> None:
    messages = compose_messages("提示", [hist("user", "")], "你好")
    assert len(messages) == 2


def test_compose_system_role_history() -> None:
    messages = compose_messages("提示", [hist("system", "内部说明")], "你好")
    assert messages[1].role == "system"


def test_group_instruction_mentions_label_format() -> None:
    assert "昵称" in group_instruction()