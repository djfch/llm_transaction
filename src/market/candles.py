"""K 线缓存与行情源抽象。

PriceSource 解耦行情来源：实盘接 MarketFeed（WS 推送），测试/回放用
ManualPriceSource 手动推送。CandleCache 只依赖 PriceSource 注册的回调，
不关心来源实现。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Protocol

from ..audit.logger import get_logger
from ..gateway.base import Candle, Gateway, Ticker
from .feed import CandleHandler, TickerHandler, maybe_await
from .intervals import interval_seconds

logger = get_logger(__name__)

# Gate REST candlesticks 单次最多返回 2000 根（当前 SDK docstring）
REST_CANDLE_LIMIT = 2000
# K 线停更判定：连续 STALE_MULTIPLIER 个周期没有等到新收盘 K 线即视为停更
# （issue #74：WS 断联后缓存冻结，消费方须按停更处理而非当新鲜数据使用）
STALE_MULTIPLIER = 2
# 看门狗巡检间隔（秒）
STALE_WATCHDOG_INTERVAL_S = 60


def close_age(candles: list[Candle], interval: str, now: float | None = None) -> float | None:
    """由 K 线列表计算最后收盘时刻距今的秒数；空列表返回 None。

    供无缓存实例的消费方（上下文摘要/指标文本/工具出口）直接判定停更，
    与 CandleCache.staleness 同口径（issue #74）。

    参数：
        candles: list[Candle]，按时间排序的 K 线列表（可为乱序，取最大 t）
        interval: str，K 线周期
        now: float | None，当前 Unix 时间戳（秒）；None 时取 time.time()

    返回：
        float | None：最后收盘时刻距今的秒数（不早于 0）；空列表返回 None
    """
    if not candles:
        return None
    now = time.time() if now is None else now
    try:
        span = interval_seconds(interval)
    except ValueError:
        span = 0
    return max(0.0, now - (max(c.t for c in candles) + span))


def stale_text(candles: list[Candle], interval: str, now: float | None = None) -> str | None:
    """K 线已停更时返回报错文案（含停更时长），未停更或无数据返回 None。

    判定口径：停更时长 > STALE_MULTIPLIER × 周期秒数（issue #74）。

    参数：
        candles: list[Candle]，K 线列表
        interval: str，K 线周期
        now: float | None，当前 Unix 时间戳（秒）；None 时取 time.time()

    返回：
        str | None：停更时返回「K线已停更 X.Xh」文案；否则 None
    """
    age = close_age(candles, interval, now)
    if age is None:
        return None
    try:
        span = interval_seconds(interval)
    except ValueError:
        span = 0
    if age <= STALE_MULTIPLIER * span:
        return None
    return f"K线已停更 {age / 3600:.1f}h"


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

    def stale_keys(self, threshold_multiplier: int = STALE_MULTIPLIER) -> list[tuple[str, str]]:
        """找出已停更的 (合约, 周期) 键：staleness 超过 倍数×周期 即认定停更。

        参数：
            threshold_multiplier: int，停更阈值倍数（issue #74）

        返回：
            list[tuple[str, str]]：停更键列表；无则空列表
        """
        stale: list[tuple[str, str]] = []
        for contract, interval in list(self._bars):
            threshold = threshold_multiplier * interval_seconds(interval)
            s = self.staleness(contract, interval)
            if s is not None and s > threshold:
                stale.append((contract, interval))
        return stale

    async def backfill_async(self, contracts: list[str], intervals: list[str]) -> None:
        """REST 回补历史（异步上下文版）：网关调用经统一卸载层执行。

        参数：
            contracts: list[str]，需要回补的合约列表
            intervals: list[str]，需要回补的周期列表

        返回：
            None，回补结果就地写入缓存
        """
        from ..gateway.async_io import run_gateway_io

        for contract in contracts:
            for interval in intervals:
                candles = await run_gateway_io(
                    self._gateway.get_candlesticks,
                    contract,
                    interval,
                    limit=min(self._maxlen, REST_CANDLE_LIMIT),
                )
                candles = sorted(candles, key=lambda c: c.t)
                self._bars[(contract, interval)] = deque(
                    candles[-self._maxlen :], maxlen=self._maxlen
                )

    def staleness(self, contract: str, interval: str, now: float | None = None) -> float | None:
        """最后一根 K 线的收盘时间距 now 的秒数；该键无数据返回 None。

        WS 断联后缓存冻结，此值持续增大——消费方据此判定停更（issue #74）。
        周期无法识别时退化为"当前时间 − 最后一根 K 线开盘时间"。

        参数：
            contract: str，合约名称
            interval: str，行情或统计周期
            now: float | None，当前 Unix 时间戳（秒）；None 时取 time.time()

        返回：
            float | None：最后收盘时刻距今的秒数（不早于 0）；键无数据返回 None
        """
        bars = self._bars.get((contract, interval))
        if not bars:
            return None
        now = time.time() if now is None else now
        try:
            span = interval_seconds(interval)
        except ValueError:
            span = 0
        return max(0.0, now - (bars[-1].t + span))


async def stale_watchdog(
    candles: CandleCache,
    *,
    threshold_multiplier: int = STALE_MULTIPLIER,
    poll_interval_s: float = STALE_WATCHDOG_INTERVAL_S,
) -> None:
    """陈旧自愈看门狗：发现停更的 (合约×周期) 即 REST 回补，失败等下轮。

    WS 断联后缓存冻结，指标与快照会基于过时数据工作（issue #74）；多数断线
    场景交易所 REST 可达，主动回补即可续上数据，无需降级。回补复用 backfill，
    网关调用经统一卸载层执行（不在事件循环内直接阻塞）。

    参数：
        candles: CandleCache，K 线缓存（其键集即巡检范围）
        threshold_multiplier: int，停更阈值倍数（staleness > 倍数×周期即回补）
        poll_interval_s: float，巡检间隔秒数

    返回：
        None，长期运行的任务协程；由调用方作为任务托管并随应用生命周期取消
    """
    while True:
        await asyncio.sleep(poll_interval_s)
        stale = candles.stale_keys(threshold_multiplier)
        for contract, interval in stale:
            logger.warning("K 线停更（%s %s），尝试 REST 回补", contract, interval)
            try:
                await candles.backfill_async([contract], [interval])
            except Exception:
                logger.warning("K 线回补失败（%s %s），等待下轮", contract, interval, exc_info=True)
