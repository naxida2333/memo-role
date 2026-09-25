"""对话编排引擎测试。"""

from __future__ import annotations

import pytest

from memo_role.dialogue.backends import BackendPool
from memo_role.dialogue.engine import DialogueEngine, TurnRequest

from conftest import FakeBackend


@pytest.fixture
def engine(cfg, memory_manager, persona_manager, fake_backend):
    """群聊随机回复概率设为 0，保证判定结果确定可测。"""
    cfg.dialogue.group_reply_probability = 0.0
    pool = BackendPool(cfg, factory=lambda c, mid: fake_backend)
    return DialogueEngine.build(
        cfg, memory=memory_manager, persona=persona_manager, backends=pool
    )


def private(**kwargs) -> TurnRequest:
    defaults = {"session_id": "web:1", "user_text": "你好", "session_kind": "web"}
    defaults.update(kwargs)
    return TurnRequest(**defaults)


def group(**kwargs) -> TurnRequest:
    defaults = {
        "session_id": "group:1",
        "user_text": "在吗",
        "session_kind": "group",
        "platform": "qq",
        "speaker_uid": "10001",
        "speaker_name": "小红",
    }
    defaults.update(kwargs)
    return TurnRequest(**defaults)


# ----------------------------------------------------------------------
# 基本回复
# ----------------------------------------------------------------------
def test_private_reply_returns_text_and_metadata(engine, fake_backend) -> None:
    result = engine.reply(private(user_text="你好呀"))
    assert result.reply == fake_backend.reply
    assert result.persona_id == "default"
    assert result.skipped is False
    assert result.model_id == "fake-model"


def test_private_reply_persists_working_memory(engine) -> None:
    engine.reply(private(user_text="你好呀"))
    roles = [m.role for m in engine.memory.working_messages("web:1")]
    assert roles == ["user", "assistant"]


def test_messages_include_system_and_current_user(engine, fake_backend) -> None:
    engine.reply(private(user_text="你好呀"))
    sent = fake_backend.calls[-1]
    assert sent[0].role == "system"
    assert "小忆" in sent[0].content  # 默认人设名
    assert sent[-1].role == "user"
    assert sent[-1].content == "你好呀"


def test_current_message_not_duplicated_in_context(engine, fake_backend) -> None:
    engine.reply(private(user_text="第一句"))
    engine.reply(private(user_text="第二句"))
    pairs = [(m.role, m.content) for m in fake_backend.calls[-1]]
    assert pairs.count(("user", "第二句")) == 1  # 当前消息只出现一次
    assert ("user", "第一句") in pairs  # 历史保留


def test_should_reply_private_is_true(engine) -> None:
    assert engine.should_reply(private()).should_reply is True


# ----------------------------------------------------------------------
# 人设与规则
# ----------------------------------------------------------------------
def test_persona_switch(engine, fake_backend) -> None:
    result = engine.reply(private(persona_id="catgirl"))
    assert result.persona_id == "catgirl"
    assert "喵酱" in fake_backend.calls[-1][0].content


def test_extra_rules_appended(engine, fake_backend) -> None:
    engine.reply(private(extra_rules="不要提到天气"))
    assert "不要提到天气" in fake_backend.calls[-1][0].content


def test_group_reply_includes_group_rules_and_prefix(engine, fake_backend) -> None:
    engine.reply(
        group(user_text="@小忆 在吗", is_mentioned=True, bot_names=["小忆"])
    )
    sent = fake_backend.calls[-1]
    assert "群聊规则" in sent[0].content
    assert sent[-1].content == "小红：在吗"  # 当前消息带发言人称谓


# ----------------------------------------------------------------------
# 记忆：提取、注入、跨会话共享
# ----------------------------------------------------------------------
def test_new_memories_reported(engine) -> None:
    result = engine.reply(private(user_text="我叫小明"))
    assert [m.content for m in result.new_memories] == ["用户的称呼是「小明」"]


def test_core_memory_injected_on_later_turn(engine, fake_backend) -> None:
    engine.reply(private(user_text="我叫小明"))
    engine.reply(private(user_text="你还记得我叫什么吗"))
    assert "小明" in fake_backend.calls[-1][0].content


