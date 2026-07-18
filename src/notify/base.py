"""通知接口定义：业务层只依赖 Notifier Protocol，不关心具体通道。"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """通知器协议：发送文本消息（通道可自行解释标记，如 Telegram 用 HTML）。"""

    async def send(self, text: str) -> bool:
        """发送消息；返回是否真正送达外部通道（降级实现返回 False）。"""
        ...


class LogNotifier:
    """降级通知器：不真正外发，仅写日志（通知内容不丢失，便于排查）。"""

    def __init__(self, name: str = "notify") -> None:
        self._logger = logging.getLogger(f"src.notify.{name}")

    async def send(self, text: str) -> bool:
        self._logger.info("[通知降级输出] %s", text)
        return False
