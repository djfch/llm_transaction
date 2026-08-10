"""Telegram Bot API 通知实现（httpx）。

token/chat_id 从环境变量 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 读取（只读环境变量，
不进配置文件、不进日志）；未启用或缺失时 build_notifier 降级为 LogNotifier。
"""

from __future__ import annotations

import logging
import os

import httpx

from src.config import NotifyConfig
from src.notify.base import LogNotifier, Notifier

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    """Telegram Bot 通知器：POST /bot<token>/sendMessage，parse_mode=HTML。"""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        """初始化 Telegram 通知器，并保存凭证、目标会话与可选复用客户端。

        参数：
            token: str，Telegram Bot 访问令牌
            chat_id: str，接收通知的 Telegram 会话标识
            client: httpx.AsyncClient | None，可复用的异步客户端；为空时发送期间临时创建
            timeout: float，临时客户端的请求超时秒数

        返回：
            None，仅保存发送消息所需配置
        """
        self._token = token
        self._chat_id = chat_id
        self._client = client
        self._timeout = timeout

    async def send(self, text: str) -> bool:
        """通过 Telegram Bot API 发送 HTML 文本，并把网络或接口失败降级为日志。

        参数：
            text: str，待发送的 HTML 通知文本

        返回：
            bool，接口确认消息发送成功时为 True，否则为 False
        """
        url = f"{API_BASE}/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        if self._client is not None:
            return await self._post(self._client, url, payload)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._post(client, url, payload)

    async def _post(self, client: httpx.AsyncClient, url: str, payload: dict) -> bool:
        """向 Telegram 发送一次消息请求并判断应答是否成功。

        参数：
            client: httpx.AsyncClient，发起请求所用的异步 HTTP 客户端
            url: str，sendMessage 接口的完整地址
            payload: dict，请求体（含 chat_id、text、parse_mode）

        返回：
            bool：HTTP 200 且应答中 ok 为 True 时返回 True；
            网络异常或 API 返回失败时记录告警日志并返回 False
        """
        try:
            resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("Telegram 发送网络异常：%s", exc)
            return False
        if resp.status_code == 200 and resp.json().get("ok") is True:
            return True
        logger.warning("Telegram 发送失败：HTTP %s %s", resp.status_code, resp.text[:200])
        return False


def build_notifier(config: NotifyConfig) -> Notifier:
    """按通知配置与环境变量构建 Telegram 通知器，不可用时退化为日志通知器。

    参数：
        config: NotifyConfig，Telegram 通知启用开关等配置

    返回：
        Notifier，可发送外部消息或仅记录日志的通知器实现
    """
    if not config.telegram_enabled:
        return LogNotifier("telegram(未启用)")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning(
            "telegram_enabled=true 但缺少 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID，降级为日志通知"
        )
        return LogNotifier("telegram(缺少环境变量)")
    return TelegramNotifier(token, chat_id)
