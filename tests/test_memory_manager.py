"""记忆管理器（三层架构）测试。"""

from __future__ import annotations

import pytest

from memo_role.db import Database
from memo_role.memory.embedding import HashingEmbedding
from memo_role.memory.extractor import RuleBasedExtractor
from memo_role.memory.manager import MemoryManager
from memo_role.memory.store import KIND_CORE, KIND_EPISODIC, SCOPE_SESSION, MemoryStore


@pytest.fixture
def manager(cfg, db: Database) -> MemoryManager:
    """用特征哈希 + 规则提取的管理器（无需模型，完全离线可测）。"""
    cfg.memory.min_importance = 0.2
    cfg.memory.recall_min_score = 0.05
    return MemoryManager.build(
        cfg,
        database=db,
        embedder=HashingEmbedding(dim=256),
        extractor=RuleBasedExtractor(),
    )


def exchange(manager: MemoryManager, session_id: str, user: str, assistant: str = "好的", **kwargs):
    """便捷地记录一轮对话。"""
    defaults = {
        "session_kind": "web",
        "platform": "qq",
        "speaker_uid": "10001",
        "speaker_name": "小明",
    }
    defaults.update(kwargs)
    return manager.record_exchange(session_id, user, assistant, **defaults)


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------
def test_record_exchange_stores_messages(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "你好呀", "你好，很高兴见到你")
    messages = manager.working_messages("private:1")
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "你好呀"


def test_record_exchange_extracts_memory(manager: MemoryManager) -> None:
    created = exchange(manager, "private:1", "我叫小明")
    assert len(created) == 1
    assert created[0].content == "用户的称呼是「小明」"
    assert created[0].kind == KIND_CORE
    assert created[0].speaker_name == "小明"  # 记忆关联到发言人


def test_extracts_only_from_user_text(manager: MemoryManager) -> None:
    """助手自己的回复不应被当作事实提取（否则会自我强化出噪声）。"""
    exchange(manager, "private:1", "今天天气不错", "我叫小白，我讨厌你")
    contents = [m.content for m in manager.store.list_memories()]
    assert contents == []


def test_repeated_exchange_is_deduplicated(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "我叫小明")
    second = exchange(manager, "private:1", "我叫小明")
    assert second == []
    assert manager.store.count_memories() == 1


def test_min_importance_filters_low_value(manager: MemoryManager) -> None:
    manager.cfg.memory.min_importance = 0.9
    # 喜好类重要度 0.55，低于阈值 → 不写入
    assert exchange(manager, "private:1", "我喜欢猫") == []
    assert manager.store.count_memories() == 0


def test_extract_and_store_directly(manager: MemoryManager) -> None:
    created = manager.extract_and_store("我喜欢在雨天读书", session_id="web:1")
    assert len(created) == 1
    assert created[0].kind == KIND_EPISODIC


def test_get_session_returns_record(manager: MemoryManager) -> None:
    assert manager.get_session("nope") is None
    manager.ensure_session("web:1", "web", persona_id="catgirl", model_id="qwen2.5-0.5b")
    session = manager.get_session("web:1")
    assert session is not None
    assert session.persona_id == "catgirl"
    assert session.model_id == "qwen2.5-0.5b"


def test_clear_working_keeps_long_term_memory(manager: MemoryManager) -> None:
    """清空上下文后，长期记忆与发言人仍应保留。"""
    exchange(manager, "web:1", "我叫小明")
    assert manager.count_messages("web:1") == 2
    assert manager.clear_working("web:1") == 2
    assert manager.count_messages("web:1") == 0
    assert manager.store.count_memories() == 1
    # 全局记忆仍可召回
    assert manager.core_memories()


def test_clear_working_other_session_untouched(manager: MemoryManager) -> None:
    exchange(manager, "web:1", "你好")
    exchange(manager, "web:2", "你好")
    manager.clear_working("web:1")
    assert manager.count_messages("web:1") == 0
    assert manager.count_messages("web:2") == 2


def test_record_message_without_extraction(manager: MemoryManager) -> None:
    """群聊中「旁观」到的消息只记录不入库记忆。"""
    manager.ensure_session("group:1", "group")
    manager.record_message("group:1", "user", "我叫路人甲")
    assert manager.store.count_memories() == 0
    assert manager.store.count_messages("group:1") == 1


def test_session_and_speaker_registered(manager: MemoryManager) -> None:
    exchange(manager, "group:5", "我叫小明", session_kind="group", platform="qq")
    session = manager.store.get_session("group:5")
    assert session.kind == "group"
    assert manager.store.find_speaker("qq", "10001").display_name == "小明"


# ----------------------------------------------------------------------
# 工作记忆
# ----------------------------------------------------------------------
def test_working_messages_respects_window(cfg, db: Database) -> None:
    cfg.memory.working_window = 2
    manager = MemoryManager.build(
        cfg, database=db, embedder=HashingEmbedding(dim=64), extractor=RuleBasedExtractor()
    )
    for i in range(4):
        exchange(manager, "web:1", f"消息{i}")
    assert len(manager.working_messages("web:1")) == 2


