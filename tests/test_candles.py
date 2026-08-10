"""CandleCache 单元测试：REST 补历史、WS 滚动更新、ManualPriceSource 接线。"""

from decimal import Decimal

import pytest
from src.gateway.base import Candle
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource

BTC = "BTC_USDT"


def make_candle(t: int, c: str = "100") -> Candle:
    """构造一根开高低收价格相同、成交量固定为 1 的测试 K 线。

    参数：
        t: int，时间戳
        c: str，收盘价文本
    返回：
        Candle，返回该测试辅助函数构造或记录的结果
    """
    return Candle(t=t, o=Decimal(c), h=Decimal(c), l=Decimal(c), c=Decimal(c), v=Decimal(1))


def make_cache(
    history: list | None = None, maxlen: int = 2000
) -> tuple[CandleCache, ManualPriceSource]:
    """构造接入 MockGateway 的 CandleCache 及与其接线的手工价格源。

    参数：
        history: list | None，历史容量
        maxlen: int，缓存最大长度
    返回：
        tuple[CandleCache, ManualPriceSource]，返回该测试辅助函数构造或记录的结果
    """
    gw = MockGateway()
    gw.candles = history or []
    source = ManualPriceSource()
    return CandleCache(gw, source, maxlen=maxlen), source


def test_backfill_and_get_recent():
    """校验 REST 回补历史后 get_recent 返回最近 N 根升序 K 线。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    history = [make_candle(t, str(100 + t)) for t in range(60, 601, 60)]  # 10 根升序
    cache, _ = make_cache(history)
    cache.backfill([BTC], ["1m"], limit=200)
    recent = cache.get_recent(BTC, "1m", 3)
    assert [c.t for c in recent] == [480, 540, 600]
    assert recent[-1].c == Decimal("700")


def test_backfill_rejects_limit_over_2000():
    """校验回补 limit 超过 2000 时被拒绝。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    cache, _ = make_cache()
    with pytest.raises(ValueError, match="2000"):
        cache.backfill([BTC], ["1m"], limit=2001)


def test_get_recent_empty():
    """校验缓存为空时 get_recent 返回空列表而非报错。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    cache, _ = make_cache()
    assert cache.get_recent(BTC, "1m", 5) == []


async def test_ws_push_appends_new_bar():
    """校验 WS 推送不同时间戳的新 K 线时逐根追加到缓存。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    cache, source = make_cache()
    await source.push_candle(BTC, "1m", make_candle(60), closed=False)
    await source.push_candle(BTC, "1m", make_candle(120), closed=False)
    assert [c.t for c in cache.get_recent(BTC, "1m", 10)] == [60, 120]


async def test_ws_push_same_t_updates_current_bar():
    """校验同一时间戳的重复推送原地更新当前未收盘 K 线而非新增一根。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    cache, source = make_cache()
    await source.push_candle(BTC, "1m", make_candle(60, "100"), closed=False)
    await source.push_candle(BTC, "1m", make_candle(60, "105"), closed=False)
    recent = cache.get_recent(BTC, "1m", 10)
    assert len(recent) == 1
    assert recent[0].c == Decimal("105")


async def test_close_rolls_to_next_bar():
    """w=true 收盘后，下一个推送开新根；旧根保持收盘终值。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    cache, source = make_cache()
    await source.push_candle(BTC, "1m", make_candle(60, "100"), closed=False)
    await source.push_candle(BTC, "1m", make_candle(60, "101"), closed=True)
    await source.push_candle(BTC, "1m", make_candle(120, "102"), closed=False)
    recent = cache.get_recent(BTC, "1m", 10)
    assert [c.t for c in recent] == [60, 120]
    assert recent[0].c == Decimal("101")  # 已完成 K 线保留收盘终值
    assert recent[1].c == Decimal("102")


async def test_out_of_order_push_ignored():
    """校验时间戳早于当前根的乱序推送被忽略。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    cache, source = make_cache()
    await source.push_candle(BTC, "1m", make_candle(120, "102"), closed=False)
    await source.push_candle(BTC, "1m", make_candle(60, "100"), closed=False)
    recent = cache.get_recent(BTC, "1m", 10)
    assert [c.t for c in recent] == [120]


async def test_maxlen_drops_oldest():
    """校验推送根数超出缓存容量上限时最旧的 K 线被丢弃。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    cache, source = make_cache(maxlen=3)
    for t in range(60, 301, 60):  # 5 根，超出容量
        await source.push_candle(BTC, "1m", make_candle(t), closed=False)
    assert [c.t for c in cache.get_recent(BTC, "1m", 10)] == [180, 240, 300]
