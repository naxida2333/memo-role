"""记忆存储层测试。"""

from __future__ import annotations

import numpy as np
import pytest

from memo_role.db import Database
from memo_role.memory.embedding import HashingEmbedding
from memo_role.memory.store import (
    KIND_CORE,
    KIND_EPISODIC,
    SCOPE_GLOBAL_ONLY,
    SCOPE_SESSION,
    MemoryStore,
)


@pytest.fixture
def store(db: Database) -> MemoryStore:
    return MemoryStore(db)


@pytest.fixture
def embedder() -> HashingEmbedding:
    return HashingEmbedding(dim=128)


def add_memory(store: MemoryStore, embedder: HashingEmbedding, content: str, **kwargs) -> int:
    """写入一条带向量的记忆。"""
    vector = embedder.embed_one(content)
    return store.add_memory(
        content,
        vector=vector,
        embedding_signature=embedder.signature,
        **kwargs,
    )


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------
def test_upsert_session_insert_then_update(store: MemoryStore) -> None:
    store.upsert_session("private:1", "private", platform="qq", peer_id="1")
    session = store.get_session("private:1")
    assert session is not None
    assert session.kind == "private"
    assert session.title == ""

    store.upsert_session("private:1", "private", platform="qq", title="和小明的私聊")
    session = store.get_session("private:1")
    assert session.title == "和小明的私聊"
    assert len(store.list_sessions()) == 1


def test_get_missing_session_returns_none(store: MemoryStore) -> None:
    assert store.get_session("nope") is None


def test_delete_session_removes_session_scoped_memory(
    store: MemoryStore, embedder: HashingEmbedding
) -> None:
    store.upsert_session("group:9", "group")
    add_memory(store, embedder, "仅属于本群的临时事", scope="session", session_id="group:9")
    add_memory(store, embedder, "跨会话的长期事实", scope="global", session_id="group:9")

    assert store.delete_session("group:9") is True
    # 会话级记忆被清理，全局记忆保留
    remaining = [m.content for m in store.list_memories()]
    assert remaining == ["跨会话的长期事实"]
    assert store.delete_session("group:9") is False


# ----------------------------------------------------------------------
# 发言人
# ----------------------------------------------------------------------
def test_upsert_speaker_is_idempotent(store: MemoryStore) -> None:
    first = store.upsert_speaker("qq", "10001", "小明")
    second = store.upsert_speaker("qq", "10001", "小明")
    assert first == second
    assert store.get_speaker(first).display_name == "小明"


def test_upsert_speaker_records_alias_on_rename(store: MemoryStore) -> None:
    """改昵称后旧名进入 aliases，避免「改了名就不认识」。"""
    speaker_id = store.upsert_speaker("qq", "10001", "小明")
    store.upsert_speaker("qq", "10001", "小明明")
    speaker = store.get_speaker(speaker_id)
    assert speaker.display_name == "小明明"
    assert "小明" in speaker.aliases


def test_upsert_speaker_without_name_keeps_existing(store: MemoryStore) -> None:
    speaker_id = store.upsert_speaker("qq", "1", "小明")
    store.upsert_speaker("qq", "1", "")
    assert store.get_speaker(speaker_id).display_name == "小明"


def test_find_speaker(store: MemoryStore) -> None:
    store.upsert_speaker("qq", "10001", "小明")
    assert store.find_speaker("qq", "10001").display_name == "小明"
    assert store.find_speaker("qq", "99999") is None


def test_list_speakers(store: MemoryStore) -> None:
    store.upsert_speaker("qq", "1", "A")
    store.upsert_speaker("qq", "2", "B")
    assert len(store.list_speakers()) == 2


# ----------------------------------------------------------------------
# 消息
# ----------------------------------------------------------------------
def test_recent_messages_order_and_limit(store: MemoryStore) -> None:
    store.upsert_session("web:1", "web")
    for i in range(5):
        store.add_message("web:1", "user", f"m{i}")

    records = store.recent_messages("web:1", limit=3)
    # 取最近 3 条但按时间正序返回，便于直接拼上下文
    assert [r.content for r in records] == ["m2", "m3", "m4"]


def test_recent_messages_exclude_last(store: MemoryStore) -> None:
    """排除末尾若干条（避免把刚写入的用户消息重复注入）。"""
    store.upsert_session("web:1", "web")
    for i in range(4):
        store.add_message("web:1", "user", f"m{i}")
    records = store.recent_messages("web:1", limit=10, exclude_last=1)
    assert [r.content for r in records] == ["m0", "m1", "m2"]


