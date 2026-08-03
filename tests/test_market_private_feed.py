"""PrivateTradeFeed 分发层测试：错误 ACK 可观测、非 update 跳过、单条异常隔离。

只测 _dispatch 纯逻辑（假 WebSocketResponse 替身）；start/stop 真实连接受 testnet 实测覆盖
（scripts/verify_private_feed.py），本文件不触网。
"""

from __future__ import annotations

import asyncio
import logging

from src.market.private_feed import PrivateTradeFeed


class _Response:
    """最小 WebSocketResponse 替身（本层只用到 error/event/result 三字段）。"""

    def __init__(self, result: list[dict], event: str = "update") -> None:
        self.error = None
        self.event = event
        self.result = result


def _feed() -> PrivateTradeFeed:
    return PrivateTradeFeed("usdt", testnet=True, api_key="k", api_secret="s")


async def test_error_ack_calls_on_error_not_handler(caplog):
    """订阅/推送 ACK 报错：记 warning 并回调 on_error，条目不送 handler。"""
    received: list[dict] = []
    errors: list[str] = []
    feed = _feed()
    feed.set_handlers(on_user_trade=received.append, on_error=errors.append)
    resp = _Response([{"id": 1}])
    resp.error = {"message": "permission denied"}  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING, logger="src.market.private_feed"):
        await feed._handle_user_trade(None, resp)  # type: ignore[arg-type]
    assert received == []
    assert errors == ["usertrades: {'message': 'permission denied'}"]
    assert any("ACK" in r.message for r in caplog.records)


async def test_non_update_event_skipped():
    """subscribe/unsubscribe 等 ACK 事件不送 handler。"""
    received: list[dict] = []
    feed = _feed()
    feed.set_handlers(on_user_trade=received.append)
    await feed._handle_user_trade(None, _Response([{"id": 1}], event="subscribe"))  # type: ignore[arg-type]
    assert received == []


async def test_update_entries_delivered_in_order():
    """update 批次逐条送达（支持异步 handler）。"""
    received: list[dict] = []

    async def _handler(raw: dict) -> None:
        received.append(raw)

    feed = _feed()
    feed.set_handlers(on_user_trade=_handler)
    await feed._handle_user_trade(None, _Response([{"id": 1}, {"id": 2}]))  # type: ignore[arg-type]
    assert received == [{"id": 1}, {"id": 2}]


async def test_handler_error_isolated_next_entry_delivered(caplog):
    """单条回调异常记日志并跳过，同批后续条目仍送达。"""
    received: list[dict] = []

    def _handler(raw: dict) -> None:
        if raw["id"] == 1:
            raise RuntimeError("回调故障")
        received.append(raw)

    feed = _feed()
    feed.set_handlers(on_user_trade=_handler)
    with caplog.at_level(logging.ERROR, logger="src.market.private_feed"):
        await feed._handle_user_trade(None, _Response([{"id": 1}, {"id": 2}]))  # type: ignore[arg-type]
    assert received == [{"id": 2}]
    assert any("回调异常" in r.message for r in caplog.records)


async def test_unregistered_channel_silently_skipped():
    """未注册 handler 的频道：批次直接跳过（订阅全部三频道但可只注册部分）。"""
    feed = _feed()  # 不注册任何 handler
    await feed._handle_auto_order(None, _Response([{"id": 1}]))  # type: ignore[arg-type]
    await feed._handle_liquidation(None, _Response([{"id": 1}]))  # type: ignore[arg-type]


async def test_start_passes_ws_host_to_configuration(monkeypatch):
    """testnet 时 ws_host 覆盖进 Configuration（SDK 内置 testnet 地址已失效）。"""
    captured: dict[str, str] = {}

    class _Conn:
        def __init__(self, cfg) -> None:
            captured["host"] = cfg.host

        def close(self) -> None:
            pass

        async def run(self) -> None:
            await asyncio.sleep(3600)

    class _Ch:
        def __init__(self, conn, callback) -> None:
            pass

        def subscribe(self, payload) -> None:
            pass

    monkeypatch.setattr("src.market.private_feed.Connection", _Conn)
    monkeypatch.setattr("src.market.private_feed.FuturesUserTradesChannel", _Ch)
    monkeypatch.setattr("src.market.private_feed.FuturesAutoOrdersChannel", _Ch)
    monkeypatch.setattr("src.market.private_feed.FuturesLiquidatesChannel", _Ch)
    feed = PrivateTradeFeed("usdt", testnet=True, api_key="k", api_secret="s", ws_host="wss://x")
    await feed.start()
    await feed.stop()
    assert captured["host"] == "wss://x"
