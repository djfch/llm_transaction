"""logger 配置测试。"""

import logging
from pathlib import Path

from src.audit.logger import get_logger, setup_logging


def test_setup_logging_creates_file(tmp_path: Path):
    """校验初始化日志后生成 agent.log 且中文日志内容正确写入。

    参数：
        tmp_path: Path，pytest 临时目录夹具，日志文件落在其中

    返回：
        None，断言临时目录下生成 agent.log，且写入的中文消息可在文件内容中读到
    """
    setup_logging(str(tmp_path), "DEBUG")
    get_logger("test").info("你好")
    for h in logging.getLogger().handlers:
        h.flush()
    assert (tmp_path / "agent.log").exists()
    content = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert "你好" in content


def test_setup_logging_idempotent(tmp_path: Path):
    """校验重复调用 setup_logging 不会重复添加处理器（幂等）。

    参数：
        tmp_path: Path，pytest 临时目录夹具，日志文件落在其中

    返回：
        None，断言第二次调用后根 logger 的处理器数量与第一次调用后相同
    """
    setup_logging(str(tmp_path))
    before = len(logging.getLogger().handlers)
    setup_logging(str(tmp_path))
    assert len(logging.getLogger().handlers) == before