def test_add_message_stores_meta_and_speaker(store: MemoryStore) -> None:
    store.upsert_session("group:1", "group")
    speaker_id = store.upsert_speaker("qq", "9", "小明")
    store.add_message(
        "group:1", "user", "大家好", speaker_id=speaker_id, meta={"speaker_name": "小明"}
    )
    record = store.recent_messages("group:1", limit=1)[0]
    assert record.speaker_id == speaker_id
    assert record.meta["speaker_name"] == "小明"


def test_count_messages(store: MemoryStore) -> None:
    store.upsert_session("web:1", "web")
    store.upsert_session("web:2", "web")
    store.add_message("web:1", "user", "a")
    store.add_message("web:2", "user", "b")
    assert store.count_messages() == 2
    assert store.count_messages("web:1") == 1


def test_clear_messages_only_affects_target_session(store: MemoryStore) -> None:
    store.upsert_session("web:1", "web")
    store.upsert_session("web:2", "web")
    store.add_message("web:1", "user", "a")
    store.add_message("web:2", "user", "b")
    assert store.clear_messages("web:1") == 1
    assert store.count_messages("web:1") == 0
    assert store.count_messages("web:2") == 1  # 其它会话不受影响


def test_clear_messages_keeps_long_term_memory(
    store: MemoryStore, embedder: HashingEmbedding
) -> None:
    """清空上下文不应连长期记忆一起删掉。"""
    store.upsert_session("web:1", "web")
    store.add_message("web:1", "user", "a")
    add_memory(store, embedder, "用户喜欢猫")
    store.clear_messages("web:1")
    assert store.count_memories() == 1


# ----------------------------------------------------------------------
# 记忆条目
# ----------------------------------------------------------------------
def test_add_and_get_memory(store: MemoryStore, embedder: HashingEmbedding) -> None:
    speaker_id = store.upsert_speaker("qq", "1", "小明")
    memory_id = add_memory(
        store,
        embedder,
        "用户喜欢猫",
        kind=KIND_EPISODIC,
        speaker_id=speaker_id,
        importance=0.6,
    )
    record = store.get_memory(memory_id)
    assert record.content == "用户喜欢猫"
    assert record.speaker_name == "小明"  # 联表带出昵称
    assert record.score is None


def test_memory_without_vector_is_not_recallable(
    store: MemoryStore, embedder: HashingEmbedding
) -> None:
    store.add_memory("没有向量的记忆", kind=KIND_EPISODIC)
    results = store.search_memories(
        embedder.embed_one("没有向量的记忆"), embedder.signature
    )
    assert results == []


# ----------------------------------------------------------------------
# 召回
# ----------------------------------------------------------------------
def test_search_ranks_by_similarity(store: MemoryStore, embedder: HashingEmbedding) -> None:
    add_memory(store, embedder, "用户喜欢猫")
    add_memory(store, embedder, "用户喜欢猫粮")
    add_memory(store, embedder, "用户讨厌下雨")

    results = store.search_memories(
        embedder.embed_one("用户喜欢猫"), embedder.signature, top_k=3
    )
    assert results[0].content == "用户喜欢猫"
    assert results[0].score >= results[-1].score
    assert all(r.score is not None for r in results)


def test_search_respects_min_score(store: MemoryStore, embedder: HashingEmbedding) -> None:
    add_memory(store, embedder, "用户喜欢猫")
    add_memory(store, embedder, "完全不相关的内容在这里")
    strict = store.search_memories(
        embedder.embed_one("用户喜欢猫"), embedder.signature, min_score=0.9
    )
    assert [r.content for r in strict] == ["用户喜欢猫"]


def test_search_respects_top_k(store: MemoryStore, embedder: HashingEmbedding) -> None:
    for i in range(5):
        add_memory(store, embedder, f"用户喜欢猫{i}")
    results = store.search_memories(
        embedder.embed_one("用户喜欢猫"), embedder.signature, top_k=2
    )
    assert len(results) == 2


def test_search_empty_query_vector(store: MemoryStore, embedder: HashingEmbedding) -> None:
    add_memory(store, embedder, "用户喜欢猫")
    # 空文本向量为零向量，余弦相似度为 0
    results = store.search_memories(embedder.embed_one(""), embedder.signature)
    assert results == []


