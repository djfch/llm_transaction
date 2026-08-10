"""通知接口定义：业务层只依赖 Notifier Protocol，不关心具体通道。"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """通知器协议：发送文本消息（通道可自行解释标记，如 Telegram 用 HTML）。"""

    async def send(self, text: str) -> bool:
        """向具体通知通道发送文本消息。

        参数：
            text: str，待发送的通知文本

        返回：
            bool，消息是否真正送达外部通道
        """
        ...


class LogNotifier:
    """降级通知器：不真正外发，仅写日志（通知内容不丢失，便于排查）。"""

    def __init__(self, name: str = "notify") -> None:
        """初始化降级通知器，创建以 src.notify.<name> 命名的日志器。

        参数：
            name: str，日志器名称后缀，省略时默认为 "notify"

        返回：
            None，初始化实例并绑定日志器
        """
        self._logger = logging.getLogger(f"src.notify.{name}")

    async def send(self, text: str) -> bool:
        """把通知内容写入日志作为降级输出，不向外部通道发送。

        参数：
            text: str，待发送的通知文本

        返回：
            bool：固定返回 False，表示消息未真正送达外部通道（仅落日志）
        """
        self._logger.info("[通知降级输出] %s", text)
        return False
