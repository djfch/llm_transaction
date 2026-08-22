"""K 线停更判定与陈旧自愈看门狗测试（issue #74）。"""

import asyncio
import time
from collections import deque
from decimal import Decimal

from src.gateway.base import Candle
from src.market.candles import CandleCache, close_age, stale_text, stale_watchdog
from src.market.intervals import interval_seconds

BTC = "BTC_USDT"


def _cache(bars: dict[str, list[Candle]]) -> CandleCache:
    """构造注入了指定 K 线的缓存实例。

    参数：
        bars: dict[str, list[Candle]]，周期到 K 线列表的映射

    返回：
        CandleCache：已写入 K 线的缓存
    """
    cache = CandleCache.__new__(CandleCache)  # 跳过 source 注册，直接注入状态
    cache._gateway = None
    cache._maxlen = 2000
    cache._bars = {(BTC, iv): deque(bs, maxlen=2000) for iv, bs in bars.items()}
    return cache


def _candle(t: int) -> Candle:
    """构造一根指定开盘时间的测试 K 线。

    参数：
        t: int，开盘时间戳（秒）

    返回：
        Candle：OHLC 均为固定值的 K 线
    """
    return Candle(
        t=int(t), o=Decimal(100), h=Decimal(101), l=Decimal(99), c=Decimal(100), v=Decimal(1)
    )


def test_staleness_fresh_and_stale_and_empty():
    """验证 staleness 三态：新鲜、冻结超阈值、空键。

    参数：无

    返回：
        None，断言新鲜值小于阈值、冻结值大于阈值、无数据返回 None
    """
    span = interval_seconds("4h")
    now = 10_000_000
    fresh = _cache({"4h": [_candle(now - span - 60)]})  # 1 分钟前收盘
    frozen = _cache({"4h": [_candle(now - span - 5 * span)]})
    empty = _cache({})
    assert fresh.staleness(BTC, "4h", now) == 60
    assert frozen.staleness(BTC, "4h", now) == 5 * span
    assert empty.staleness(BTC, "4h", now) is None


def test_stale_keys_returns_only_stale():
    """验证 stale_keys 只返回停更键。

    参数：无

    返回：
        None，断言混合新鲜与冻结键时仅冻结键入选
    """
    now = int(time.time())
    cache = _cache(
        {
            "4h": [_candle(now - interval_seconds("4h") - 60)],  # 新鲜
            "1d": [_candle(now - interval_seconds("1d") - 5 * interval_seconds("1d"))],
        }
    )
    assert cache.stale_keys() == [(BTC, "1d")]


def test_close_age_and_stale_text():
    """验证 close_age 与 stale_text 辅助：空列表、未停更、已停更三态。

    参数：无

    返回：
        None，断言各形态返回值符合口径
    """
    span = interval_seconds("1h")
    now = int(time.time())
    fresh = [_candle(now - span - 60)]
    old = [_candle(now - span - 5 * span)]
    assert close_age([], "1h") is None
    assert close_age(fresh, "1h", now) == 60
    assert stale_text(fresh, "1h", now) is None
    assert stale_text(old, "1h", now) == "K线已停更 5.0h"


async def test_watchdog_backfills_stale_only():
    """验证看门狗只对停更键触发回补且数据续上后不再停更。

    参数：无

    返回：
        None，断言冻结键被回补为新鲜数据（缓存恢复）、新鲜键未被触碰
    """

    span = interval_seconds("1h")
    now = int(time.time())

    class _StubGateway:
        """记录 get_candlesticks 调用并返回新鲜单根 K 线的桩网关。"""

        def __init__(self):
            """初始化调用计数。参数：无。返回：None。"""
            self.calls: list[str] = []

        def get_candlesticks(self, contract: str, interval: str, limit: int | None = None):
            """返回一根新鲜 K 线并记录合约名。"""
            self.calls.append(contract)
            return [_candle(time.time() - span - 30)]

    gw = _StubGateway()
    cache = _cache(
        {
            "1h": [_candle(now - span - 5 * span)],  # 冻结
            "4h": [_candle(now - interval_seconds("4h") - 60)],  # 新鲜
        }
    )
    cache._gateway = gw
    task = asyncio.ensure_future(stale_watchdog(cache, poll_interval_s=0.01))
    try:
        for _ in range(300):
            await asyncio.sleep(0.01)
            if not cache.stale_keys():  # 回补完成（赋值在 executor 返回之后）
                break
        assert gw.calls == [BTC]  # 只有冻结的 1h 被回补
        assert cache.stale_keys() == []
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
