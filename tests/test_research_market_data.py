"""研报逐标的市场快照：公开服务契约测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.gateway.base import Candle, Contract, OpenInterestPoint
from src.research.market_data import ResearchMarketDataService


class _CandleCache:
    def __init__(self, candles: dict[str, list[Candle]]) -> None:
        self._candles = candles

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        return self._candles.get(interval, [])[-n:]


class _Gateway:
    def __init__(self, oi: dict[str, list[OpenInterestPoint]]) -> None:
        self._oi = oi

    def get_contract(self, contract: str) -> Contract:
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
        return self._oi.get(interval, [])[-limit:]


def _candles(
    interval_seconds: int, *, rising: bool = False, weak_last: bool = False
) -> list[Candle]:
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
    end = 8_000_000
    return [
        OpenInterestPoint(time=end - interval_seconds, value=Decimal(before)),
        OpenInterestPoint(time=end, value=Decimal(current)),
    ]


@pytest.mark.asyncio
async def test_snapshot_uses_closed_history_and_returns_both_timeframes() -> None:
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
    service = ResearchMarketDataService(_CandleCache({}), _Gateway({}), now_fn=lambda: 8_000_001)

    result = await service.snapshot("BTC_USDT", limit=30)

    assert result["data_status"] == "不可用"
    assert result["timeframes"]["4h"]["oi_change_pct"] is None
    assert "4h: 无已收盘K线" in result["missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101])
async def test_snapshot_rejects_limit_outside_contract(limit: int) -> None:
    service = ResearchMarketDataService(_CandleCache({}), _Gateway({}))

    with pytest.raises(ValueError, match="limit 必须在 1-100"):
        await service.snapshot("BTC_USDT", limit=limit)
