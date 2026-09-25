"""SQLite 持久化基础设施。

目标：单文件数据库、零外部服务、低内存占用，适配安卓 PRoot 与低配 PC。

设计要点：

- 每次操作使用短连接（``with db.connect() as conn``），避免连接长期占用；
  SQLite 单文件 + WAL 模式对小规模并发足够。
- 建表语句集中为 ``SCHEMA_STATEMENTS``，通过 ``CREATE TABLE IF NOT EXISTS``
  实现幂等初始化，后续版本可追加语句做增量迁移。
- 向量以 ``float32`` 小端字节串存 BLOB，读取时再还原，避免额外存储开销。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence

SCHEMA_VERSION = 1

# ----------------------------------------------------------------------
# 建表语句（幂等）
# ----------------------------------------------------------------------
SCHEMA_STATEMENTS: Sequence[str] = (
    # 表结构版本，便于后续做增量迁移
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # 会话：一次持续的聊天上下文（私聊或群聊）
    """
    CREATE TABLE IF NOT EXISTS session (
        id           TEXT PRIMARY KEY,          -- private:<uid> / group:<gid> / web:<sid>
        kind         TEXT NOT NULL,             -- private | group | web
        platform     TEXT NOT NULL DEFAULT 'local',  -- qq | local | web
        peer_id      TEXT NOT NULL DEFAULT '',  -- 对方 uid 或群号
        title        TEXT NOT NULL DEFAULT '',
        persona_id   TEXT NOT NULL DEFAULT '',
        model_id     TEXT NOT NULL DEFAULT '',
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL
    )
    """,
    # 发言人：群聊中区分不同人，私聊也统一登记，保证记忆可关联发言人维度
    """
    CREATE TABLE IF NOT EXISTS speaker (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        platform     TEXT NOT NULL,
        platform_uid TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        aliases      TEXT NOT NULL DEFAULT '[]',  -- JSON 数组，曾用昵称
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL,
        UNIQUE (platform, platform_uid)
    )
    """,
    # 原始消息流水：工作记忆的事实来源
    """
    CREATE TABLE IF NOT EXISTS message (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        speaker_id INTEGER,
        role       TEXT NOT NULL,              -- user | assistant | system
        content    TEXT NOT NULL,
        created_at REAL NOT NULL,
        meta       TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY (session_id) REFERENCES session (id) ON DELETE CASCADE,
        FOREIGN KEY (speaker_id) REFERENCES speaker (id) ON DELETE SET NULL
    )
    """,
    # 记忆条目：情节记忆（episodic）与核心记忆（core）共用一张表
    """
    CREATE TABLE IF NOT EXISTS memory_item (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL,             -- episodic | core
        scope        TEXT NOT NULL DEFAULT 'global',  -- global | session | speaker
        session_id   TEXT,                      -- 来源会话（global 也可留痕）
        speaker_id   INTEGER,                   -- 关联发言人，可空
        persona_id   TEXT,                      -- 关联人设，可空
        content      TEXT NOT NULL,
        importance   REAL NOT NULL DEFAULT 0.5,
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL,
        last_access_at REAL,
        access_count INTEGER NOT NULL DEFAULT 0,
        source_message_id INTEGER,
        meta         TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY (speaker_id) REFERENCES speaker (id) ON DELETE SET NULL,
        FOREIGN KEY (source_message_id) REFERENCES message (id) ON DELETE SET NULL
    )
    """,
    # 记忆向量：与 memory_item 一对一，整数主键即 memory_item.id
    """
    CREATE TABLE IF NOT EXISTS memory_embedding (
        memory_id INTEGER PRIMARY KEY,
        model     TEXT NOT NULL,
        dim       INTEGER NOT NULL,
        vector    BLOB NOT NULL,
        FOREIGN KEY (memory_id) REFERENCES memory_item (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_message_session ON message (session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_item (kind, scope)",
    "CREATE INDEX IF NOT EXISTS idx_memory_speaker ON memory_item (speaker_id)",
    "CREATE INDEX IF NOT EXISTS idx_session_updated ON session (updated_at)",
)


class Database:
    """轻量 SQLite 封装。

    实际路径解析由上层（``AppConfig.db_path``）完成，这里只关心读写。
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """建立一个新的连接（调用方负责关闭，推荐用 ``session()``）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        # WAL 提升读写并发；低配设备上依然很轻
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """上下文管理器：正常结束提交，异常回滚，最后一定关闭。"""
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def init_schema(self) -> None:
        """建表（幂等）。可重复调用。"""
        with self._lock, self.session() as conn:
            for stmt in SCHEMA_STATEMENTS:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def get_schema_version(self) -> Optional[int]:
        """读取当前表结构版本；未初始化时返回 ``None``。"""
        with self.session() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
        return int(row["value"]) if row else None

    def is_initialized(self) -> bool:
        """判断数据库是否已完成初始化。"""
        with self.session() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_item'"
            ).fetchone()
        return row is not None