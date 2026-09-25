"""记忆持久化层。

只负责「读写 SQLite」，不关心向量怎么算（由 :mod:`memo_role.memory.manager`
协调 embedder 与本层）。这样拆分的目的是让存储逻辑可以独立测试。

关于「全局统一记忆」：``memory_item`` 表**没有**按私聊/群聊分库，而是统一存放，
只通过 ``session_id`` / ``scope`` 记录来源。召回时默认不按 session 过滤，
因此私聊里说过的事在群聊中同样能被召回，反之亦然。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

from ..db import Database
from ..logging_setup import get_logger
from .embedding import cosine_similarity

logger = get_logger(__name__)

#: 记忆种类
KIND_EPISODIC = "episodic"  # 情节记忆：具体事件、偏好、对话要点
KIND_CORE = "core"  # 核心记忆：身份与长期设定（姓名、生日、职业等）

#: 召回范围
SCOPE_ALL = "all"  # 全局（默认）：跨私聊/群聊统一召回
SCOPE_SESSION = "session"  # 仅当前会话
SCOPE_GLOBAL_ONLY = "global"  # 仅标记为 global 的条目


# ----------------------------------------------------------------------
# 记录对象
# ----------------------------------------------------------------------
@dataclass
class SessionRecord:
    id: str
    kind: str
    platform: str
    peer_id: str
    title: str
    persona_id: str
    model_id: str
    created_at: float
    updated_at: float


@dataclass
class SpeakerRecord:
    id: int
    platform: str
    platform_uid: str
    display_name: str
    aliases: List[str] = field(default_factory=list)


@dataclass
class MessageRecord:
    id: int
    session_id: str
    speaker_id: Optional[int]
    role: str
    content: str
    created_at: float
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryRecord:
    id: int
    kind: str
    scope: str
    content: str
    importance: float
    session_id: Optional[str]
    speaker_id: Optional[int]
    persona_id: Optional[str]
    created_at: float
    updated_at: float
    last_access_at: Optional[float]
    access_count: int
    source_message_id: Optional[int]
    meta: Dict[str, Any] = field(default_factory=dict)
    #: 召回相似度（列表查询时为 None）
    score: Optional[float] = None
    #: 关联发言人昵称（联表查询时填充，便于前端展示「谁说的」）
    speaker_name: str = ""


# ----------------------------------------------------------------------
# 存储
# ----------------------------------------------------------------------
class MemoryStore:
    """记忆读写。"""

    def __init__(self, database: Database):
        self.database = database

    # ==================================================================
    # 会话
    # ==================================================================
    def upsert_session(
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
        """创建或更新会话（存在则只补充非空字段）。"""
        now = time.time()
        with self.database.session() as conn:
            row = conn.execute(
                "SELECT * FROM session WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO session "
                    "(id, kind, platform, peer_id, title, persona_id, model_id, "
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        kind,
                        platform,
                        peer_id,
                        title,
                        persona_id,
                        model_id,
                        now,
                        now,
                    ),
                )
                return
            conn.execute(
                "UPDATE session SET platform = ?, peer_id = ?, title = ?, "
                "persona_id = ?, model_id = ?, updated_at = ? WHERE id = ?",
                (
                    platform or row["platform"],
                    peer_id or row["peer_id"],
                    title or row["title"],
                    persona_id or row["persona_id"],
                    model_id or row["model_id"],
                    now,
                    session_id,
                ),
            )

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        with self.database.session() as conn:
            row = conn.execute(
                "SELECT * FROM session WHERE id = ?", (session_id,)
            ).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(self, limit: int = 100, offset: int = 0) -> List[SessionRecord]:
        with self.database.session() as conn:
            rows = conn.execute(
                "SELECT * FROM session ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def delete_session(self, session_id: str) -> bool:
        """删除会话（其消息与「仅属于该会话」的记忆一并删除）。"""
        with self.database.session() as conn:
            conn.execute(
                "DELETE FROM memory_item WHERE scope = 'session' AND session_id = ?",
                (session_id,),
            )
            cur = conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
        return cur.rowcount > 0

    # ==================================================================
    # 发言人
    # ==================================================================
    def upsert_speaker(
        self, platform: str, platform_uid: str, display_name: str = ""
    ) -> int:
        """登记发言人并返回其 id。

        群聊中同一个人可能改昵称，这里会把旧昵称追加进 ``aliases``，
        避免「改了名字就不认识」。
        """
        now = time.time()
        with self.database.session() as conn:
            row = conn.execute(
                "SELECT * FROM speaker WHERE platform = ? AND platform_uid = ?",
                (platform, platform_uid),
            ).fetchone()

            if row is None:
                cur = conn.execute(
                    "INSERT INTO speaker "
                    "(platform, platform_uid, display_name, aliases, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (platform, platform_uid, display_name, "[]", now, now),
                )
                return int(cur.lastrowid)

            speaker_id = int(row["id"])
            old_name = row["display_name"] or ""
            try:
                aliases: List[str] = json.loads(row["aliases"] or "[]")
            except json.JSONDecodeError:
                aliases = []

            if display_name and display_name != old_name:
                if old_name and old_name not in aliases:
                    aliases.append(old_name)
                conn.execute(
                    "UPDATE speaker SET display_name = ?, aliases = ?, updated_at = ? "
                    "WHERE id = ?",
                    (display_name, json.dumps(aliases, ensure_ascii=False), now, speaker_id),
                )
            return speaker_id

    def get_speaker(self, speaker_id: int) -> Optional[SpeakerRecord]:
        with self.database.session() as conn:
            row = conn.execute(
                "SELECT * FROM speaker WHERE id = ?", (speaker_id,)
            ).fetchone()
        return _row_to_speaker(row) if row else None

    def find_speaker(self, platform: str, platform_uid: str) -> Optional[SpeakerRecord]:
        with self.database.session() as conn:
            row = conn.execute(
                "SELECT * FROM speaker WHERE platform = ? AND platform_uid = ?",
                (platform, platform_uid),
            ).fetchone()
        return _row_to_speaker(row) if row else None

    def list_speakers(self, limit: int = 200) -> List[SpeakerRecord]:
        with self.database.session() as conn:
            rows = conn.execute(
                "SELECT * FROM speaker ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_speaker(r) for r in rows]

    # ==================================================================
    # 消息（工作记忆的事实来源）
    # ==================================================================
    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        speaker_id: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        now = time.time()
        with self.database.session() as conn:
            cur = conn.execute(
                "INSERT INTO message (session_id, speaker_id, role, content, created_at, meta) "
                "VALUES (?,?,?,?,?,?)",
                (
                    session_id,
                    speaker_id,
                    role,
                    content,
                    now,
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
            conn.execute(
                "UPDATE session SET updated_at = ? WHERE id = ?", (now, session_id)
            )
        return int(cur.lastrowid)

    def recent_messages(
        self, session_id: str, limit: int = 12, *, exclude_last: int = 0
    ) -> List[MessageRecord]:
        """取最近 ``limit`` 条消息，按时间正序返回（便于拼上下文）。

        :param exclude_last: 末尾要排除的条数（例如排除「刚写入的这条用户消息」，
            避免在生成时重复注入）
        """
        with self.database.session() as conn:
            rows = conn.execute(
                "SELECT * FROM message WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit + exclude_last),
            ).fetchall()
        records = [_row_to_message(r) for r in rows]
        records.reverse()
        if exclude_last > 0:
            records = records[:-exclude_last]
        return records

    def count_messages(self, session_id: Optional[str] = None) -> int:
        with self.database.session() as conn:
            if session_id:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM message WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS c FROM message").fetchone()
        return int(row["c"])

    def clear_messages(self, session_id: str) -> int:
        """清空某会话的工作记忆（消息），返回删除条数。

        只删消息，不动 ``memory_item``：长期记忆是全局共享的，
        清空上下文不应该把「记住的事」也抹掉。
        """
        with self.database.session() as conn:
            cur = conn.execute("DELETE FROM message WHERE session_id = ?", (session_id,))
            return int(cur.rowcount or 0)

    # ==================================================================
    # 记忆条目
    # ==================================================================
    def add_memory(
        self,
        content: str,
        *,
        kind: str = KIND_EPISODIC,
        scope: str = "global",
        session_id: Optional[str] = None,
        speaker_id: Optional[int] = None,
        persona_id: Optional[str] = None,
        importance: float = 0.5,
        source_message_id: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
        vector: Optional[np.ndarray] = None,
        embedding_signature: Optional[str] = None,
    ) -> int:
        """写入一条记忆；``vector`` 非空时同时落库向量。"""
        now = time.time()
        with self.database.session() as conn:
            cur = conn.execute(
                "INSERT INTO memory_item "
                "(kind, scope, session_id, speaker_id, persona_id, content, importance, "
                " created_at, updated_at, access_count, source_message_id, meta) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
                (
                    kind,
                    scope,
                    session_id,
                    speaker_id,
                    persona_id,
                    content,
                    importance,
                    now,
                    now,
                    source_message_id,
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
            memory_id = int(cur.lastrowid)
            if vector is not None and embedding_signature:
                conn.execute(
                    "INSERT INTO memory_embedding (memory_id, model, dim, vector) "
                    "VALUES (?,?,?,?)",
                    (
                        memory_id,
                        embedding_signature,
                        int(vector.shape[0]),
                        vector.astype(np.float32).tobytes(),
                    ),
                )
        return memory_id

    def get_memory(self, memory_id: int) -> Optional[MemoryRecord]:
        with self.database.session() as conn:
            row = conn.execute(
                "SELECT m.*, s.display_name AS speaker_name FROM memory_item m "
                "LEFT JOIN speaker s ON s.id = m.speaker_id WHERE m.id = ?",
                (memory_id,),
            ).fetchone()
        return _row_to_memory(row) if row else None

    def list_memories(
        self,
        *,
        kinds: Optional[Sequence[str]] = None,
        session_id: Optional[str] = None,
        speaker_id: Optional[int] = None,
        persona_id: Optional[str] = None,
        query_text: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[MemoryRecord]:
        """按条件列出记忆（管理后台用，不做向量排序）。"""
        sql = (
            "SELECT m.*, s.display_name AS speaker_name FROM memory_item m "
            "LEFT JOIN speaker s ON s.id = m.speaker_id WHERE 1 = 1"
        )
        params: List[Any] = []
        if kinds:
            sql += f" AND m.kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        if session_id:
            sql += " AND m.session_id = ?"
            params.append(session_id)
        if speaker_id is not None:
            sql += " AND m.speaker_id = ?"
            params.append(speaker_id)
        if persona_id:
            # 与 search_memories 保持一致：未绑定人设（NULL）的记忆属于全局共享，
            # 指定人设时也应一并返回，否则核心记忆会「忘了你是谁」。
            sql += " AND (m.persona_id = ? OR m.persona_id IS NULL)"
            params.append(persona_id)
        if query_text:
            sql += " AND m.content LIKE ?"
            params.append(f"%{query_text}%")
        sql += " ORDER BY m.importance DESC, m.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self.database.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_memory(r) for r in rows]

    def find_duplicate_memory(
        self,
        content: str,
        *,
        speaker_id: Optional[int] = None,
        kind: Optional[str] = None,
    ) -> Optional[int]:
        """查找内容完全相同的记忆 id（用于写入前去重）。

        去重很有必要：用户可能反复说「我叫小明」，没有去重就会不断堆积重复条目，
        既浪费空间也稀释召回效果。
        """
        sql = "SELECT id FROM memory_item WHERE content = ?"
        params: List[Any] = [content]
        if speaker_id is not None:
            sql += " AND speaker_id = ?"
            params.append(speaker_id)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " LIMIT 1"
        with self.database.session() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["id"]) if row else None

    def count_memories(self, kinds: Optional[Sequence[str]] = None) -> int:
        with self.database.session() as conn:
            if kinds:
                row = conn.execute(
                    f"SELECT COUNT(*) AS c FROM memory_item "
                    f"WHERE kind IN ({','.join('?' * len(kinds))})",
                    list(kinds),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS c FROM memory_item").fetchone()
        return int(row["c"])

    def update_memory(self, memory_id: int, **fields: Any) -> bool:
        """更新记忆的允许字段。"""
        allowed = {"content", "importance", "kind", "scope", "persona_id", "meta"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        if "meta" in updates and not isinstance(updates["meta"], str):
            updates["meta"] = json.dumps(updates["meta"], ensure_ascii=False)
        assignments = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [time.time(), memory_id]
        with self.database.session() as conn:
            cur = conn.execute(
                f"UPDATE memory_item SET {assignments}, updated_at = ? WHERE id = ?",
                params,
            )
        return cur.rowcount > 0

    def delete_memory(self, memory_id: int) -> bool:
        with self.database.session() as conn:
            cur = conn.execute("DELETE FROM memory_item WHERE id = ?", (memory_id,))
        return cur.rowcount > 0

    def touch_memories(self, memory_ids: Iterable[int]) -> None:
        """记录「被召回过」：更新访问次数与时间（用于后续重要性衰减/排序）。"""
        ids = [int(i) for i in memory_ids]
        if not ids:
            return
        now = time.time()
        placeholders = ",".join("?" * len(ids))
        with self.database.session() as conn:
            conn.execute(
                f"UPDATE memory_item SET last_access_at = ?, access_count = access_count + 1 "
                f"WHERE id IN ({placeholders})",
                [now, *ids],
            )

    # ==================================================================
    # 向量召回
    # ==================================================================
    def search_memories(
        self,
        query_vector: np.ndarray,
        embedding_signature: str,
        *,
        kinds: Optional[Sequence[str]] = None,
        scope: str = SCOPE_ALL,
        session_id: Optional[str] = None,
        speaker_id: Optional[int] = None,
        persona_id: Optional[str] = None,
        top_k: int = 6,
        min_score: float = 0.0,
        candidate_limit: Optional[int] = None,
    ) -> List[MemoryRecord]:
        """向量相似度召回。

        :param scope: ``all``（默认，跨会话全局召回）/ ``session`` / ``global``
        :param min_score: 相似度下限，低于该值不返回
        :param candidate_limit: 扫描上限；``None`` 表示扫描全部候选
            （记忆量很大时可设置该值做粗筛，代价是可能漏掉较老的条目）
        """
        # 零向量（例如空查询文本）与任何记忆的余弦相似度都是 0，若继续按
        # min_score=0 过滤会把全部记忆都召回，产生纯噪声，因此直接返回空。
        if float(np.linalg.norm(query_vector)) == 0.0:
            return []

        sql = (
            "SELECT m.*, e.vector AS embedding_blob, s.display_name AS speaker_name "
            "FROM memory_item m "
            "JOIN memory_embedding e ON e.memory_id = m.id "
            "LEFT JOIN speaker s ON s.id = m.speaker_id "
            "WHERE e.model = ?"
        )
        params: List[Any] = [embedding_signature]

        if kinds:
            sql += f" AND m.kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        if scope == SCOPE_SESSION:
            sql += " AND m.session_id = ?"
            params.append(session_id)
        elif scope == SCOPE_GLOBAL_ONLY:
            sql += " AND m.scope = 'global'"
        # scope == SCOPE_ALL：不加会话条件 —— 这正是「全局统一记忆」的实现方式
        if speaker_id is not None:
            sql += " AND m.speaker_id = ?"
            params.append(speaker_id)
        if persona_id:
            # 人设维度：只召回「该人设」或「未绑定人设」的记忆
            sql += " AND (m.persona_id = ? OR m.persona_id IS NULL)"
            params.append(persona_id)

        sql += " ORDER BY m.id DESC"
        if candidate_limit:
            sql += " LIMIT ?"
            params.append(candidate_limit)

        with self.database.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return []

        dim = int(query_vector.shape[0])
        vectors: List[np.ndarray] = []
        records: List[MemoryRecord] = []
        for row in rows:
            blob = row["embedding_blob"]
            if not blob:
                continue
            vector = np.frombuffer(blob, dtype=np.float32)
            # 维度不一致说明向量空间已变（例如换过 embedding 后端），跳过
            if vector.shape[0] != dim:
                continue
            vectors.append(vector)
            records.append(_row_to_memory(row))

        if not vectors:
            return []

        matrix = np.vstack(vectors)
        scores = cosine_similarity(matrix, query_vector)

        ranked = sorted(
            zip(records, scores.tolist()), key=lambda pair: pair[1], reverse=True
        )
        results: List[MemoryRecord] = []
        for record, score in ranked:
            if score < min_score:
                break
            record.score = float(score)
            results.append(record)
            if len(results) >= top_k:
                break
        return results

    # ==================================================================
    # 统计
    # ==================================================================
    def stats(self) -> Dict[str, Any]:
        """汇总统计，供管理后台首页展示。"""
        with self.database.session() as conn:
            sessions = conn.execute("SELECT COUNT(*) AS c FROM session").fetchone()["c"]
            messages = conn.execute("SELECT COUNT(*) AS c FROM message").fetchone()["c"]
            speakers = conn.execute("SELECT COUNT(*) AS c FROM speaker").fetchone()["c"]
            episodic = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_item WHERE kind = ?", (KIND_EPISODIC,)
            ).fetchone()["c"]
            core = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_item WHERE kind = ?", (KIND_CORE,)
            ).fetchone()["c"]
            vectors = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_embedding"
            ).fetchone()["c"]
        return {
            "sessions": int(sessions),
            "messages": int(messages),
            "speakers": int(speakers),
            "episodic": int(episodic),
            "core": int(core),
            "vectors": int(vectors),
        }


# ----------------------------------------------------------------------
# 行 -> 记录
# ----------------------------------------------------------------------
def _safe_json(text: Any) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _row_to_session(row: Any) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        kind=row["kind"],
        platform=row["platform"],
        peer_id=row["peer_id"],
        title=row["title"],
        persona_id=row["persona_id"],
        model_id=row["model_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_speaker(row: Any) -> SpeakerRecord:
    try:
        aliases = json.loads(row["aliases"] or "[]")
    except json.JSONDecodeError:
        aliases = []
    return SpeakerRecord(
        id=int(row["id"]),
        platform=row["platform"],
        platform_uid=row["platform_uid"],
        display_name=row["display_name"],
        aliases=aliases if isinstance(aliases, list) else [],
    )


def _row_to_message(row: Any) -> MessageRecord:
    return MessageRecord(
        id=int(row["id"]),
        session_id=row["session_id"],
        speaker_id=row["speaker_id"],
        role=row["role"],
        content=row["content"],
        created_at=row["created_at"],
        meta=_safe_json(row["meta"]),
    )


def _row_to_memory(row: Any) -> MemoryRecord:
    keys = row.keys()
    return MemoryRecord(
        id=int(row["id"]),
        kind=row["kind"],
        scope=row["scope"],
        content=row["content"],
        importance=float(row["importance"]),
        session_id=row["session_id"],
        speaker_id=row["speaker_id"],
        persona_id=row["persona_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_access_at=row["last_access_at"],
        access_count=int(row["access_count"]),
        source_message_id=row["source_message_id"],
        meta=_safe_json(row["meta"]),
        speaker_name=(row["speaker_name"] or "") if "speaker_name" in keys else "",
    )