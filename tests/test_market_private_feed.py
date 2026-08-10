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
        """初始化替身响应：error 置空，推送条目与事件名写入实例字段。

        参数：
            self: _Response，当前测试替身实例
            result: list[dict]，工具执行结果文本
            event: str，推送事件名称
        返回：
            None，初始化并保存测试替身状态
        """
        self.error = None
        self.event = event
        self.result = result


def _feed() -> PrivateTradeFeed:
    """构造 testnet 配置的 PrivateTradeFeed 实例（仅内存对象，不建立真实连接）。

    参数：无
    返回：
        PrivateTradeFeed，返回该测试辅助函数构造或记录的结果
    """
    return PrivateTradeFeed("usdt", testnet=True, api_key="k", api_secret="s")


async def test_error_ack_calls_on_error_not_handler(caplog):
    """订阅/推送 ACK 报错：记 warning 并回调 on_error，条目不送 handler。

    参数：
        caplog: LogCaptureFixture，pytest 日志捕获夹具
    返回：
        None，执行断言验证目标行为
    """
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
    """subscribe/unsubscribe 等 ACK 事件不送 handler。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    received: list[dict] = []
    feed = _feed()
    feed.set_handlers(on_user_trade=received.append)
    await feed._handle_user_trade(None, _Response([{"id": 1}], event="subscribe"))  # type: ignore[arg-type]
    assert received == []


async def test_update_entries_delivered_in_order():
    """update 批次逐条送达（支持异步 handler）。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    received: list[dict] = []

    async def _handler(raw: dict) -> None:
        """记录私有成交订阅交付的原始事件。

        参数：
            raw: dict，原始订阅事件
        返回：
            None，返回该测试辅助函数构造或记录的结果
        """
        received.append(raw)

    feed = _feed()
    feed.set_handlers(on_user_trade=_handler)
    await feed._handle_user_trade(None, _Response([{"id": 1}, {"id": 2}]))  # type: ignore[arg-type]
    assert received == [{"id": 1}, {"id": 2}]


async def test_handler_error_isolated_next_entry_delivered(caplog):
    """单条回调异常记日志并跳过，同批后续条目仍送达。

    参数：
        caplog: LogCaptureFixture，pytest 日志捕获夹具
    返回：
        None，执行断言验证目标行为
    """
    received: list[dict] = []

    def _handler(raw: dict) -> None:
        """记录私有成交订阅交付的原始事件。

        参数：
            raw: dict，原始订阅事件
        返回：
            None，返回该测试辅助函数构造或记录的结果
        异常：
            RuntimeError: 测试场景主动触发该失败条件时抛出
        """
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
    """未注册 handler 的频道：批次直接跳过（订阅全部三频道但可只注册部分）。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    feed = _feed()  # 不注册任何 handler
    await feed._handle_auto_order(None, _Response([{"id": 1}]))  # type: ignore[arg-type]
    await feed._handle_liquidation(None, _Response([{"id": 1}]))  # type: ignore[arg-type]


async def test_start_passes_ws_host_to_configuration(monkeypatch):
    """testnet 时 ws_host 覆盖进 Configuration（SDK 内置 testnet 地址已失效）。

    参数：
        monkeypatch: MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
    captured: dict[str, str] = {}

    class _Conn:
        def __init__(self, cfg) -> None:
            """初始化测试替身及其可观测状态。

            参数：
                self: _Conn，当前测试替身实例
                cfg: object，WebSocket 配置
            返回：
                None，初始化并保存测试替身状态
            """
            captured["host"] = cfg.host

        def close(self) -> None:
            """关闭测试 WebSocket 连接。

            参数：
                self: _Conn，当前测试替身实例
            返回：
                None，返回该测试辅助函数构造或记录的结果
            """
            pass

        async def run(self) -> None:
            """模拟测试 WebSocket 连接进入运行状态。

            参数：
                self: _Conn，当前测试替身实例
            返回：
                None，返回该测试辅助函数构造或记录的结果
            """
            await asyncio.sleep(3600)

    class _Ch:
        def __init__(self, conn, callback) -> None:
            """初始化测试替身及其可观测状态。

            参数：
                self: _Ch，当前测试替身实例
                conn: object，WebSocket 连接替身
                callback: Callable，订阅回调
            返回：
                None，初始化并保存测试替身状态
            """
            pass

        def subscribe(self, payload) -> None:
            """记录测试 WebSocket 的订阅请求。

            参数：
                self: _Ch，当前测试替身实例
                payload: dict，订阅请求载荷
            返回：
                None，返回该测试辅助函数构造或记录的结果
            """
            pass

    monkeypatch.setattr("src.market.private_feed.Connection", _Conn)
    monkeypatch.setattr("src.market.private_feed.FuturesUserTradesChannel", _Ch)
    monkeypatch.setattr("src.market.private_feed.FuturesAutoOrdersChannel", _Ch)
    monkeypatch.setattr("src.market.private_feed.FuturesLiquidatesChannel", _Ch)
    feed = PrivateTradeFeed("usdt", testnet=True, api_key="k", api_secret="s", ws_host="wss://x")
    await feed.start()
    await feed.stop()
    assert captured["host"] == "wss://x"
