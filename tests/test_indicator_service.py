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
        """注入预置 K 线数据，构造内存假 K 线缓存。

        参数：
            self: FakeCandleCache，当前测试替身实例
            bars: dict[tuple[str, str], list[Candle]] | None，按（合约，周期）保存的 K 线数据
        返回：
            None，初始化并保存测试替身状态
        """
        self._bars = bars or {}

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        """按真缓存语义返回该合约周期最近 n 根 K 线（时间升序）。

        参数：
            self: FakeCandleCache，当前测试替身实例
            contract: str，合约标识
            interval: str，K 线周期
            n: int，请求数量
        返回：
            list[Candle]，返回该测试辅助函数构造或记录的结果
        """
        return list(self._bars.get((contract, interval), []))[-n:]


class FakeOiCache:
    """内存 OI 缓存：与 OpenInterestCache.get 同签名。"""

    def __init__(self, values: dict[str, Decimal] | None = None) -> None:
        """注入预置持仓量数据，构造内存假 OI 缓存。

        参数：
            self: FakeOiCache，当前测试替身实例
            values: dict[str, Decimal] | None，按合约保存的持仓量
        返回：
            None，初始化并保存测试替身状态
        """
        self._values = values or {}

    def get(self, contract: str) -> Decimal | None:
        """返回该合约预置的持仓量。

        参数：
            self: FakeOiCache，当前测试替身实例
            contract: str，合约标识
        返回：
            Decimal | None，返回该测试辅助函数构造或记录的结果
        """
        return self._values.get(contract)


def make_candles(n: int, start: int = 1_700_000_000) -> list[Candle]:
    """n 根 1h K 线：收盘价单调上行（100 起），时间升序。

    参数：
        n: int，请求数量
        start: int，起始时间戳
    返回：
        list[Candle]，返回该测试辅助函数构造或记录的结果
    """
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
    """装配使用假缓存的 IndicatorService：BTC 1h 预置 n 根上行 K 线与固定持仓量。

    参数：
        n_candles: int，K 线数量
        oi: Decimal | None，可选持仓量
    返回：
        IndicatorService，返回该测试辅助函数构造或记录的结果
    """
    cache = FakeCandleCache({(BTC, INTERVAL): make_candles(n_candles)})
    oi_cache = FakeOiCache({BTC: oi} if oi is not None else {})
    return IndicatorService(cache, oi_cache)


def test_full_panel_shape_and_oi_from_cache():
    """校验完整面板形状：覆盖全部注册指标，OI 取自 OI 缓存而非 K 线。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
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
    """验证 K 线不足时完整指标面板会返回空指标值。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    panel = make_service(n_candles=10).full_panel(BTC, INTERVAL)
    indicators = panel["indicators"]
    # 10 根满足 ema9/rsi7/kdj/obv，不满足 ema50/macd/boll/vol_ratio/atr14/roc10
    assert indicators["ema9"]["values"]["ema9"] is not None
    assert indicators["rsi7"]["values"]["rsi7"] is not None
    for key in ("ema50", "macd", "boll", "vol_ratio", "atr14", "roc10"):
        assert all(v is None for v in indicators[key]["values"].values()), key


def test_full_panel_no_candles():
    """验证没有 K 线时完整指标面板会返回无数据结果。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    panel = make_service(n_candles=0).full_panel(BTC, INTERVAL)
    assert panel["time"] is None
    for key, entry in panel["indicators"].items():
        if key == "oi":
            assert entry["values"] == {"oi": "123456"}  # OI 不依赖 K 线
        else:
            assert all(v is None for v in entry["values"].values()), key


def test_series_time_alignment():
    """验证指标序列与 K 线时间戳保持对齐。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    candles = make_candles(60)
    out = make_service().series(BTC, INTERVAL, ["ema9", "rsi14"], limit=5)
    assert out["contract"] == BTC and out["interval"] == INTERVAL
    for key in ("ema9", "rsi14"):
        entry = out["series"][key]
        field = entry["fields"][key]
        assert [p["time"] for p in field] == [c.t for c in candles[-5:]]
        assert all(p["value"] is not None for p in field)


