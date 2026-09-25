"""路由层公共依赖。"""

from __future__ import annotations

from fastapi import Request

from ..state import AppState


def get_state(request: Request) -> AppState:
    """取全局部件容器（在 ``app.py`` 启动时挂到 ``app.state.memo``）。"""
    return request.app.state.memo