"""成交回报对账装配（testnet/live）：PrivateTradeFeed + ExchangeFillSync 接线。

独立成文控制 bootstrap.py 行数。装配内容：
- 私有 WS 三频道（usertrades/autoorders/liquidates）推送 → 同步器落库
- 断线重连（on_reconnected）→ REST 补漏 catch_up；启动补漏由 run_app 显式调用
- 频道错误（on_error）→ Telegram 告警一次（不降级、不重复骚扰；权限问题需人工介入）

paper 模式不装配（模拟撮合由 FillPersister 落库，见 fill_persist.py）。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from src.agent.fill_sync import ExchangeFillSync, ExchangeRestSource
from src.config import Settings
from src.market.private_feed import PrivateTradeFeed
from src.memory.db import Database
from src.memory.fills_repo import ExchangeFillsRepo
from src.utils import maybe_await

AlertFn = Callable[[str], Awaitable[None] | None]


def build_trade_sync(
    settings: Settings,
    rest: ExchangeRestSource,
    db: Database,
    notify_event: Callable[[dict], None],
    send_alert: AlertFn,
) -> tuple[PrivateTradeFeed, ExchangeFillSync] | None:
    """装配私有成交订阅与同步器；paper 模式返回 None（调用方还需按 mock_market 跳过）。"""
    if settings.mode == "paper":
        return None
    feed = PrivateTradeFeed(
        settings.gate.settle,
        testnet=settings.mode == "testnet",
        api_key=os.environ.get("GATE_API_KEY", ""),
        api_secret=os.environ.get("GATE_API_SECRET", ""),
        ws_host=settings.gate.testnet_ws_host,
    )
    sync = ExchangeFillSync(ExchangeFillsRepo(db), rest, settings.mode, notify_event)
    alerted = False  # 错误告警只发一次：连续异常不骚扰（日志仍有全量记录）

    async def on_error(message: str) -> None:
        nonlocal alerted
        if alerted:
            return
        alerted = True
        await maybe_await(send_alert(f"私有成交频道异常，成交对账可能中断，请检查：{message}"))

    feed.set_handlers(
        on_user_trade=sync.handle_user_trade,
        on_auto_order=sync.handle_auto_order,
        on_liquidation=sync.handle_liquidation,
        on_reconnected=sync.catch_up,
        on_error=on_error,
    )
    return feed, sync
