"""Telegram 通知与降级路径单元测试（httpx MockTransport，不真实外发）。"""

import json
import logging
from collections.abc import Callable

import httpx

from src.config import NotifyConfig
from src.notify import LogNotifier, Notifier, TelegramNotifier, build_notifier

TOKEN = "123456:ABC-test"
CHAT_ID = "12345"


def make_client(
    handler: Callable[[httpx.Request], httpx.Response], requests: list[httpx.Request]
) -> httpx.AsyncClient:
    """构造走 MockTransport 的客户端，并记录所有请求供断言。"""

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(_record))


# ---------- Telegram 发送 ----------


async def test_send_success():
    requests: list[httpx.Request] = []
    client = make_client(lambda req: httpx.Response(200, json={"ok": True, "result": {}}), requests)
    notifier = TelegramNotifier(TOKEN, CHAT_ID, client=client)
    assert await notifier.send("你好 <b>世界</b>") is True
    assert isinstance(notifier, Notifier)  # 符合 Notifier 协议
    req = requests[0]
    assert req.url.path == f"/bot{TOKEN}/sendMessage"
    assert json.loads(req.content) == {
        "chat_id": CHAT_ID,
        "text": "你好 <b>世界</b>",
        "parse_mode": "HTML",
    }
    await client.aclose()


async def test_send_http_error_returns_false():
    client = make_client(lambda req: httpx.Response(500, json={"ok": False}), [])
    notifier = TelegramNotifier(TOKEN, CHAT_ID, client=client)
    assert await notifier.send("x") is False
    await client.aclose()


async def test_send_api_not_ok_returns_false():
    client = make_client(
        lambda req: httpx.Response(200, json={"ok": False, "description": "chat not found"}), []
    )
    notifier = TelegramNotifier(TOKEN, CHAT_ID, client=client)
    assert await notifier.send("x") is False
    await client.aclose()


async def test_send_network_error_returns_false():
    def _boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连接失败", request=req)

    client = make_client(_boom, [])
    notifier = TelegramNotifier(TOKEN, CHAT_ID, client=client)
    assert await notifier.send("x") is False  # 网络异常不抛出，仅降级返回 False
    await client.aclose()


# ---------- 降级路径 ----------


async def test_build_notifier_disabled():
    notifier = build_notifier(NotifyConfig(telegram_enabled=False))
    assert isinstance(notifier, LogNotifier)


async def test_build_notifier_missing_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier = build_notifier(NotifyConfig(telegram_enabled=True))
    assert isinstance(notifier, LogNotifier)


async def test_build_notifier_partial_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)  # 只有 token 没有 chat_id 也降级
    notifier = build_notifier(NotifyConfig(telegram_enabled=True))
    assert isinstance(notifier, LogNotifier)


async def test_build_notifier_with_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    notifier = build_notifier(NotifyConfig(telegram_enabled=True))
    assert isinstance(notifier, TelegramNotifier)


async def test_log_notifier_send(caplog):
    notifier = LogNotifier("test")
    with caplog.at_level(logging.INFO):
        assert await notifier.send("持仓告警") is False  # 降级发送不视为送达
    assert "持仓告警" in caplog.text
