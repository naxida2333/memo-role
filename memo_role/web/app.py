"""FastAPI 应用组装：路由、异常映射、静态页与反向 WS。

组装顺序上的两个要点：

1. **异常处理器在路由之外**：沙箱越界、会话不存在这类错误由部件抛出，
   统一在这里映射成状态码，路由里就不用到处 ``try``；
2. **反向 WS 后挂**：NapCat 的 ``/onebot/v11/ws`` 只在配置启用时才注册，
   否则普通用户会看到一个能连上但没人处理的端点。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..files import (
    ProtectedPathError,
    SandboxError,
    SandboxNotFoundError,
    TooLargeError,
)
from ..inference.base import InferenceError, ModelNotFoundError
from ..logging_setup import get_logger
from ..persona.manager import PersonaNotFoundError
from .api import dialogue, files, memories, models, personas, system
from .errors import WebError
from .serialize import error_body
from .state import AppState, build_state

logger = get_logger(__name__)

#: 前端静态资源目录
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: 页面路由 → 模板文件
PAGES = {
    "/": "index.html",
    "/admin": "admin.html",
    "/files": "files.html",
}

#: 异常类型 → HTTP 状态码（顺序无关，Starlette 按异常的实际类型查）
_EXCEPTION_STATUS = {
    SandboxNotFoundError: 404,
    TooLargeError: 413,
    ProtectedPathError: 400,
    SandboxError: 400,
    PersonaNotFoundError: 404,
    ModelNotFoundError: 400,
    InferenceError: 400,
    ValueError: 400,
}


def create_app(
    state: Optional[AppState] = None, *, cfg: Any = None
) -> FastAPI:
    """创建应用。

    :param state: 注入已装配的部件容器（测试常用）
    :param cfg: 未注入 ``state`` 时使用的配置；再缺省则按默认规则加载
    """
    if state is None:
        if cfg is None:
            from ..config import load_config

            cfg = load_config()
        state = build_state(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "memo-role Web 已启动：http://%s:%s",
            state.cfg.server.host,
            state.cfg.server.port,
        )
        try:
            yield
        finally:
            logger.info("正在释放推理后端与记忆资源…")
            state.close()

    app = FastAPI(
        title="memo-role",
        description="本地离线多模型角色扮演机器人的对话页与管理后台",
        version=__version__,
        lifespan=lifespan,
    )
    # 路由通过 Depends(get_state) 取部件，这里把容器挂到应用上
    app.state.memo = state

    _register_exception_handlers(app)
    _register_api(app)
    _register_pages(app)
    _register_websocket(app, state)
    return app


# ----------------------------------------------------------------------
# 注册
# ----------------------------------------------------------------------
def _register_api(app: FastAPI) -> None:
    for module in (dialogue, files, memories, models, personas, system):
        app.include_router(module.router, prefix="/api")


def _register_pages(app: FastAPI) -> None:
    """页面与静态资源。

    前端是无构建步骤的原生 HTML/JS，直接由服务端提供：这样安卓 PRoot 上
    不需要 node，把项目目录拷过去就能跑。
    """
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for route, filename in PAGES.items():
        app.get(route, include_in_schema=False)(
            _page_handler(filename)
        )


def _page_handler(filename: str):
    """为每个页面生成处理器（避免在循环里闭包捕获同一个变量）。"""

    def handler() -> FileResponse:
        target = STATIC_DIR / filename
        return FileResponse(target, media_type="text/html")

    return handler


def _register_websocket(app: FastAPI, state: AppState) -> None:
    if state.napcat is not None:
        state.napcat.mount(app)
        logger.info("已挂载 OneBot 反向 WebSocket：%s", state.napcat.ws_path)


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(WebError)
    async def _web_error(_: Any, exc: WebError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(str(exc), code=type(exc).__name__),
        )

    @app.exception_handler(SandboxError)
    async def _sandbox_error(_: Any, exc: SandboxError) -> JSONResponse:
        status = _EXCEPTION_STATUS.get(type(exc), 400)
        return JSONResponse(
            status_code=status,
            content=error_body(str(exc), code=type(exc).__name__),
        )

    @app.exception_handler(PersonaNotFoundError)
    async def _persona_error(_: Any, exc: PersonaNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=error_body(str(exc) or "人设不存在", code="PersonaNotFound"),
        )

    @app.exception_handler(ModelNotFoundError)
    @app.exception_handler(InferenceError)
    async def _inference_error(_: Any, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_body(str(exc), code=type(exc).__name__),
        )

    @app.exception_handler(ValueError)
    async def _value_error(_: Any, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content=error_body(str(exc), code="ValueError")
        )