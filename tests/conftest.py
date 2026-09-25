"""pytest 公共夹具。

统一在此注入 ``memo_role`` 的导入路径，保证未 ``pip install`` 也能跑测试。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

import pytest

# 仓库根目录加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memo_role.inference.base import (  # noqa: E402 - 需先注入 sys.path
    ChatBackend,
    ChatMessage,
    GenParams,
)


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    """临时项目根目录，用于隔离配置 / 数据库 / 人设等文件。"""
    return tmp_path


@pytest.fixture
def cfg(tmp_root: Path):
    """使用临时根目录加载的默认配置（屏蔽宿主机环境变量）。"""
    from memo_role.config import load_config

    return load_config(root=tmp_root, environ={})


@pytest.fixture
def db(cfg):
    """已完成建表的临时数据库。"""
    from memo_role.db import Database

    database = Database(cfg.db_path)
    database.init_schema()
    return database


# ----------------------------------------------------------------------
# 对话相关公共夹具
# ----------------------------------------------------------------------
class FakeBackend(ChatBackend):
    """测试用假推理后端：记录收到的消息，返回固定回复，且支持流式。

    继承 ``ChatBackend`` 以强制实现同一套接口，避免测试与真实后端脱节。
    """

    name = "fake"
    supports_stream = True

    def __init__(self, reply: str = "好的，我明白了", model: str = "fake-model") -> None:
        self.reply = reply
        self.model = model
        self.calls: List[List[object]] = []
        self.closed = False

    def chat(self, messages: Sequence[ChatMessage], params: Optional[GenParams] = None) -> str:
        self.calls.append(list(messages))
        return self.reply

    def stream(
        self, messages: Sequence[ChatMessage], params: Optional[GenParams] = None
    ) -> Iterator[str]:
        self.calls.append(list(messages))
        for char in self.reply:
            yield char

    def is_available(self) -> bool:
        return True

    def describe(self) -> dict:
        return {"backend": self.name, "available": True, "model": self.model}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def backend_factory(fake_backend):
    """后端工厂：任何模型都交给同一个假后端（避免测试触碰真实推理）。"""

    def factory(cfg, model_id=None):
        return fake_backend

    return factory


@pytest.fixture
def memory_manager(cfg, db):
    """离线可测的记忆管理器（特征哈希 + 规则提取）。"""
    from memo_role.memory.embedding import HashingEmbedding
    from memo_role.memory.extractor import RuleBasedExtractor
    from memo_role.memory.manager import MemoryManager

    cfg.memory.recall_min_score = 0.05
    return MemoryManager.build(
        cfg,
        database=db,
        embedder=HashingEmbedding(dim=128),
        extractor=RuleBasedExtractor(),
    )


@pytest.fixture
def persona_manager(cfg):
    """已写入内置人设的人设管理器。"""
    from memo_role.persona.manager import PersonaManager

    return PersonaManager.build(cfg)


# ----------------------------------------------------------------------
# Web 层夹具
# ----------------------------------------------------------------------
@pytest.fixture
def web_state(tmp_root, backend_factory):
    """Web 部件容器：假推理后端 + 临时根目录，完全离线。"""
    from memo_role.config import load_config
    from memo_role.web.state import build_state

    cfg = load_config(root=tmp_root, environ={})
    return build_state(cfg, backend_factory=backend_factory)


@pytest.fixture
def web_client(web_state):
    """FastAPI 测试客户端（进入上下文以触发 lifespan 的启动 / 释放）。"""
    from fastapi.testclient import TestClient

    from memo_role.web.app import create_app

    with TestClient(create_app(web_state)) as client:
        yield client