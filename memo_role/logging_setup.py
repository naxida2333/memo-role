"""统一日志配置。

只依赖标准库 ``logging``，输出到控制台 + 可选滚动文件，适配安卓 PRoot 环境
（不引入 loguru 等额外依赖，减少安装体积）。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 标记是否已初始化，避免重复添加 handler
_configured = False


def setup_logging(
    level: str = "INFO",
    log_file: Optional[Path] = None,
    max_bytes: int = 1_000_000,
    backup_count: int = 3,
    force: bool = False,
) -> logging.Logger:
    """初始化根日志器。

    :param level: 日志级别名（DEBUG/INFO/WARNING/ERROR）
    :param log_file: 日志文件路径，``None`` 表示只输出控制台
    :param max_bytes: 单文件最大字节数，超出后滚动
    :param backup_count: 保留的历史日志份数
    :param force: 为 ``True`` 时无论是否已初始化都重新配置（测试用）
    """
    global _configured
    root_logger = logging.getLogger()

    if _configured and not force:
        return root_logger

    # 清理已有 handler，保证重复调用不会重复打印
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()

    root_logger.setLevel(_normalize_level(level))
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    _configured = True
    return root_logger


def _normalize_level(level: str) -> int:
    """把级别名转成 logging 常量；非法值回退到 INFO。"""
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """获取子日志器（统一走根配置）。"""
    return logging.getLogger(name)