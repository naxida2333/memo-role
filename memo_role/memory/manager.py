"""记忆管理器：三层记忆架构的统一入口。

三层划分
--------

============  ==================================================================
工作记忆      当前会话最近 N 条消息（``message`` 表）。直接拼进模型上下文，
             不做向量检索，保证「记得刚才说了什么」。
情节记忆      ``memory_item.kind = 'episodic'``。从对话中抽取的偏好、事件、
              明确要求记住的事。写入时算向量，召回时按相似度排序。
核心记忆      ``memory_item.kind = 'core'``。身份与长期设定（称呼、生日、
              职业、所在地）。**不依赖查询**，每轮都注入，保证角色不会「忘了你是谁」。
============  ==================================================================

全局统一记忆
------------

``memory_item`` 只有一张表，私聊与群聊的消息共用同一份记忆库。
:meth:`MemoryManager.recall` 默认 ``scope='all'``，即**不做会话过滤**，
所以群聊里发生的事可以在私聊中被召回，反之亦然 —— 这正是为了消除
「私聊有记忆、群聊无记忆」的割裂。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ..db import Database
from ..logging_setup import get_logger
from .embedding import EmbeddingProvider, build_embedding_provider
from .extractor import ExtractedMemory, MemoryExtractor, build_extractor
from .store import (
    KIND_CORE,
    KIND_EPISODIC,
    SCOPE_ALL,
    MemoryRecord,
    MemoryStore,
    MessageRecord,
    SessionRecord,
)

logger = get_logger(__name__)

#: 注入上下文时核心记忆的条数上限（防止上下文被长期设定撑满）
DEFAULT_CORE_LIMIT = 12


@dataclass
class RecallBundle:
    """一次召回的结果集合。"""

    core: List[MemoryRecord] = field(default_factory=list)
    episodic: List[MemoryRecord] = field(default_factory=list)
    working: List[MessageRecord] = field(default_factory=list)

    def all_memories(self) -> List[MemoryRecord]:
        return [*self.core, *self.episodic]


class MemoryManager:
    """三层记忆的统一协调者。"""

    def __init__(
        self,
        cfg: Any,
        store: MemoryStore,
        embedder: EmbeddingProvider,
        extractor: MemoryExtractor,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.extractor = extractor

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    @classmethod
    def build(
        cls,
        cfg: Any,
        *,
        database: Optional[Database] = None,
        store: Optional[MemoryStore] = None,
        embedder: Optional[EmbeddingProvider] = None,
        extractor: Optional[MemoryExtractor] = None,
        backend: Any = None,
        embedding_client: Any = None,
    ) -> "MemoryManager":
        """按配置组装管理器。

        :param backend: 推理后端；仅当 ``memory.extractor=llm`` 时需要
        :param embedding_client: 注入的 httpx 客户端（测试远程 embedding 用）
        """
        if store is None:
            db = database or Database(cfg.db_path)
            db.init_schema()
            store = MemoryStore(db)
        embedder = embedder or build_embedding_provider(cfg, client=embedding_client)
        extractor = extractor or build_extractor(cfg, backend=backend)
        return cls(cfg, store, embedder, extractor)

    # ------------------------------------------------------------------
    # 会话与发言人
    # ------------------------------------------------------------------
    def ensure_session(
        self,
        session_id: str,
        kind: str,
        *,
        platform: str = "local",
        peer_id: str = "",
        title: str = "",
        persona_id: str = "",
        model_id: str = "",
    ) -> None:
        """登记 / 更新会话元信息。"""
        self.store.upsert_session(
            session_id,
            kind,
            platform=platform,
            peer_id=peer_id,
            title=title,
            persona_id=persona_id,
            model_id=model_id,
        )

    def register_speaker(
        self, platform: str, platform_uid: str, display_name: str = ""
    ) -> int:
        """登记发言人，返回其内部 id。"""
        return self.store.upsert_speaker(platform, platform_uid, display_name)

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        """读取会话记录；不存在返回 ``None``。

        会话上记录了该会话绑定的人设 / 模型，供对话引擎解析「本会话用什么角色、什么模型」。
        """
        return self.store.get_session(session_id)

    def clear_working(self, session_id: str) -> int:
        """清空某会话的工作记忆（消息），返回删除条数；长期记忆不受影响。"""
        return self.store.clear_messages(session_id)

    def count_messages(self, session_id: Optional[str] = None) -> int:
        """消息条数（不传 session_id 时为全局）。"""
        return self.store.count_messages(session_id)

    # ------------------------------------------------------------------
    # 工作记忆
    # ------------------------------------------------------------------
    def working_messages(
        self, session_id: str, limit: Optional[int] = None, *, exclude_last: int = 0
    ) -> List[MessageRecord]:
        """取当前会话的最近消息（工作记忆）。"""
        return self.store.recent_messages(
            session_id, limit or self.cfg.memory.working_window, exclude_last=exclude_last
        )

    def record_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        speaker_id: Optional[int] = None,
        speaker_name: str = "",
    ) -> int:
        """只记录一条消息，不做记忆提取（群聊中「旁观」的消息用）。"""
        meta = {"speaker_name": speaker_name} if speaker_name else {}
        return self.store.add_message(
            session_id, role, content, speaker_id=speaker_id, meta=meta
        )

    # ------------------------------------------------------------------
    # 记录一轮对话 + 提取记忆
    # ------------------------------------------------------------------
    def record_exchange(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        *,
        session_kind: str = "web",
        platform: str = "local",
        peer_id: str = "",
        session_title: str = "",
        persona_id: str = "",
        model_id: str = "",
        speaker_uid: str = "",
        speaker_name: str = "",
    ) -> List[MemoryRecord]:
        """把一轮「用户说 → 助手答」写入记忆系统。

        只从**用户发言**中提取记忆：助手自己的回复属于模型的即兴发挥，
        把它当事实提取会自我强化，产生大量噪声。

        :return: 本轮新写入的记忆条目
        """
        self.ensure_session(
            session_id,
            session_kind,
            platform=platform,
            peer_id=peer_id,
            title=session_title,
            persona_id=persona_id,
            model_id=model_id,
        )

        speaker_id: Optional[int] = None
        if speaker_uid:
            speaker_id = self.register_speaker(platform, speaker_uid, speaker_name)

        self.record_message(
            session_id,
            "user",
            user_text,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
        )
        self.record_message(session_id, "assistant", assistant_text)

        return self.extract_and_store(
            user_text,
            session_id=session_id,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            persona_id=persona_id or None,
        )

    def extract_and_store(
        self,
        text: str,
        *,
        session_id: Optional[str] = None,
        speaker_id: Optional[int] = None,
        speaker_name: str = "",
        persona_id: Optional[str] = None,
    ) -> List[MemoryRecord]:
        """从文本提取关键信息并写入记忆（含向量化与去重）。"""
        candidates = self.extractor.extract(text, speaker_name=speaker_name)
        if not candidates:
            return []

        min_importance = float(self.cfg.memory.min_importance)
        kept = [c for c in candidates if c.importance >= min_importance]
        if not kept:
            return []

        # 批量向量化，减少远程 embedding 的往返次数
        vectors = self.embedder.embed([c.content for c in kept])

        created: List[MemoryRecord] = []
        for item, vector in zip(kept, vectors):
            stored = self._store_one(
                item,
                vector,
                session_id=session_id,
                speaker_id=speaker_id,
                persona_id=persona_id,
            )
            if stored is not None:
                created.append(stored)
        return created

    def _store_one(
        self,
        item: ExtractedMemory,
        vector: np.ndarray,
        *,
        session_id: Optional[str],
        speaker_id: Optional[int],
        persona_id: Optional[str],
    ) -> Optional[MemoryRecord]:
        """写入单条记忆，命中重复则跳过。"""
        duplicate = self.store.find_duplicate_memory(
            item.content, speaker_id=speaker_id, kind=item.kind
        )
        if duplicate is not None:
            logger.debug("跳过重复记忆：%s", item.content)
            return None

        # 身份类信息跨会话通用，标记为 global；偏好与事件也默认 global，
        # 这样私聊聊到的喜好在群聊里同样能被用到（全局统一记忆）
        memory_id = self.store.add_memory(
            item.content,
            kind=item.kind,
            scope="global",
            session_id=session_id,
            speaker_id=speaker_id,
            persona_id=persona_id,
            importance=item.importance,
            meta=item.meta,
            vector=vector,
            embedding_signature=self.embedder.signature,
        )
        return self.store.get_memory(memory_id)

    # ------------------------------------------------------------------
    # 召回
    # ------------------------------------------------------------------
    def recall(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        session_id: Optional[str] = None,
        scope: str = SCOPE_ALL,
        kinds: Optional[Sequence[str]] = None,
        speaker_id: Optional[int] = None,
        persona_id: Optional[str] = None,
        touch: bool = True,
    ) -> List[MemoryRecord]:
        """按查询文本做向量召回。

        默认 ``scope='all'``：跨会话全局召回（私聊 / 群聊共用一套记忆）。
        """
        if not query or not query.strip():
            return []

        vector = self.embedder.embed_one(query)
        if vector.shape[0] == 0:
            return []

        results = self.store.search_memories(
            vector,
            self.embedder.signature,
            kinds=kinds,
            scope=scope,
            session_id=session_id,
            speaker_id=speaker_id,
            persona_id=persona_id,
            top_k=top_k or self.cfg.memory.recall_top_k,
            min_score=(
                self.cfg.memory.recall_min_score if min_score is None else min_score
            ),
        )
        if touch and results:
            self.store.touch_memories([r.id for r in results])
        return results

    def core_memories(
        self, *, persona_id: Optional[str] = None, limit: int = DEFAULT_CORE_LIMIT
    ) -> List[MemoryRecord]:
        """核心记忆：按重要度取，不受查询影响。"""
        return self.store.list_memories(
            kinds=[KIND_CORE], persona_id=persona_id, limit=limit
        )

    def build_bundle(
        self,
        query: str,
        *,
        session_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        include_working: bool = True,
        exclude_last: int = 0,
    ) -> RecallBundle:
        """一次性组装三层记忆，供对话引擎直接使用。"""
        return RecallBundle(
            core=self.core_memories(persona_id=persona_id),
            episodic=self.recall(
                query, session_id=session_id, persona_id=persona_id
            ),
            working=(
                self.working_messages(session_id, exclude_last=exclude_last)
                if include_working and session_id
                else []
            ),
        )

    def build_memory_context(
        self,
        query: str,
        *,
        session_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        exclude_last: int = 0,
    ) -> str:
        """把记忆渲染成可注入系统提示的文本块。

        返回空串表示没有可用记忆（调用方据此决定是否拼接）。
        """
        bundle = self.build_bundle(
            query,
            session_id=session_id,
            persona_id=persona_id,
            include_working=False,
            exclude_last=exclude_last,
        )

        sections: List[str] = []
        if bundle.core:
            sections.append(
                "【长期设定】\n"
                + "\n".join(f"- {_format_memory(m)}" for m in bundle.core)
            )
        if bundle.episodic:
            sections.append(
                "【相关回忆】\n"
                + "\n".join(f"- {_format_memory(m)}" for m in bundle.episodic)
            )
        if not sections:
            return ""
        return "\n".join(sections)

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """统计信息（附带当前向量后端，便于排查召回为空的原因）。"""
        data = self.store.stats()
        data["embedding"] = self.embedder.describe()
        data["extractor"] = self.extractor.describe()
        return data

    def close(self) -> None:
        """释放资源。"""
        closer = getattr(self.embedder, "close", None)
        if callable(closer):
            closer()


def _format_memory(record: MemoryRecord) -> str:
    """渲染单条记忆；有发言人时标注「谁说的」。"""
    if record.speaker_name:
        return f"{record.content}（来自：{record.speaker_name}）"
    return record.content