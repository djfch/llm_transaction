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
    """构造走 MockTransport 的客户端，并记录所有请求供断言。

    参数：
        handler: Callable[[httpx.Request], httpx.Response]，生成模拟 HTTP 响应的处理器
        requests: list[httpx.Request]，收集已发送请求的列表

    返回：
        httpx.AsyncClient，使用记录型 MockTransport 的异步客户端
    """

    def _record(request: httpx.Request) -> httpx.Response:
        """记录请求到列表后，转交真实处理器生成模拟响应。

        参数：
            request: httpx.Request，MockTransport 收到的待发送请求

        返回：
            httpx.Response：由传入 handler 生成的模拟响应
        """
        requests.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(_record))


# ---------- Telegram 发送 ----------


async def test_send_success():
    """校验发送成功时按 Telegram 协议组装请求并返回 True。

    参数：无

    返回：
        None，断言 send 返回 True、对象符合 Notifier 协议、请求路径为
        /bot<TOKEN>/sendMessage 且请求体含 chat_id、text 与 parse_mode=HTML
    """
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
    """校验 Telegram 返回 HTTP 500 时 send 降级返回 False。

    参数：无

    返回：
        None，断言 HTTP 层失败时 send 返回 False 而不抛异常
    """
    client = make_client(lambda req: httpx.Response(500, json={"ok": False}), [])
    notifier = TelegramNotifier(TOKEN, CHAT_ID, client=client)
    assert await notifier.send("x") is False
    await client.aclose()


async def test_send_api_not_ok_returns_false():
    """校验 HTTP 200 但响应体 ok=false 时 send 返回 False。

    参数：无

    返回：
        None，断言 Telegram 业务层报错（如 chat not found）时 send 返回 False
    """
    client = make_client(
        lambda req: httpx.Response(200, json={"ok": False, "description": "chat not found"}), []
    )
    notifier = TelegramNotifier(TOKEN, CHAT_ID, client=client)
    assert await notifier.send("x") is False
    await client.aclose()


async def test_send_network_error_returns_false():
    """校验网络异常时 send 不抛出而是降级返回 False。

    参数：无

    返回：
        None，断言 ConnectError 被内部吞掉，send 返回 False
    """

    def _boom(req: httpx.Request) -> httpx.Response:
        """模拟网络连接失败的请求处理器。

        参数：
            req: httpx.Request，触发异常时正在处理的请求

        返回：
            httpx.Response，实际不会返回（函数体内总是抛出异常）

        异常：
            httpx.ConnectError：模拟网络连接失败，用于验证降级路径
        """
        raise httpx.ConnectError("连接失败", request=req)

    client = make_client(_boom, [])
    notifier = TelegramNotifier(TOKEN, CHAT_ID, client=client)
    assert await notifier.send("x") is False  # 网络异常不抛出，仅降级返回 False
    await client.aclose()


# ---------- 降级路径 ----------


async def test_build_notifier_disabled():
    """校验配置关闭 Telegram 时构建出日志降级通知器。

    参数：无

    返回：
        None，断言 telegram_enabled=False 时 build_notifier 返回 LogNotifier
    """
    notifier = build_notifier(NotifyConfig(telegram_enabled=False))
    assert isinstance(notifier, LogNotifier)


async def test_build_notifier_missing_env(monkeypatch):
    """校验启用 Telegram 但环境变量全缺时降级为日志通知。

    参数：
        monkeypatch: pytest monkeypatch 夹具，用于删除 TELEGRAM_BOT_TOKEN
            与 TELEGRAM_CHAT_ID 环境变量

    返回：
        None，断言环境变量缺失时 build_notifier 返回 LogNotifier
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier = build_notifier(NotifyConfig(telegram_enabled=True))
    assert isinstance(notifier, LogNotifier)


async def test_build_notifier_partial_env(monkeypatch):
    """校验只有 bot token、缺少 chat_id 时同样降级为日志通知。

    参数：
        monkeypatch: pytest monkeypatch 夹具，设置 TELEGRAM_BOT_TOKEN
            并删除 TELEGRAM_CHAT_ID

    返回：
        None，断言环境变量不完整时 build_notifier 返回 LogNotifier
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)  # 只有 token 没有 chat_id 也降级
    notifier = build_notifier(NotifyConfig(telegram_enabled=True))
    assert isinstance(notifier, LogNotifier)


async def test_build_notifier_with_env(monkeypatch):
    """校验环境变量齐全且启用时构建出 Telegram 通知器。

    参数：
        monkeypatch: pytest monkeypatch 夹具，设置 TELEGRAM_BOT_TOKEN
            与 TELEGRAM_CHAT_ID 环境变量

    返回：
        None，断言 build_notifier 返回 TelegramNotifier
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    notifier = build_notifier(NotifyConfig(telegram_enabled=True))
    assert isinstance(notifier, TelegramNotifier)


async def test_log_notifier_send(caplog):
    """校验日志降级通知器发送时写入日志且返回 False。

    参数：
        caplog: pytest caplog 夹具，捕获 INFO 级日志用于断言

    返回：
        None，断言消息写入日志、降级发送不视为送达（send 返回 False）
    """
    notifier = LogNotifier("test")
    with caplog.at_level(logging.INFO):
        assert await notifier.send("持仓告警") is False  # 降级发送不视为送达
    assert "持仓告警" in caplog.text
