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
        """client 可注入（测试用 MockTransport）；为 None 时每次发送临时创建。"""
        self._token = token
        self._chat_id = chat_id
        self._client = client
        self._timeout = timeout

    async def send(self, text: str) -> bool:
        """发送消息；网络或 API 失败时记录日志并返回 False（不抛出，避免影响主流程）。"""
        url = f"{API_BASE}/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        if self._client is not None:
            return await self._post(self._client, url, payload)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._post(client, url, payload)

    async def _post(self, client: httpx.AsyncClient, url: str, payload: dict) -> bool:
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
    """按配置构建通知器：未启用或缺少 token/chat_id 时降级为 LogNotifier。"""
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