def test_global_recall_crosses_sessions(store: MemoryStore, embedder: HashingEmbedding) -> None:
    """核心需求：私聊写入的记忆，在群聊场景同样能被召回。"""
    add_memory(store, embedder, "用户在私聊里说喜欢猫", session_id="private:1", scope="global")

    results = store.search_memories(
        embedder.embed_one("喜欢猫"),
        embedder.signature,
        scope="all",
        session_id="group:99",  # 完全不同的会话
    )
    assert len(results) == 1


def test_scope_session_filters_to_one_session(
    store: MemoryStore, embedder: HashingEmbedding
) -> None:
    add_memory(store, embedder, "私聊的事", session_id="private:1")
    add_memory(store, embedder, "群聊的事", session_id="group:1")

    results = store.search_memories(
        embedder.embed_one("私聊的事"),
        embedder.signature,
        scope=SCOPE_SESSION,
        session_id="private:1",
    )
    assert [r.content for r in results] == ["私聊的事"]


def test_scope_global_only(store: MemoryStore, embedder: HashingEmbedding) -> None:
    add_memory(store, embedder, "会话内的临时信息", scope="session", session_id="web:1")
    add_memory(store, embedder, "全局的长期信息", scope="global", session_id="web:1")

    results = store.search_memories(
        embedder.embed_one("信息"), embedder.signature, scope=SCOPE_GLOBAL_ONLY
    )
    assert [r.content for r in results] == ["全局的长期信息"]


def test_search_filters_by_kind(store: MemoryStore, embedder: HashingEmbedding) -> None:
    add_memory(store, embedder, "用户叫小明", kind=KIND_CORE)
    add_memory(store, embedder, "用户喜欢小明", kind=KIND_EPISODIC)

    results = store.search_memories(
        embedder.embed_one("小明"), embedder.signature, kinds=[KIND_CORE]
    )
    assert [r.content for r in results] == ["用户叫小明"]


def test_search_filters_by_speaker(store: MemoryStore, embedder: HashingEmbedding) -> None:
    a = store.upsert_speaker("qq", "1", "A")
    b = store.upsert_speaker("qq", "2", "B")
    add_memory(store, embedder, "A 喜欢猫", speaker_id=a)
    add_memory(store, embedder, "B 喜欢猫", speaker_id=b)

    results = store.search_memories(
        embedder.embed_one("喜欢猫"), embedder.signature, speaker_id=b
    )
    assert [r.content for r in results] == ["B 喜欢猫"]


def test_search_filters_by_persona_including_unbound(
    store: MemoryStore, embedder: HashingEmbedding
) -> None:
    """人设维度：只召回该人设的，以及未绑定人设的记忆。"""
    add_memory(store, embedder, "属于猫娘的事", persona_id="catgirl")
    add_memory(store, embedder, "属于助手的事", persona_id="assistant")
    add_memory(store, embedder, "未绑定人设的通用事")

    results = store.search_memories(
        embedder.embed_one("事"), embedder.signature, persona_id="catgirl"
    )
    found = {r.content for r in results}
    assert "属于猫娘的事" in found
    assert "未绑定人设的通用事" in found
    assert "属于助手的事" not in found


def test_search_skips_vectors_from_other_signature(
    store: MemoryStore, embedder: HashingEmbedding
) -> None:
    """换了 embedding 后端后，旧向量不应参与比较。"""
    other = HashingEmbedding(dim=128)
    store.add_memory(
        "旧空间里的记忆",
        vector=other.embed_one("旧空间里的记忆"),
        embedding_signature="hashing:999",
    )
    results = store.search_memories(embedder.embed_one("记忆"), embedder.signature)
    assert results == []


def test_search_skips_dimension_mismatch(store: MemoryStore) -> None:
    """签名相同但维度不一致（异常数据）时应跳过而不是崩溃。"""
    store.add_memory(
        "维度不对的记忆",
        vector=np.ones(8, dtype=np.float32),
        embedding_signature="hashing:8",
    )
    query = np.ones(4, dtype=np.float32)
    assert store.search_memories(query, "hashing:8") == []


def test_search_on_empty_table(store: MemoryStore, embedder: HashingEmbedding) -> None:
    assert store.search_memories(embedder.embed_one("x"), embedder.signature) == []


def test_search_candidate_limit(store: MemoryStore, embedder: HashingEmbedding) -> None:
    for i in range(5):
        add_memory(store, embedder, f"记忆{i}")
    results = store.search_memories(
        embedder.embed_one("记忆"), embedder.signature, candidate_limit=2, top_k=5
    )
    assert len(results) <= 2


