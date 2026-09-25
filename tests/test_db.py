"""SQLite 基础设施测试。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from memo_role.db import SCHEMA_VERSION, Database


def test_init_schema_is_idempotent(tmp_root: Path) -> None:
    """重复建表不应报错，且版本号正确写入。"""
    db = Database(tmp_root / "data" / "memo_role.db")
    assert db.is_initialized() is False
    db.init_schema()
    db.init_schema()  # 第二次调用
    assert db.is_initialized() is True
    assert db.get_schema_version() == SCHEMA_VERSION


def test_connect_creates_parent_dir(tmp_root: Path) -> None:
    """父目录不存在时应自动创建。"""
    db = Database(tmp_root / "nested" / "deep" / "x.db")
    db.init_schema()
    assert (tmp_root / "nested" / "deep" / "x.db").exists()


def test_row_factory_returns_mapping(db: Database) -> None:
    """查询结果应支持按列名访问。"""
    with db.session() as conn:
        conn.execute(
            "INSERT INTO session (id, kind, created_at, updated_at) VALUES (?,?,?,?)",
            ("web:1", "web", time.time(), time.time()),
        )
    with db.session() as conn:
        row = conn.execute("SELECT * FROM session WHERE id = ?", ("web:1",)).fetchone()
    assert row["kind"] == "web"


def test_session_rolls_back_on_error(db: Database) -> None:
    """上下文内抛异常应回滚。"""
    with pytest.raises(RuntimeError):
        with db.session() as conn:
            conn.execute(
                "INSERT INTO session (id, kind, created_at, updated_at) VALUES (?,?,?,?)",
                ("web:rollback", "web", time.time(), time.time()),
            )
            raise RuntimeError("boom")

    with db.session() as conn:
        row = conn.execute(
            "SELECT id FROM session WHERE id = ?", ("web:rollback",)
        ).fetchone()
    assert row is None


def test_message_cascade_on_session_delete(db: Database) -> None:
    """删除会话应级联删除其消息。"""
    now = time.time()
    with db.session() as conn:
        conn.execute(
            "INSERT INTO session (id, kind, created_at, updated_at) VALUES (?,?,?,?)",
            ("private:1", "private", now, now),
        )
        conn.execute(
            "INSERT INTO message (session_id, role, content, created_at) VALUES (?,?,?,?)",
            ("private:1", "user", "你好", now),
        )
        conn.execute("DELETE FROM session WHERE id = ?", ("private:1",))

    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM message").fetchone()["c"]
    assert count == 0


def test_foreign_key_enforced(db: Database) -> None:
    """外键约束应生效（session_id 不存在时拒绝插入消息）。"""
    with pytest.raises(sqlite3.IntegrityError):
        with db.session() as conn:
            conn.execute(
                "INSERT INTO message (session_id, role, content, created_at) "
                "VALUES (?,?,?,?)",
                ("not-exist", "user", "hi", time.time()),
            )


def test_embedding_cascades_with_memory(db: Database) -> None:
    """删除记忆条目应级联删除其向量。"""
    now = time.time()
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO memory_item (kind, content, created_at, updated_at) "
            "VALUES ('episodic', '用户喜欢猫', ?, ?)",
            (now, now),
        )
        memory_id = cur.lastrowid
        conn.execute(
            "INSERT INTO memory_embedding (memory_id, model, dim, vector) VALUES (?,?,?,?)",
            (memory_id, "hashing", 4, b"\x00\x00\x00\x00"),
        )
        conn.execute("DELETE FROM memory_item WHERE id = ?", (memory_id,))

    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM memory_embedding").fetchone()["c"]
    assert count == 0


def test_speaker_unique_per_platform(db: Database) -> None:
    """同一平台下 platform_uid 唯一。"""
    now = time.time()
    with db.session() as conn:
        conn.execute(
            "INSERT INTO speaker (platform, platform_uid, created_at, updated_at) "
            "VALUES ('qq','10001',?,?)",
            (now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with db.session() as conn:
            conn.execute(
                "INSERT INTO speaker (platform, platform_uid, created_at, updated_at) "
                "VALUES ('qq','10001',?,?)",
                (now, now),
            )