"""研报逐标的市场快照：公开服务契约测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.gateway.base import Candle, Contract, OpenInterestPoint
from src.research.market_data import ResearchMarketDataService


class _CandleCache:
    def __init__(self, candles: dict[str, list[Candle]]) -> None:
        """保存按周期分组的 K 线假数据。

        参数：
            candles: dict[str, list[Candle]]，键为周期（如 "4h"/"1d"），值为该周期的 K 线列表

        返回：
            None，副作用为初始化假缓存实例
        """
        self._candles = candles

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        """按周期返回末尾 n 根 K 线，模拟真实缓存的取数行为。

        参数：
            contract: str，合约名，假缓存忽略该参数
            interval: str，K 线周期，用作假数据字典的键
            n: int，需要的 K 线根数，从列表末尾截取

        返回：
            list[Candle]：该周期最后 n 根 K 线，无该周期数据时返回空列表
        """
        return self._candles.get(interval, [])[-n:]


class _Gateway:
    def __init__(self, oi: dict[str, list[OpenInterestPoint]]) -> None:
        """保存按周期分组的持仓量假数据。

        参数：
            oi: dict[str, list[OpenInterestPoint]]，键为周期，值为持仓量历史点列表

        返回：
            None，副作用为初始化假网关实例
        """
        self._oi = oi

    def get_contract(self, contract: str) -> Contract:
        """返回一份字段固定的合约元数据，仅名称使用入参。

        参数：
            contract: str，合约名，原样填入返回对象的 name 字段

        返回：
            Contract：资金费率 0.0001、标记价 100 等字段固定的假合约信息
        """
        return Contract(
            name=contract,
            quanto_multiplier=Decimal("0.0001"),
            order_size_min=1,
            order_size_max=1000,
            order_price_round=Decimal("0.1"),
            leverage_min=1,
            leverage_max=20,
            enable_decimal=False,
            mark_price=Decimal("100"),
            funding_rate=Decimal("0.0001"),
            funding_interval=28_800,
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.0005"),
            status="trading",
            in_delisting=False,
        )

    def fetch_open_interest_history(
        self, contract: str, interval: str, limit: int = 3
    ) -> list[OpenInterestPoint]:
        """按周期返回末尾 limit 条持仓量历史，模拟交易所接口取数。

        参数：
            contract: str，合约名，假网关忽略该参数
            interval: str，统计周期，用作假数据字典的键
            limit: int，返回条数上限，默认 3，从列表末尾截取

        返回：
            list[OpenInterestPoint]：该周期最后 limit 条持仓量点，无该周期数据时返回空列表
        """
        return self._oi.get(interval, [])[-limit:]


def _candles(
    interval_seconds: int, *, rising: bool = False, weak_last: bool = False
) -> list[Candle]:
    """构造 61 根 K 线假数据：60 根已收盘的正常 K 线加 1 根未收盘的极端 K 线。

    参数：
        interval_seconds: int，K 线周期秒数，用于倒推每根 K 线的时间戳
        rising: bool，为 True 时已收盘 K 线收盘价逐根递增（上涨趋势），默认 False 为横盘 100
        weak_last: bool，为 True 时最后一根已收盘 K 线成交量减半（缩量），默认 False

    返回：
        list[Candle]：末尾一根为未收盘极端 K 线（收盘 1000）的完整序列，
        用于验证指标只基于已收盘数据计算
    """
    rows: list[Candle] = []
    end = 8_000_000
    for i in range(60):
        close = Decimal(100 + i) if rising else Decimal("100")
        volume = Decimal("0.5") if weak_last and i == 59 else Decimal("1")
        rows.append(
            Candle(
                t=end - (60 - i) * interval_seconds,
                o=close,
                h=close + 1,
                l=close - 1,
                c=close,
                v=volume,
            )
        )
    # 最后一根尚未收盘且价格极端，若误入指标会明显污染 EMA/ATR。
    rows.append(
        Candle(
            t=end,
            o=Decimal("1000"),
            h=Decimal("1100"),
            l=Decimal("900"),
            c=Decimal("1000"),
            v=Decimal("100"),
        )
    )
    return rows


def _oi(interval_seconds: int, before: str, current: str) -> list[OpenInterestPoint]:
    """构造两个相邻的持仓量数据点，用于驱动 OI 变化率计算。

    参数：
        interval_seconds: int，统计周期秒数，决定两个点的时间间隔
        before: str，较早一点的持仓量数值字符串
        current: str，最新一点的持仓量数值字符串

    返回：
        list[OpenInterestPoint]：按时间先后排列的两个持仓量点
    """
    end = 8_000_000
    return [
        OpenInterestPoint(time=end - interval_seconds, value=Decimal(before)),
        OpenInterestPoint(time=end, value=Decimal(current)),
    ]


@pytest.mark.asyncio
async def test_snapshot_uses_closed_history_and_returns_both_timeframes() -> None:
    """校验快照同时返回 4h 与 1d 两个周期，且指标只基于已收盘 K 线计算。

    参数：无

    返回：
        None，断言数据状态为「完整」、末根 K 线标记为未收盘、
        各指标数值符合横盘假数据预期且 OI 变化率为 10%
    """
    cache = _CandleCache({"4h": _candles(14_400), "1d": _candles(86_400)})
    gateway = _Gateway({"4h": _oi(14_400, "100", "110"), "1d": _oi(86_400, "200", "220")})
    service = ResearchMarketDataService(cache, gateway, now_fn=lambda: 8_000_001)

    result = await service.snapshot("BTC_USDT", limit=5)

    assert result["contract"] == "BTC_USDT"
    assert result["funding_rate"] == "0.0001"
    assert result["data_status"] == "完整"
    assert set(result["timeframes"]) == {"4h", "1d"}
    four_hour = result["timeframes"]["4h"]
    assert len(four_hour["candles"]) == 5
    assert four_hour["candles"][-1]["closed"] is False
    assert four_hour["ema20"] == "100"
    assert four_hour["ema50"] == "100"
    assert four_hour["ema20_slope_pct_per_bar"] == "0"
    assert four_hour["atr14"] == "2"
    assert four_hour["volume_ratio"] == "1"
    assert four_hour["oi_current"] == "110"
    assert four_hour["oi_change_pct"] == "10"
    assert four_hour["divergence"]["price_trend"] == "震荡"
    assert four_hour["divergence"]["flags"] == []


@pytest.mark.asyncio
async def test_snapshot_sorts_unordered_candles_before_slicing_and_calculation() -> None:
    """乱序 K 线会先按时间排序，再截取窗口并计算指标。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    four_hour = _candles(14_400)
    daily = _candles(86_400)
    cache = _CandleCache({"4h": list(reversed(four_hour)), "1d": list(reversed(daily))})
    gateway = _Gateway({"4h": _oi(14_400, "100", "110"), "1d": _oi(86_400, "200", "220")})
    service = ResearchMarketDataService(cache, gateway, now_fn=lambda: 8_000_001)

    result = await service.snapshot("BTC_USDT", limit=5)

    candles = result["timeframes"]["4h"]["candles"]
    assert [item["time"] for item in candles] == sorted(item["time"] for item in candles)
    assert candles[-1]["closed"] is False
    assert result["timeframes"]["4h"]["ema20"] == "100"


