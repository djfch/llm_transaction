"""运行日志配置：控制台 + 按天轮转的文件日志。

所有模块通过 get_logger 获取 logger，禁止自行配置 root handler。
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_configured = False


def setup_logging(log_dir: str, level: str = "INFO") -> None:
    """初始化全局日志：控制台 + logs/agent.log（按天轮转，保留 14 天）。"""
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
    return logging.getLogger(name)
