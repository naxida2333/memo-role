"""命令行入口：``python -m memo_role``。

只做三件事：加载配置 → 装配部件 → 交给 uvicorn。这样「部署」在安卓 PRoot 上
就是一条命令，不需要额外的启动脚本或进程管理器。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import List, Optional

#: 热重载模式下，子进程需要重新读到的配置路径（经由环境变量传递）
_CONFIG_ENV = "MEMO_ROLE_CONFIG"
_ROOT_ENV = "MEMO_ROLE_ROOT"


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)

    cfg = _load(args)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    if args.log_level:
        cfg.logging.level = args.log_level
    if args.reload:
        cfg.server.reload = True

    if args.check:
        # 自检不需要日志落盘（手机上没有写权限时反而会多一条误导性的报错）
        return _run_check(cfg, args.config)

    from .logging_setup import setup_logging

    setup_logging(cfg.logging.level, log_file=cfg.log_file)

    import uvicorn

    if cfg.server.reload:
        # 热重载要求以「导入字符串 + factory」方式启动，否则 uvicorn 无法
        # 在子进程里重建应用（直接传 app 对象会失去重载能力）
        os.environ[_CONFIG_ENV] = args.config or ""
        os.environ[_ROOT_ENV] = str(cfg.root)
        uvicorn.run(
            "memo_role.__main__:create_app_factory",
            factory=True,
            host=cfg.server.host,
            port=cfg.server.port,
            reload=True,
            log_config=None,
        )
    else:
        from .web.app import create_app
        from .web.state import build_state

        uvicorn.run(
            create_app(build_state(cfg)),
            host=cfg.server.host,
            port=cfg.server.port,
            log_config=None,
        )
    return 0


def create_app_factory():
    """供 uvicorn 热重载使用的工厂（从环境变量恢复配置）。"""
    from .web.app import create_app
    from .web.state import build_state

    args = argparse.Namespace(
        config=os.environ.get(_CONFIG_ENV) or None,
        root=os.environ.get(_ROOT_ENV) or None,
        host=None,
        port=None,
        log_level=None,
        reload=False,
    )
    return create_app(build_state(_load(args)))


def _run_check(cfg, config_path: Optional[str]) -> int:
    """执行自检并打印报告；有失败项时返回 1（便于脚本判断）。"""
    from .selfcheck import failed, format_report, run_checks

    results = run_checks(cfg, config_path=config_path)
    print(format_report(results))
    return 1 if failed(results) else 0


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="memo_role",
        description="本地离线多模型角色扮演机器人（对话页 + 管理后台 + OneBot 适配）",
    )
    parser.add_argument("--config", default=None, help="配置文件路径；缺省自动查找 config.yaml")
    parser.add_argument(
        "--root", default=None, help="项目根目录（同时是文件管理沙箱的根）；默认当前目录"
    )
    parser.add_argument("--host", default=None, help="监听地址，覆盖配置值")
    parser.add_argument("--port", type=int, default=None, help="监听端口，覆盖配置值")
    parser.add_argument("--reload", action="store_true", help="开启热重载（开发用）")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做启动自检（依赖 / 配置 / 目录 / 模型 / 端口）并退出，不启动服务",
    )
    parser.add_argument("--log-level", default=None, help="日志级别：DEBUG/INFO/WARNING/ERROR")
    return parser.parse_args(argv)


def _load(args: argparse.Namespace):
    """加载配置。

    ``--root`` 缺省时用当前工作目录：这样在任意目录下运行
    ``python -m memo_role`` 都会把那个目录当作项目根（数据、人设、模型都在里面），
    符合「整个项目拷到 U 盘里就能跑」的使用方式。
    """
    from .config import load_config

    root = Path(args.root).expanduser().resolve() if args.root else Path.cwd().resolve()
    return load_config(path=args.config, root=root)


if __name__ == "__main__":  # pragma: no cover - 由解释器调用
    raise SystemExit(main())