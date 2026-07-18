"""K 线缓存与行情源抽象。

PriceSource 解耦行情来源：实盘接 MarketFeed（WS 推送），测试/回放用
ManualPriceSource 手动推送。CandleCache 只依赖 PriceSource 注册的回调，
不关心来源实现。
"""

from __future__ import annotations

from collections import deque
from typing import Protocol

from ..gateway.base import Candle, Gateway, Ticker
from .feed import CandleHandler, TickerHandler, maybe_await

# Gate REST candlesticks 单次上限（实现计划附录，已核实）
REST_CANDLE_LIMIT = 2000


class PriceSource(Protocol):
    """行情源接口：注册处理函数 + start/stop。"""

    def set_handlers(
        self,
        on_ticker: TickerHandler | None = None,
        on_candle: CandleHandler | None = None,
    ) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class ManualPriceSource:
    """手动推送的行情源：单元测试与历史回放用。"""

    def __init__(self) -> None:
        self._on_ticker: TickerHandler | None = None
        self._on_candle: CandleHandler | None = None

    def set_handlers(
        self,
        on_ticker: TickerHandler | None = None,
        on_candle: CandleHandler | None = None,
    ) -> None:
        if on_ticker is not None:
            self._on_ticker = on_ticker
        if on_candle is not None:
            self._on_candle = on_candle

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def push_ticker(self, ticker: Ticker) -> None:
        if self._on_ticker is not None:
            await maybe_await(self._on_ticker(ticker))

    async def push_candle(self, contract: str, interval: str, candle: Candle, closed: bool) -> None:
        if self._on_candle is not None:
            await maybe_await(self._on_candle(contract, interval, candle, closed))


class CandleCache:
    """按 (contract, interval) 缓存 K 线：REST 补历史 + WS 滚动更新。

    滚动语义：同一开盘时间 t 的推送视为当前 K 线的更新（w=true 为收盘终值）；
    t 大于最后一根即新 K 线，append 后旧根自然固定——因此收盘滚动无需特判。
    早于最后一根的乱序推送直接忽略。
    """

    def __init__(
        self, gateway: Gateway, source: PriceSource, maxlen: int = REST_CANDLE_LIMIT
    ) -> None:
        self._gateway = gateway
        self._maxlen = maxlen
        self._bars: dict[tuple[str, str], deque[Candle]] = {}
        source.set_handlers(on_candle=self.on_candle)

    def backfill(self, contracts: list[str], intervals: list[str], limit: int = 200) -> None:
        """REST 补历史。只用 limit（与 from/to 互斥），limit≤2000。"""
        if limit > REST_CANDLE_LIMIT:
            raise ValueError(f"limit 不能超过 {REST_CANDLE_LIMIT}")
        for contract in contracts:
            for interval in intervals:
                candles = self._gateway.get_candlesticks(contract, interval, limit=limit)
                candles = sorted(candles, key=lambda c: c.t)
                self._bars[(contract, interval)] = deque(
                    candles[-self._maxlen :], maxlen=self._maxlen
                )

    def on_candle(self, contract: str, interval: str, candle: Candle, closed: bool) -> None:
        """WS 推送入口（注册为 PriceSource 的 K 线处理函数）。"""
        bars = self._bars.setdefault((contract, interval), deque(maxlen=self._maxlen))
        if not bars or candle.t > bars[-1].t:
            bars.append(candle)
        elif candle.t == bars[-1].t:
            bars[-1] = candle

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        """取最近 n 根（时间升序），供上下文组装用。"""
        bars = self._bars.get((contract, interval))
        if not bars:
            return []
        return list(bars)[-n:]
