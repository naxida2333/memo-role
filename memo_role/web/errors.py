"""错误类型：路由层用它表达 HTTP 语义，由 ``app.py`` 统一映射成状态码。

不直接抛 ``HTTPException`` 的原因：路由之外的部件（引擎、存储）也会报错，
用一组与框架无关的异常能让映射规则集中在一处，也便于离线单测。
"""

from __future__ import annotations


class WebError(Exception):
    """Web 层错误基类。"""

    #: 默认 HTTP 状态码
    status_code = 400


class BadRequestError(WebError):
    """请求不合法（参数非法、内容超限等）。"""

    status_code = 400


class NotFoundError(WebError):
    """目标不存在（会话 / 人设 / 模型 / 文件）。"""

    status_code = 404


class ConflictError(WebError):
    """与现有状态冲突（如人设 id 已存在）。"""

    status_code = 409