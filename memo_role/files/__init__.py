"""可视化文件管理（受限于项目根目录的沙箱）。

- :mod:`memo_role.files.sandbox` 纯逻辑：路径校验 / 读写 / 增删改
- HTTP 路由见 :mod:`memo_role.web.api.files`

拆开的原因是沙箱逻辑不依赖 Web 框架，可以完全离线单测；而路由层只做
「参数绑定 + 异常映射」，很薄。
"""

from __future__ import annotations

from .sandbox import (
    FileSandbox,
    PathEscapeError,
    ProtectedPathError,
    SandboxError,
    SandboxNotFoundError,
    TooLargeError,
    clean_filename,
    clean_rel_path,
)

__all__ = [
    "FileSandbox",
    "PathEscapeError",
    "ProtectedPathError",
    "SandboxError",
    "SandboxNotFoundError",
    "TooLargeError",
    "clean_filename",
    "clean_rel_path",
]