@pytest.mark.asyncio
async def test_snapshot_is_partial_when_ema50_slope_cannot_be_calculated() -> None:
    """只有 50 根已收盘 K 线时保留 EMA50，但将斜率标为缺失并降级数据状态。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    four_hour = _candles(14_400)[-51:]
    daily = _candles(86_400)[-51:]
    service = ResearchMarketDataService(
        _CandleCache({"4h": four_hour, "1d": daily}),
        _Gateway({"4h": _oi(14_400, "100", "110"), "1d": _oi(86_400, "200", "220")}),
        now_fn=lambda: 8_000_001,
    )

    result = await service.snapshot("BTC_USDT")

    assert result["timeframes"]["4h"]["closed_candle_count"] == 50
    assert result["timeframes"]["4h"]["ema50"] == "100"
    assert result["timeframes"]["4h"]["ema50_slope_pct_per_bar"] is None
    assert result["data_status"] == "部分缺失"
    assert any("4h: 技术指标不完整" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_snapshot_explains_single_open_interest_point() -> None:
    """仅有一个持仓量数据点时保留当前值，并明确标注变化率不可用。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    single_oi = {
        "4h": [_oi(14_400, "100", "110")[-1]],
        "1d": [_oi(86_400, "200", "220")[-1]],
    }
    service = ResearchMarketDataService(
        _CandleCache({"4h": _candles(14_400), "1d": _candles(86_400)}),
        _Gateway(single_oi),
        now_fn=lambda: 8_000_001,
    )

    result = await service.snapshot("BTC_USDT")

    assert result["timeframes"]["4h"]["oi_current"] == "110"
    assert result["timeframes"]["4h"]["oi_change_pct"] is None
    assert result["data_status"] == "部分缺失"
    assert any("4h: OI变化率不可用" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_snapshot_marks_low_volume_rise_with_falling_oi() -> None:
    """价格上涨、成交量萎缩且持仓量下降时识别量价背离与空头回补风险。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    cache = _CandleCache(
        {"4h": _candles(14_400, rising=True, weak_last=True), "1d": _candles(86_400)}
    )
    gateway = _Gateway({"4h": _oi(14_400, "100", "99"), "1d": _oi(86_400, "100", "100")})
    service = ResearchMarketDataService(cache, gateway, now_fn=lambda: 8_000_001)

    result = await service.snapshot("BTC_USDT", limit=30)

    divergence = result["timeframes"]["4h"]["divergence"]
    assert divergence["price_trend"] == "上涨"
    assert divergence["volume_state"] == "缩量"
    assert divergence["oi_state"] == "减仓"
    assert divergence["flags"] == ["量价背离", "空头回补风险"]


@pytest.mark.asyncio
async def test_snapshot_degrades_without_market_history() -> None:
    """没有 K 线和持仓量历史时快照降级为不可用并列明缺失项。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    service = ResearchMarketDataService(_CandleCache({}), _Gateway({}), now_fn=lambda: 8_000_001)

    result = await service.snapshot("BTC_USDT", limit=30)

    assert result["data_status"] == "不可用"
    assert result["timeframes"]["4h"]["oi_change_pct"] is None
    assert "4h: 无已收盘K线" in result["missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101])
async def test_snapshot_rejects_limit_outside_contract(limit: int) -> None:
    """快照拒绝超出 1 至 100 范围的 K 线数量上限。

    参数：
        limit: int，待校验的数量上限

    返回：
        None：通过断言校验目标场景，无返回值
    """
    service = ResearchMarketDataService(_CandleCache({}), _Gateway({}))

    with pytest.raises(ValueError, match="limit 必须在 1-100"):
        await service.snapshot("BTC_USDT", limit=limit)
