"""Gate 永续 WS 行情订阅封装（gatews 官方包），实盘 PriceSource 实现。

实测确认的 gatews 行为（scripts/check_feed.py 可复验，禁止猜测）：
- 回调签名 callback(conn, response)；协程回调由 gatews create_task 调度，
  同步回调会被扔进线程池（run_in_executor），故本层只用协程回调
- 订阅 ACK 与行情推送走同一频道名：event=="subscribe" 为 ACK，只处理 "update"
- ticker 订阅 payload 为合约名列表；K 线 payload 为 [interval, contract]
- K 线结果字段：t/o/h/l/c/v + n="{interval}_{contract}" + w（True=收盘）
- gatews 内部断线自动重连并回放订阅（sending_history），但退避是线性的，
  且放弃重连后 run() 直接返回；本层用指数退避重启 run()，保证长期在线
- 解析或业务回调异常由本层逐条捕获记日志，不外抛进 gatews 任务
  （协程回调由 gatews create_task 调度，异常外抛即 exception never retrieved）
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from decimal import Decimal
from typing import Awaitable, Callable

from gate_ws import Configuration, Connection, WebSocketResponse
from gate_ws.futures import FuturesCandlesticksChannel, FuturesTickerChannel

from ..gateway.base import Candle, Ticker

logger = logging.getLogger(__name__)

TickerHandler = Callable[[Ticker], Awaitable[None] | None]
# 参数：contract, interval, candle, closed（w=true 收盘）
CandleHandler = Callable[[str, str, Candle, bool], Awaitable[None] | None]

_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0


async def maybe_await(result: Awaitable[None] | None) -> None:
    """处理函数允许同步或协程，统一在此消化。"""
    if inspect.isawaitable(result):
        await result


def parse_ticker(raw: dict) -> Ticker:
    """解析 futures.tickers 推送条目（字段已实测）。"""
    return Ticker(
        contract=raw["contract"],
        last=Decimal(str(raw["last"])),
        mark_price=Decimal(str(raw["mark_price"])),
        funding_rate=Decimal(str(raw["funding_rate"])),
        high_24h=Decimal(str(raw["high_24h"])),
        low_24h=Decimal(str(raw["low_24h"])),
        change_percentage=Decimal(str(raw["change_percentage"])),
    )


def parse_ws_candle(raw: dict) -> tuple[str, str, Candle, bool]:
    """解析 futures.candlesticks 推送条目，返回 (contract, interval, candle, closed)。"""
    interval, contract = raw["n"].split("_", 1)
    candle = Candle(
        t=int(raw["t"]),
        o=Decimal(str(raw["o"])),
        h=Decimal(str(raw["h"])),
        l=Decimal(str(raw["l"])),  # noqa: E741
        c=Decimal(str(raw["c"])),
        v=Decimal(str(raw["v"])),
    )
    return contract, interval, candle, bool(raw.get("w"))


class MarketFeed:
    """白名单合约的 ticker + K 线 WS 订阅。start/stop 为 asyncio 接口。"""

    def __init__(
        self,
        contracts: list[str],
        intervals: list[str],
        settle: str = "usdt",
        testnet: bool = False,
        on_ticker: TickerHandler | None = None,
        on_candle: CandleHandler | None = None,
    ) -> None:
        self._contracts = list(contracts)
        self._intervals = list(intervals)
        self._settle = settle
        self._testnet = testnet
        self._on_ticker = on_ticker
        self._on_candle = on_candle
        self._conn: Connection | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    def set_handlers(
        self,
        on_ticker: TickerHandler | None = None,
        on_candle: CandleHandler | None = None,
    ) -> None:
        """注册/替换处理函数（PriceSource 接口，须在 start 前调用）。"""
        if on_ticker is not None:
            self._on_ticker = on_ticker
        if on_candle is not None:
            self._on_candle = on_candle

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Connection 须在运行中的事件循环内创建（gatews 取当前 loop）
        self._conn = Connection(
            Configuration(app="futures", settle=self._settle, test_net=self._testnet, max_retry=1)
        )
        ticker_ch = FuturesTickerChannel(self._conn, self._handle_ticker)
        candle_ch = FuturesCandlesticksChannel(self._conn, self._handle_candle)
        ticker_ch.subscribe(self._contracts)
        for interval in self._intervals:
            for contract in self._contracts:
                candle_ch.subscribe([interval, contract])
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._running = False
        if self._conn is not None:
            self._conn.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _supervise(self) -> None:
        """守护 conn.run()：gatews 放弃重连后（run 返回/抛错）指数退避重启。"""
        backoff = _INITIAL_BACKOFF
        while self._running:
            try:
                await self._conn.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("WS 连接异常退出")
            if not self._running:
                break
            logger.warning("WS %.0f 秒后重连（指数退避）", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _handle_ticker(self, conn: Connection, response: WebSocketResponse) -> None:
        if response.error or response.event != "update":
            if response.error:
                logger.warning("ticker 频道异常 ACK（订阅可能被拒）：%s", response.error)
            return
        for raw in response.result or []:
            try:
                ticker = parse_ticker(raw)
            except Exception:
                logger.exception("ticker 解析失败，已跳过：%s", raw)
                continue
            if self._on_ticker is not None:
                try:
                    await maybe_await(self._on_ticker(ticker))
                except Exception:
                    logger.exception("ticker 回调异常（%s）", ticker.contract)

    async def _handle_candle(self, conn: Connection, response: WebSocketResponse) -> None:
        if response.error or response.event != "update":
            if response.error:
                logger.warning("K 线频道异常 ACK（订阅可能被拒）：%s", response.error)
            return
        for raw in response.result or []:
            try:
                contract, interval, candle, closed = parse_ws_candle(raw)
            except Exception:
                logger.exception("K 线解析失败，已跳过：%s", raw)
                continue
            if self._on_candle is not None:
                try:
                    await maybe_await(self._on_candle(contract, interval, candle, closed))
                except Exception:
                    logger.exception("K 线回调异常（%s %s）", contract, interval)
