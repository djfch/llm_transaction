"""logger 配置测试。"""

import logging
from pathlib import Path

from src.audit.logger import get_logger, setup_logging


def test_setup_logging_creates_file(tmp_path: Path):
    setup_logging(str(tmp_path), "DEBUG")
    get_logger("test").info("你好")
    for h in logging.getLogger().handlers:
        h.flush()
    assert (tmp_path / "agent.log").exists()
    content = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert "你好" in content


def test_setup_logging_idempotent(tmp_path: Path):
    setup_logging(str(tmp_path))
    before = len(logging.getLogger().handlers)
    setup_logging(str(tmp_path))
    assert len(logging.getLogger().handlers) == before
