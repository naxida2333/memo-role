"""Web 层：对话页 + 管理后台 + 文件管理（FastAPI + 原生 HTML/JS）。

分层：

- :mod:`memo_role.web.state`   全部依赖的装配（引擎 / 记忆 / 人设 / 后端池 / 文件沙箱）
- :mod:`memo_role.web.app`     FastAPI 应用组装（路由、异常映射、静态页）
- :mod:`memo_role.web.api`     REST 路由，只做「参数绑定 + 调部件 + 序列化」
- ``static/``                  前端页面（无构建步骤，直接用浏览器打开也能读）

之所以把部件装配单独抽成一层：后端池同一时刻只能有一个活跃模型、文件沙箱的
受保护清单只应在启动时确定一次。这些部件必须是全局唯一的，交给 ``AppState``
统一持有，路由通过 ``Depends`` 取用，避免每个请求各建一份。
"""

from __future__ import annotations

from .state import AppState, RuntimeSettings, build_state

__all__ = ["AppState", "RuntimeSettings", "build_state"]