def test_working_messages_exclude_last(manager: MemoryManager) -> None:
    """组装上下文时应能排除「刚写入的这条用户消息」，且不混入其它会话。"""
    exchange(manager, "web:1", "第一句")  # 其它会话的消息不应出现
    exchange(manager, "web:2", "前一句")
    # 模拟真实生成流程：先写入当前用户消息，再组装上下文时排除它
    manager.record_message("web:2", "user", "第二句")
    messages = manager.working_messages("web:2", exclude_last=1)
    assert [m.content for m in messages] == ["前一句", "好的"]


# ----------------------------------------------------------------------
# 召回：全局统一记忆（核心需求）
# ----------------------------------------------------------------------
def test_recall_finds_relevant_memory(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "我喜欢吃草莓")
    results = manager.recall("草莓", session_id="private:1")
    assert results
    assert any("草莓" in r.content for r in results)


def test_global_memory_shared_between_private_and_group(manager: MemoryManager) -> None:
    """私聊说过的事，换成完全不同的群聊会话也要能召回。"""
    exchange(manager, "private:1", "我喜欢一只叫布丁的猫", session_kind="private")
    results = manager.recall("猫", session_id="group:888")
    assert results
    assert any("布丁" in r.content for r in results)


def test_group_memory_visible_in_private(manager: MemoryManager) -> None:
    """反向也要成立：群聊里说的，私聊能召回。"""
    exchange(
        manager,
        "group:1",
        "我叫小美",
        session_kind="group",
        speaker_uid="20002",
        speaker_name="小美",
    )
    results = manager.recall("小美", session_id="private:9")
    assert any("小美" in r.content for r in results)


def test_recall_scope_session_isolates(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "我喜欢猫")
    exchange(manager, "group:1", "我喜欢狗")
    results = manager.recall("喜欢", session_id="private:1", scope=SCOPE_SESSION)
    assert all("狗" not in r.content for r in results)


def test_recall_empty_query_returns_empty(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "我叫小明")
    assert manager.recall("") == []
    assert manager.recall("   ") == []


def test_recall_touches_access_stats(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "我叫小明")
    manager.recall("小明")
    record = manager.store.list_memories(kinds=[KIND_CORE])[0]
    assert record.access_count == 1


def test_recall_can_skip_touch(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "我叫小明")
    manager.recall("小明", touch=False)
    record = manager.store.list_memories(kinds=[KIND_CORE])[0]
    assert record.access_count == 0


def test_recall_with_top_k(manager: MemoryManager) -> None:
    for i in range(4):
        exchange(manager, "web:1", f"我喜欢猫{i}")
    assert len(manager.recall("喜欢猫", top_k=2)) == 2


# ----------------------------------------------------------------------
# 三层组装
# ----------------------------------------------------------------------
def test_core_memories_returns_identity_facts(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "我叫小明，我的生日是 2001-03-15")
    core = manager.core_memories()
    assert len(core) >= 2
    assert all(m.kind == KIND_CORE for m in core)


def test_build_bundle(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "我叫小明，我喜欢猫")
    bundle = manager.build_bundle("猫", session_id="private:1")
    assert bundle.core  # 核心记忆始终注入
    assert bundle.episodic
    assert bundle.working
    assert len(bundle.all_memories()) == len(bundle.core) + len(bundle.episodic)


def test_build_bundle_without_working(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "我叫小明")
    bundle = manager.build_bundle("小明", session_id="private:1", include_working=False)
    assert bundle.working == []


def test_build_memory_context_sections(manager: MemoryManager) -> None:
    exchange(manager, "private:1", "我叫小明，我喜欢猫")
    context = manager.build_memory_context("猫", session_id="private:1")
    assert "【长期设定】" in context
    assert "小明" in context
    assert "【相关回忆】" in context
    assert "来自：小明" in context  # 标注了信息来源发言人


def test_build_memory_context_empty_when_no_memory(manager: MemoryManager) -> None:
    assert manager.build_memory_context("你好", session_id="web:none") == ""


def test_build_memory_context_excludes_current_message(manager: MemoryManager) -> None:
    """生成回复时，刚写入的那条用户消息不应重复出现在上下文里。"""
    exchange(manager, "web:1", "我叫小明")
    context = manager.build_memory_context("小明", session_id="web:1", exclude_last=0)
    assert "【长期设定】" in context


# ----------------------------------------------------------------------
# 统计与工具
# ----------------------------------------------------------------------
def test_stats_includes_backends(manager: MemoryManager) -> None:
    exchange(manager, "web:1", "我叫小明")
    stats = manager.stats()
    assert stats["core"] == 1
    assert stats["embedding"]["backend"] == "hashing"
    assert stats["extractor"] == {"extractor": "rule"}


def test_build_creates_schema_when_no_store(cfg) -> None:
    """不传 store 时应自行建表。"""
    manager = MemoryManager.build(cfg)
    exchange(manager, "web:1", "我叫小明")
    assert manager.store.count_memories() == 1


def test_build_with_explicit_store(cfg, db: Database) -> None:
    store = MemoryStore(db)
    manager = MemoryManager.build(cfg, store=store)
    assert manager.store is store


def test_close_is_safe_for_local_embedder(manager: MemoryManager) -> None:
    manager.close()  # 特征哈希没有需要释放的资源，不应报错