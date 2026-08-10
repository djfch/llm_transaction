"""运行日志配置：控制台 + 按天轮转的文件日志。

所有模块通过 get_logger 获取 logger，禁止自行配置 root handler。
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_configured = False


def setup_logging(log_dir: str, level: str = "INFO") -> None:
    """初始化控制台与按天轮转的全局文件日志，重复调用时保持现有配置。

    参数：
        log_dir: str，保存 agent.log 的日志目录
        level: str，根日志器级别名称，非法值按 INFO 处理

    返回：
        None，首次调用时创建目录并就地配置根日志器
    """
    global _configured
    if _configured:
        return
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        Path(log_dir) / "agent.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的日志记录器，用于模块内统一打日志。

    参数：
        name: str，日志记录器名称，通常传模块名（如 __name__）

    返回：
        logging.Logger：标准库日志记录器，handler 由 setup_logging 统一配置
    """
    return logging.getLogger(name)
