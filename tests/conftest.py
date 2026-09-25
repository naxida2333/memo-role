"""pytest 公共夹具。

统一在此注入 ``memo_role`` 的导入路径，保证未 ``pip install`` 也能跑测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 仓库根目录加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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