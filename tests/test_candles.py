"""CandleCache 单元测试：REST 补历史、WS 滚动更新、ManualPriceSource 接线。"""

from decimal import Decimal

import pytest
from src.gateway.base import Candle
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource

BTC = "BTC_USDT"


def make_candle(t: int, c: str = "100") -> Candle:
    return Candle(t=t, o=Decimal(c), h=Decimal(c), l=Decimal(c), c=Decimal(c), v=Decimal(1))


def make_cache(
    history: list | None = None, maxlen: int = 2000
) -> tuple[CandleCache, ManualPriceSource]:
    gw = MockGateway()
    gw.candles = history or []
    source = ManualPriceSource()
    return CandleCache(gw, source, maxlen=maxlen), source


def test_backfill_and_get_recent():
    history = [make_candle(t, str(100 + t)) for t in range(60, 601, 60)]  # 10 根升序
    cache, _ = make_cache(history)
    cache.backfill([BTC], ["1m"], limit=200)
    recent = cache.get_recent(BTC, "1m", 3)
    assert [c.t for c in recent] == [480, 540, 600]
    assert recent[-1].c == Decimal("700")


def test_backfill_rejects_limit_over_2000():
    cache, _ = make_cache()
    with pytest.raises(ValueError, match="2000"):
        cache.backfill([BTC], ["1m"], limit=2001)


def test_get_recent_empty():
    cache, _ = make_cache()
    assert cache.get_recent(BTC, "1m", 5) == []


async def test_ws_push_appends_new_bar():
    cache, source = make_cache()
    await source.push_candle(BTC, "1m", make_candle(60), closed=False)
    await source.push_candle(BTC, "1m", make_candle(120), closed=False)
    assert [c.t for c in cache.get_recent(BTC, "1m", 10)] == [60, 120]


async def test_ws_push_same_t_updates_current_bar():
    cache, source = make_cache()
    await source.push_candle(BTC, "1m", make_candle(60, "100"), closed=False)
    await source.push_candle(BTC, "1m", make_candle(60, "105"), closed=False)
    recent = cache.get_recent(BTC, "1m", 10)
    assert len(recent) == 1
    assert recent[0].c == Decimal("105")


async def test_close_rolls_to_next_bar():
    """w=true 收盘后，下一个推送开新根；旧根保持收盘终值。"""
    cache, source = make_cache()
    await source.push_candle(BTC, "1m", make_candle(60, "100"), closed=False)
    await source.push_candle(BTC, "1m", make_candle(60, "101"), closed=True)
    await source.push_candle(BTC, "1m", make_candle(120, "102"), closed=False)
    recent = cache.get_recent(BTC, "1m", 10)
    assert [c.t for c in recent] == [60, 120]
    assert recent[0].c == Decimal("101")  # 已完成 K 线保留收盘终值
    assert recent[1].c == Decimal("102")


async def test_out_of_order_push_ignored():
    cache, source = make_cache()
    await source.push_candle(BTC, "1m", make_candle(120, "102"), closed=False)
    await source.push_candle(BTC, "1m", make_candle(60, "100"), closed=False)
    recent = cache.get_recent(BTC, "1m", 10)
    assert [c.t for c in recent] == [120]


async def test_maxlen_drops_oldest():
    cache, source = make_cache(maxlen=3)
    for t in range(60, 301, 60):  # 5 根，超出容量
        await source.push_candle(BTC, "1m", make_candle(t), closed=False)
    assert [c.t for c in cache.get_recent(BTC, "1m", 10)] == [180, 240, 300]