def test_group_memory_shared_with_private(engine, fake_backend) -> None:
    """群聊里说的话，私聊也必须能召回到（全局统一记忆）。"""
    engine.reply(
        group(
            user_text="@小忆 我喜欢下雨天",
            is_mentioned=True,
            bot_names=["小忆"],
            speaker_uid="20002",
            speaker_name="小明",
        )
    )
    engine.reply(
        TurnRequest(
            session_id="private:9", user_text="下雨天", session_kind="private", platform="qq"
        )
    )
    assert "下雨天" in fake_backend.calls[-1][0].content


def test_private_memory_shared_with_group(engine, fake_backend) -> None:
    engine.reply(private(session_id="private:1", user_text="我喜欢喝奶茶"))
    engine.reply(
        group(
            user_text="@小忆 奶茶好喝吗",
            is_mentioned=True,
            bot_names=["小忆"],
            speaker_uid="30003",
            speaker_name="小刚",
        )
    )
    assert "奶茶" in fake_backend.calls[-1][0].content


# ----------------------------------------------------------------------
# 群聊调度：不回也要记
# ----------------------------------------------------------------------
def test_group_unmentioned_is_skipped_but_remembered(engine, fake_backend) -> None:
    calls_before = len(fake_backend.calls)
    result = engine.reply(
        group(user_text="我叫小红", speaker_uid="7", speaker_name="小红")
    )
    assert result.skipped is True
    assert result.reply == ""
    assert result.decision.reason == "ignored"
    assert len(fake_backend.calls) == calls_before  # 没有调用模型，省算力
    # 但消息与记忆都要保留，否则群聊信息就丢了
    assert engine.memory.store.count_messages("group:1") == 1
    assert engine.memory.store.count_memories() == 1


def test_group_mention_replies_and_cleans_text(engine, fake_backend) -> None:
    result = engine.reply(
        group(user_text="@小忆 早上好", is_mentioned=True, bot_names=["小忆"])
    )
    assert result.skipped is False
    assert result.decision.reason == "mentioned"
    assert engine.memory.working_messages("group:1")[0].content == "早上好"


def test_group_command_always_replies(engine, fake_backend) -> None:
    result = engine.reply(group(user_text="/help"))
    assert result.skipped is False
    assert result.decision.reason == "command"
    assert result.decision.is_command is True


# ----------------------------------------------------------------------
# 流式
# ----------------------------------------------------------------------
def test_stream_reply_yields_chunks_and_records(engine, fake_backend) -> None:
    events = []
    chunks = list(
        engine.stream_reply(
            private(user_text="你好"), on_complete=events.append
        )
    )
    assert "".join(chunks) == fake_backend.reply
    assert events[0].reply == fake_backend.reply
    roles = [m.role for m in engine.memory.working_messages("web:1")]
    assert roles == ["user", "assistant"]


def test_stream_reply_skipped_invokes_on_complete(engine, fake_backend) -> None:
    events = []
    chunks = list(
        engine.stream_reply(group(user_text="随便聊聊"), on_complete=events.append)
    )
    assert chunks == []
    assert events[0].skipped is True
    assert fake_backend.calls == []  # 未触发推理


# ----------------------------------------------------------------------
# 模型切换
# ----------------------------------------------------------------------
def test_request_model_switch_rebuilds_backend(cfg, memory_manager, persona_manager) -> None:
    built: list[FakeBackend] = []

    def factory(_cfg, model_id):
        backend = FakeBackend(model=model_id or "default")
        built.append(backend)
        return backend

    engine = DialogueEngine.build(
        cfg,
        memory=memory_manager,
        persona=persona_manager,
        backends=BackendPool(cfg, factory=factory),
    )
    first = engine.reply(private(user_text="你好", model_id="qwen2.5-0.5b"))
    second = engine.reply(private(user_text="你好", model_id="smollm2-135m"))
    assert first.model_id == "qwen2.5-0.5b"
    assert second.model_id == "smollm2-135m"
    assert built[0].closed is True  # 切换时卸载旧模型


def test_same_model_reuses_backend(engine, fake_backend) -> None:
    engine.reply(private(user_text="你好"))
    engine.reply(private(user_text="再见"))
    assert len(fake_backend.calls) == 2  # 同一后端被复用，未重建


# ----------------------------------------------------------------------
# 维护
# ----------------------------------------------------------------------
def test_describe_reports_subsystems(engine) -> None:
    info = engine.describe()
    assert "persona" in info and "memory" in info and "backend" in info
    assert info["policy"]["group_reply_probability"] == 0.0


def test_close_releases_backend(engine, fake_backend) -> None:
    engine.reply(private(user_text="你好"))
    engine.close()
    assert fake_backend.closed is True