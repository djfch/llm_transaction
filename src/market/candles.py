"""K 线缓存与行情源抽象。

PriceSource 解耦行情来源：实盘接 MarketFeed（WS 推送），测试/回放用
ManualPriceSource 手动推送。CandleCache 只依赖 PriceSource 注册的回调，
不关心来源实现。
"""

from __future__ import annotations

from collections import deque
from typing import Protocol

from ..audit.logger import get_logger
from ..gateway.base import Candle, Gateway, Ticker
from .feed import CandleHandler, TickerHandler, maybe_await

logger = get_logger(__name__)

# Gate REST candlesticks 单次最多返回 2000 根（当前 SDK docstring）
REST_CANDLE_LIMIT = 2000


class PriceSource(Protocol):
    """行情源接口：注册处理函数 + start/stop。"""

    def set_handlers(
        self,
        on_ticker: TickerHandler | None = None,
        on_candle: CandleHandler | None = None,
    ) -> None:
        """注册/替换 ticker 与 K 线处理函数，须在 start 前调用。

        参数：
            on_ticker: TickerHandler | None，ticker 行情处理函数，省略时不设置
            on_candle: CandleHandler | None，K 线行情处理函数，省略时不设置

        返回：
            None，仅登记回调，由行情源在收到行情时调用
        """
        ...

    async def start(self) -> None:
        """启动行情源，开始接收行情并回调已注册的处理函数。

        参数：无

        返回：
            None，启动后行情经 set_handlers 注册的回调分发
        """
        ...

    async def stop(self) -> None:
        """停止行情源，结束行情接收与回调分发。

        参数：无

        返回：
            None，停止后不再回调处理函数
        """
        ...


class ManualPriceSource:
    """手动推送的行情源：单元测试与历史回放用。"""

    def __init__(self) -> None:
        """初始化手动行情源，处理函数留空待 set_handlers 注册。

        参数：无

        返回：
            None，仅就地初始化内部字段
        """
        self._on_ticker: TickerHandler | None = None
        self._on_candle: CandleHandler | None = None

    def set_handlers(
        self,
        on_ticker: TickerHandler | None = None,
        on_candle: CandleHandler | None = None,
    ) -> None:
        """注册/替换处理函数，传 None 的参数保持已注册的处理函数不变。

        参数：
            on_ticker: TickerHandler | None，ticker 行情处理函数，省略时不替换
            on_candle: CandleHandler | None，K 线行情处理函数，省略时不替换

        返回：
            None，仅就地更新内部登记的处理函数
        """
        if on_ticker is not None:
            self._on_ticker = on_ticker
        if on_candle is not None:
            self._on_candle = on_candle

    async def start(self) -> None:
        """启动手动行情源（无实际连接，仅为 PriceSource 接口占位）。

        参数：无

        返回：
            None，无副作用；行情仅靠 push_ticker/push_candle 手动推入
        """
        pass

    async def stop(self) -> None:
        """停止手动行情源（无实际连接，仅为 PriceSource 接口占位）。

        参数：无

        返回：
            None，无副作用
        """
        pass

    async def push_ticker(self, ticker: Ticker) -> None:
        """手动推入一条 ticker 行情，模拟 WS 推送触发已注册的处理函数。

        参数：
            ticker: Ticker，要推入的合约 ticker 行情摘要

        返回：
            None，处理函数为协程时在此 await；未注册处理函数时直接丢弃
        """
        if self._on_ticker is not None:
            await maybe_await(self._on_ticker(ticker))

    async def push_candle(self, contract: str, interval: str, candle: Candle, closed: bool) -> None:
        """手动推入一根 K 线，模拟 WS 推送触发已注册的处理函数。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，K 线周期（如 1m、5m）
            candle: Candle，要推入的 K 线数据
            closed: bool，该 K 线是否已收盘（w=true 为收盘终值）

        返回：
            None，处理函数为协程时在此 await；未注册处理函数时直接丢弃
        """
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
        """初始化 K 线缓存，并向行情源注册 K 线处理函数。

        参数：
            gateway: Gateway，交易所网关，backfill 时经 REST 回补历史 K 线
            source: PriceSource，行情源，其 K 线推送驱动缓存滚动更新
            maxlen: int，每个 (contract, interval) 最多保留的 K 线根数，
                省略时为 REST 单次上限 2000 根

        返回：
            None，仅就地初始化内部字段并向 source 注册 on_candle 回调
        """
        self._gateway = gateway
        self._maxlen = maxlen
        self._bars: dict[tuple[str, str], deque[Candle]] = {}
        source.set_handlers(on_candle=self.on_candle)

    def backfill(self, contracts: list[str], intervals: list[str], limit: int = 200) -> None:
        """REST 补历史。只用 limit（与 from/to 互斥），limit≤2000。

        单个 (contract, interval) 失败只记 warning 并跳过：15 周期 × N 合约下
        任一周期被 REST 拒绝都不能拖垮其余周期（含 1h）的回补。

        参数：
            contracts: list[str]，需要处理的合约列表
            intervals: list[str]，需要补齐的 K 线周期列表
            limit: int，最多读取或返回的记录数量

        返回：
            None：REST 补历史。只用 limit（与 from/to 互斥），limit≤2000

        异常：
            ValueError：f'limit 不能超过 {REST_CANDLE_LIMIT}' 所描述的条件发生时
        """
        if limit > REST_CANDLE_LIMIT:
            raise ValueError(f"limit 不能超过 {REST_CANDLE_LIMIT}")
        for contract in contracts:
            for interval in intervals:
                try:
                    candles = self._gateway.get_candlesticks(contract, interval, limit=limit)
                except Exception:
                    logger.warning(
                        "K 线回补失败（%s %s），跳过该周期", contract, interval, exc_info=True
                    )
                    continue
                candles = sorted(candles, key=lambda c: c.t)
                self._bars[(contract, interval)] = deque(
                    candles[-self._maxlen :], maxlen=self._maxlen
                )

    def on_candle(self, contract: str, interval: str, candle: Candle, closed: bool) -> None:
        """WS 推送入口（注册为 PriceSource 的 K 线处理函数）。

        参数：
            contract: str，合约名称
            interval: str，行情或统计周期
            candle: Candle，收到的 K 线数据
            closed: bool，该 K 线是否已经收盘

        返回：
            None：WS 推送入口（注册为 PriceSource 的 K 线处理函数）
        """
        bars = self._bars.setdefault((contract, interval), deque(maxlen=self._maxlen))
        if not bars or candle.t > bars[-1].t:
            bars.append(candle)
        elif candle.t == bars[-1].t:
            bars[-1] = candle

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        """取最近 n 根（时间升序），供上下文组装用。

        参数：
            contract: str，合约名称
            interval: str，行情或统计周期
            n: int，需要读取的最近 K 线根数

        返回：
            list[Candle]：取最近 n 根（时间升序），供上下文组装用
        """
        bars = self._bars.get((contract, interval))
        if not bars:
            return []
        return list(bars)[-n:]
