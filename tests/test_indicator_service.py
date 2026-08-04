"""IndicatorService 测试：假 K 线缓存 + 假 OI 缓存装配，不触网不依赖真实网关。"""

from decimal import Decimal

import pytest

from src.gateway.base import Candle
from src.market.indicator_service import HISTORY_LIMIT, REGISTRY, IndicatorService

BTC = "BTC_USDT"
INTERVAL = "1h"


class FakeCandleCache:
    """内存 K 线缓存：与 CandleCache.get_recent 同签名（时间升序，取尾 n 根）。"""

    def __init__(self, bars: dict[tuple[str, str], list[Candle]] | None = None) -> None:
        self._bars = bars or {}

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        return list(self._bars.get((contract, interval), []))[-n:]


class FakeOiCache:
    """内存 OI 缓存：与 OpenInterestCache.get 同签名。"""

    def __init__(self, values: dict[str, Decimal] | None = None) -> None:
        self._values = values or {}

    def get(self, contract: str) -> Decimal | None:
        return self._values.get(contract)


def make_candles(n: int, start: int = 1_700_000_000) -> list[Candle]:
    """n 根 1h K 线：收盘价单调上行（100 起），时间升序。"""
    return [
        Candle(
            t=start + i * 3600,
            o=Decimal(100 + i),
            h=Decimal(101 + i),
            l=Decimal(99 + i),
            c=Decimal(100 + i),
            v=Decimal(10),
        )
        for i in range(n)
    ]


def make_service(n_candles: int = 60, oi: Decimal | None = Decimal("123456")) -> IndicatorService:
    cache = FakeCandleCache({(BTC, INTERVAL): make_candles(n_candles)})
    oi_cache = FakeOiCache({BTC: oi} if oi is not None else {})
    return IndicatorService(cache, oi_cache)


def test_full_panel_shape_and_oi_from_cache():
    panel = make_service().full_panel(BTC, INTERVAL)
    assert panel["contract"] == BTC
    assert panel["interval"] == INTERVAL
    assert panel["time"] == make_candles(60)[-1].t
    assert panel["shortlist"] is None
    assert set(panel["indicators"]) == set(REGISTRY)
    ema20 = panel["indicators"]["ema20"]
    assert ema20["label"] == "EMA20(指数均线)"
    assert ema20["kind"] == "overlay"
    assert ema20["values"]["ema20"] is not None
    assert panel["indicators"]["macd"]["values"].keys() == {"dif", "dea", "hist"}
    # OI 不来自 K 线，来自 OI 缓存
    assert panel["indicators"]["oi"]["values"] == {"oi": "123456"}


def test_full_panel_insufficient_candles_outputs_none():
    panel = make_service(n_candles=10).full_panel(BTC, INTERVAL)
    indicators = panel["indicators"]
    # 10 根满足 ema9/rsi7/kdj/obv，不满足 ema50/macd/boll/vol_ratio/atr14/roc10
    assert indicators["ema9"]["values"]["ema9"] is not None
    assert indicators["rsi7"]["values"]["rsi7"] is not None
    for key in ("ema50", "macd", "boll", "vol_ratio", "atr14", "roc10"):
        assert all(v is None for v in indicators[key]["values"].values()), key


def test_full_panel_no_candles():
    panel = make_service(n_candles=0).full_panel(BTC, INTERVAL)
    assert panel["time"] is None
    for key, entry in panel["indicators"].items():
        if key == "oi":
            assert entry["values"] == {"oi": "123456"}  # OI 不依赖 K 线
        else:
            assert all(v is None for v in entry["values"].values()), key


def test_series_time_alignment():
    candles = make_candles(60)
    out = make_service().series(BTC, INTERVAL, ["ema9", "rsi14"], limit=5)
    assert out["contract"] == BTC and out["interval"] == INTERVAL
    for key in ("ema9", "rsi14"):
        entry = out["series"][key]
        field = entry["fields"][key]
        assert [p["time"] for p in field] == [c.t for c in candles[-5:]]
        assert all(p["value"] is not None for p in field)


def test_series_short_history_leading_none():
    out = make_service(n_candles=12).series(BTC, INTERVAL, ["rsi7"], limit=12)
    points = out["series"]["rsi7"]["fields"]["rsi7"]
    assert len(points) == 12
    assert [p["value"] for p in points[:7]] == [None] * 7
    assert all(p["value"] is not None for p in points[7:])


def test_series_oi_gives_current_only():
    out = make_service().series(BTC, INTERVAL, ["oi"], limit=5)
    entry = out["series"]["oi"]
    assert entry["fields"] == {}
    assert entry["current"] == "123456"
    # 缓存无值时 current 为 None
    out2 = make_service(oi=None).series(BTC, INTERVAL, ["oi"], limit=5)
    assert out2["series"]["oi"]["current"] is None


def test_series_rejects_bad_args():
    service = make_service()
    with pytest.raises(ValueError, match="limit"):
        service.series(BTC, INTERVAL, ["ema9"], 0)
    with pytest.raises(ValueError, match="未知指标"):
        service.series(BTC, INTERVAL, ["sma200"], 5)
    with pytest.raises(ValueError, match="未知指标"):
        service.shortlist_line(BTC, INTERVAL, ["sma200"])


def test_shortlist_line_no_candles():
    assert make_service(n_candles=0).shortlist_line(BTC, INTERVAL, ["ema20"]) == (
        f"{BTC} 指标({INTERVAL}): 无K线数据"
    )


def test_shortlist_line_format():
    line = make_service().shortlist_line(BTC, INTERVAL, ["ema20", "rsi14", "macd", "oi"])
    assert line.startswith(f"{BTC} 指标({INTERVAL}): ")
    assert "EMA20=" in line
    assert "RSI14=" in line
    assert "MACD(dif/dea/hist)=" in line
    assert "持仓量=123456" in line


def test_shortlist_line_insufficient_marks_no_data():
    line = make_service(n_candles=5).shortlist_line(BTC, INTERVAL, ["ema20", "obv"])
    assert "EMA20=无数据" in line
    assert "OBV=无数据" not in line  # obv 只需 2 根，5 根有值


def test_shortlist_line_oi_missing_marks_no_data():
    line = make_service(oi=None).shortlist_line(BTC, INTERVAL, ["oi"])
    assert "持仓量=无数据" in line


def test_history_limit_covers_registry():
    # 取数深度必须覆盖注册表最大 min_candles，否则长周期指标永远 None
    assert HISTORY_LIMIT >= max(d.min_candles for d in REGISTRY.values())


def test_series_fetches_deep_enough_for_large_limit():
    """大窗口序列（15m×700）：取数深度 max(HISTORY_LIMIT, limit+60)，对齐窗口且前段暖机有值。"""

    class RecordingCache(FakeCandleCache):
        def __init__(self, bars):
            super().__init__(bars)
            self.asked: list[int] = []

        def get_recent(self, contract, interval, n):
            self.asked.append(n)
            return super().get_recent(contract, interval, n)

    cache = RecordingCache({(BTC, INTERVAL): make_candles(760)})
    service = IndicatorService(cache, FakeOiCache({BTC: Decimal(1)}))
    out = service.series(BTC, INTERVAL, ["ema50"], 700)
    assert cache.asked == [760]  # limit 700 + 暖机 60
    points = out["series"]["ema50"]["fields"]["ema50"]
    assert len(points) == 700  # 与最后 700 根 K 线时间对齐
    assert points[0]["value"] is not None  # 暖机余量覆盖 ema50，窗口首根即有值