def test_series_short_history_leading_none():
    """验证历史不足时指标序列前部会使用 None 补齐。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    out = make_service(n_candles=12).series(BTC, INTERVAL, ["rsi7"], limit=12)
    points = out["series"]["rsi7"]["fields"]["rsi7"]
    assert len(points) == 12
    assert [p["value"] for p in points[:7]] == [None] * 7
    assert all(p["value"] is not None for p in points[7:])


def test_series_oi_gives_current_only():
    """验证持仓量指标序列只在当前时点提供数值。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    out = make_service().series(BTC, INTERVAL, ["oi"], limit=5)
    entry = out["series"]["oi"]
    assert entry["fields"] == {}
    assert entry["current"] == "123456"
    # 缓存无值时 current 为 None
    out2 = make_service(oi=None).series(BTC, INTERVAL, ["oi"], limit=5)
    assert out2["series"]["oi"]["current"] is None


def test_series_rejects_bad_args():
    """验证指标序列接口会拒绝非法参数。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    service = make_service()
    with pytest.raises(ValueError, match="limit"):
        service.series(BTC, INTERVAL, ["ema9"], 0)
    with pytest.raises(ValueError, match="未知指标"):
        service.series(BTC, INTERVAL, ["sma200"], 5)
    with pytest.raises(ValueError, match="未知指标"):
        service.shortlist_line(BTC, INTERVAL, ["sma200"])


def test_shortlist_line_no_candles():
    """验证没有 K 线时指标短名单文本会标记无数据。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    assert make_service(n_candles=0).shortlist_line(BTC, INTERVAL, ["ema20"]) == (
        f"{BTC} 指标({INTERVAL}): 无K线数据"
    )


def test_shortlist_line_format():
    """验证指标短名单文本符合约定格式。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    line = make_service().shortlist_line(BTC, INTERVAL, ["ema20", "rsi14", "macd", "oi"])
    assert line.startswith(f"{BTC} 指标({INTERVAL}): ")
    assert "EMA20=" in line
    assert "RSI14=" in line
    assert "MACD(dif/dea/hist)=" in line
    assert "持仓量=123456" in line


def test_shortlist_line_insufficient_marks_no_data():
    """验证历史不足的指标在短名单文本中会标记无数据。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    line = make_service(n_candles=5).shortlist_line(BTC, INTERVAL, ["ema20", "obv"])
    assert "EMA20=无数据" in line
    assert "OBV=无数据" not in line  # obv 只需 2 根，5 根有值


def test_shortlist_line_oi_missing_marks_no_data():
    """验证缺少持仓量时短名单文本会标记无数据。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    line = make_service(oi=None).shortlist_line(BTC, INTERVAL, ["oi"])
    assert "持仓量=无数据" in line


def test_history_limit_covers_registry():
    # 取数深度必须覆盖注册表最大 min_candles，否则长周期指标永远 None
    """验证历史读取上限覆盖指标注册表的最大窗口。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    assert HISTORY_LIMIT >= max(d.min_candles for d in REGISTRY.values())


def test_series_fetches_deep_enough_for_large_limit():
    """大窗口序列（15m×700）：取数深度 max(HISTORY_LIMIT, limit+60)，对齐窗口且前段暖机有值。

    参数：无
    返回：
        None，执行断言验证目标行为
    """

    class RecordingCache(FakeCandleCache):
        def __init__(self, bars):
            """初始化测试替身及其可观测状态。

            参数：
                self: RecordingCache，当前测试替身实例
                bars: dict[tuple[str, str], list[Candle]]，按（合约，周期）保存的 K 线数据
            返回：
                None，初始化并保存测试替身状态
            """
            super().__init__(bars)
            self.asked: list[int] = []

        def get_recent(self, contract, interval, n):
            """返回测试缓存中的最近 K 线。

            参数：
                self: RecordingCache，当前测试替身实例
                contract: str，合约标识
                interval: str，K 线周期
                n: int，请求数量
            返回：
                list[Candle]，返回该测试辅助函数构造或记录的结果
            """
            self.asked.append(n)
            return super().get_recent(contract, interval, n)

    cache = RecordingCache({(BTC, INTERVAL): make_candles(760)})
    service = IndicatorService(cache, FakeOiCache({BTC: Decimal(1)}))
    out = service.series(BTC, INTERVAL, ["ema50"], 700)
    assert cache.asked == [760]  # limit 700 + 暖机 60
    points = out["series"]["ema50"]["fields"]["ema50"]
    assert len(points) == 700  # 与最后 700 根 K 线时间对齐
    assert points[0]["value"] is not None  # 暖机余量覆盖 ema50，窗口首根即有值
