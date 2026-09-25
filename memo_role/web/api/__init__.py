"""REST 路由层。

统一约定：

- 所有接口挂在 ``/api`` 前缀下，返回 JSON（SSE 除外）
- 请求体用 pydantic 模型声明，字段缺失即报 422（框架行为）
- 异常不在路由里逐个 ``try``，而是交给 ``app.py`` 的异常处理器统一映射，
  保证同一类错误在任何路由上都返回同样的状态码
"""

from __future__ import annotations

from .deps import get_state
from . import dialogue, files, memories, models, personas, system

__all__ = ["get_state", "dialogue", "files", "memories", "models", "personas", "system"]