def test_touch_memories_updates_access_stats(
    store: MemoryStore, embedder: HashingEmbedding
) -> None:
    memory_id = add_memory(store, embedder, "用户喜欢猫")
    store.touch_memories([memory_id])
    record = store.get_memory(memory_id)
    assert record.access_count == 1
    assert record.last_access_at is not None

    store.touch_memories([])  # 空列表不应报错
    assert store.get_memory(memory_id).access_count == 1


# ----------------------------------------------------------------------
# 管理操作
# ----------------------------------------------------------------------
def test_list_memories_filters(store: MemoryStore, embedder: HashingEmbedding) -> None:
    add_memory(store, embedder, "用户叫小明", kind=KIND_CORE, importance=0.9, session_id="s1")
    add_memory(store, embedder, "用户喜欢猫", kind=KIND_EPISODIC, session_id="s1")
    add_memory(store, embedder, "别的会话的事", kind=KIND_EPISODIC, session_id="s2")

    assert len(store.list_memories()) == 3
    assert len(store.list_memories(kinds=[KIND_CORE])) == 1
    assert len(store.list_memories(session_id="s2")) == 1
    assert [m.content for m in store.list_memories(query_text="喜欢")] == ["用户喜欢猫"]


def test_list_memories_orders_by_importance(
    store: MemoryStore, embedder: HashingEmbedding
) -> None:
    add_memory(store, embedder, "低重要度", importance=0.1)
    add_memory(store, embedder, "高重要度", importance=0.9)
    assert store.list_memories()[0].content == "高重要度"


def test_list_memories_pagination(store: MemoryStore, embedder: HashingEmbedding) -> None:
    for i in range(5):
        add_memory(store, embedder, f"记忆{i}")
    assert len(store.list_memories(limit=2, offset=0)) == 2
    assert len(store.list_memories(limit=2, offset=4)) == 1


def test_count_memories(store: MemoryStore, embedder: HashingEmbedding) -> None:
    add_memory(store, embedder, "a", kind=KIND_CORE)
    add_memory(store, embedder, "b", kind=KIND_EPISODIC)
    assert store.count_memories() == 2
    assert store.count_memories([KIND_CORE]) == 1


def test_update_memory(store: MemoryStore, embedder: HashingEmbedding) -> None:
    memory_id = add_memory(store, embedder, "旧内容")
    assert store.update_memory(memory_id, content="新内容", importance=0.95) is True
    record = store.get_memory(memory_id)
    assert record.content == "新内容"
    assert record.importance == 0.95
    # 不允许更新的字段被忽略
    assert store.update_memory(memory_id, id=999) is False


def test_update_memory_meta_serialized(store: MemoryStore, embedder: HashingEmbedding) -> None:
    memory_id = add_memory(store, embedder, "内容")
    store.update_memory(memory_id, meta={"tag": "重要"})
    assert store.get_memory(memory_id).meta == {"tag": "重要"}


def test_delete_memory(store: MemoryStore, embedder: HashingEmbedding) -> None:
    memory_id = add_memory(store, embedder, "待删除")
    assert store.delete_memory(memory_id) is True
    assert store.get_memory(memory_id) is None
    assert store.delete_memory(memory_id) is False


def test_find_duplicate_memory(store: MemoryStore, embedder: HashingEmbedding) -> None:
    speaker_id = store.upsert_speaker("qq", "1", "小明")
    memory_id = add_memory(store, embedder, "用户喜欢猫", speaker_id=speaker_id)

    assert store.find_duplicate_memory("用户喜欢猫", speaker_id=speaker_id) == memory_id
    assert store.find_duplicate_memory("用户喜欢猫", speaker_id=999) is None
    assert store.find_duplicate_memory("用户喜欢狗") is None


def test_find_duplicate_memory_by_kind(store: MemoryStore, embedder: HashingEmbedding) -> None:
    add_memory(store, embedder, "同样的内容", kind=KIND_CORE)
    assert store.find_duplicate_memory("同样的内容", kind=KIND_CORE) is not None
    assert store.find_duplicate_memory("同样的内容", kind=KIND_EPISODIC) is None


def test_stats(store: MemoryStore, embedder: HashingEmbedding) -> None:
    store.upsert_session("web:1", "web")
    store.upsert_speaker("qq", "1", "小明")
    store.add_message("web:1", "user", "hi")
    add_memory(store, embedder, "核心", kind=KIND_CORE)
    add_memory(store, embedder, "情节", kind=KIND_EPISODIC)

    stats = store.stats()
    assert stats == {
        "sessions": 1,
        "messages": 1,
        "speakers": 1,
        "episodic": 1,
        "core": 1,
        "vectors": 2,
    }