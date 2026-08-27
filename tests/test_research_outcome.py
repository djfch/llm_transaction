"""研报复盘客观结果计算测试：时长映射、窗口边界、数据状态四态。

K 线来源用内存桩（满足 CandleSource 结构协议），不触真实网关。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.gateway.base import Candle
from src.research.payload_v2 import HORIZON_SECONDS
from src.review.research_outcome import compute_outcome, outcome_from_candles

_BASE_TS = 1_700_000_000.0  # 固定窗口起点，避免依赖真实时间


def _candle(t: float, o: str, h: str, low: str, c: str) -> Candle:
    """构造一根测试 K 线（成交量恒 1）。

    参数：
        t: float，K 线时间戳（秒）
        o: str，开盘价
        h: str，最高价
        low: str，最低价
        c: str，收盘价

    返回：
        Candle：测试用 K 线
    """
    return Candle(t=int(t), o=Decimal(o), h=Decimal(h), l=Decimal(low), c=Decimal(c), v=Decimal(1))


def _window_candles(count: int, start_price: int = 100) -> list[Candle]:
    """构造 count 根连续 1h K 线：每小时一根，价格每小时 +1。

    参数：
        count: int，K 线根数
        start_price: int，首根开盘价

    返回：
        list[Candle]：从 _BASE_TS 起每小时一根的连续 K 线
    """
    return [
        _candle(
            _BASE_TS + i * 3600,
            str(start_price + i),
            str(start_price + i + 5),
            str(start_price + i - 5),
            str(start_price + i + 1),
        )
        for i in range(count)
    ]


class _StubCandleSource:
    """内存 K 线桩：满足 CandleSource 结构协议，记录调用参数。"""

    def __init__(self, candles: list[Candle], error: Exception | None = None) -> None:
        """保存固定返回的 K 线或待抛出的异常。

        参数：
            candles: list[Candle]，被调用时返回的 K 线列表
            error: Exception | None，非空时被调用即抛出（模拟网关故障）
        """
        self._candles = candles
        self._error = error
        self.calls: list[dict] = []

    def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """记录调用参数后返回固定 K 线（或抛出预设异常）。

        参数：
            contract: str，合约名
            interval: str，K 线周期
            limit: int | None，最近 N 根
            from_ts: int | None，起始时间戳
            to_ts: int | None，结束时间戳

        返回：
            list[Candle]：初始化时给定的 K 线列表

        异常：
            Exception：初始化时注入了 error 则原样抛出
        """
        self.calls.append(
            {"contract": contract, "interval": interval, "from_ts": from_ts, "to_ts": to_ts}
        )
        if self._error is not None:
            raise self._error
        return self._candles


def test_horizon_seconds_mapping() -> None:
    """horizon→秒数映射固定为当日 86400 / 3日 259200 / 周 604800。

    参数：无

    返回：
        None，断言三个枚举的秒数与期望 1h K 线根数
    """
    assert HORIZON_SECONDS == {"当日": 86400, "3日": 259200, "周": 604800}


def test_outcome_complete_window_metrics() -> None:
    """完整窗口：起价取首根开盘、止价取末根收盘，高低与三类百分比正确。

    参数：无

    返回：
        None，断言 data_status=complete 且各指标数值符合手工计算结果
    """
    candles = _window_candles(24)
    result = outcome_from_candles(candles, _BASE_TS, "当日")

    assert result["data_status"] == "complete"
    assert result["candles_expected"] == 24
    assert result["candles_actual"] == 24
    assert result["start_price"] == "100"
    assert result["end_price"] == "124"  # 第 24 根收盘 = 100 + 23 + 1
    assert result["high"] == "128"  # 第 24 根最高 = 100 + 23 + 5
    assert result["low"] == "95"  # 第 1 根最低 = 100 - 5
    assert Decimal(result["return_pct"]) == pytest.approx(Decimal(24))  # (124-100)/100
    assert Decimal(result["max_up_pct"]) == pytest.approx(Decimal(28))
    assert Decimal(result["max_down_pct"]) == pytest.approx(Decimal(-5))


def test_outcome_ignores_candles_outside_window() -> None:
    """窗口外 K 线（早于起点、晚于终点）不参与起止价与高低计算。

    参数：无

    返回：
        None，断言混入窗口外极端价格后结果与纯窗口内一致
    """
    inside = _window_candles(24)
    outside = [
        _candle(_BASE_TS - 3600, "1", "1", "1", "1"),  # 窗口前一根
        _candle(_BASE_TS + 86400, "9999", "9999", "9999", "9999"),  # 恰在窗口终点（不含）
        _candle(_BASE_TS + 90000, "9999", "9999", "9999", "9999"),  # 窗口后
    ]
    result = outcome_from_candles(inside + outside, _BASE_TS, "当日")

    assert result["data_status"] == "complete"
    assert result["candles_actual"] == 24
    assert result["start_price"] == "100"
    assert result["high"] == "128"


def test_outcome_partial_and_unavailable() -> None:
    """根数不足期望为 partial；窗口内零根为 unavailable 且价格字段全 None。

    参数：无

    返回：
        None，断言两种数据状态的判定与字段形状
    """
    partial = outcome_from_candles(_window_candles(10), _BASE_TS, "当日")
    assert partial["data_status"] == "partial"
    assert partial["candles_actual"] == 10
    assert partial["start_price"] == "100"

    empty = outcome_from_candles([], _BASE_TS, "周")
    assert empty["data_status"] == "unavailable"
    assert empty["candles_expected"] == 168
    assert empty["start_price"] is None
    assert empty["return_pct"] is None


def test_compute_outcome_pending_without_fetch() -> None:
    """窗口未到期返回 pending 且不发起 K 线拉取。

    参数：无

    返回：
        None，断言 data_status=pending 且 K 线桩无任何调用记录
    """
    source = _StubCandleSource(_window_candles(24))
    result = compute_outcome(
        "BTC_USDT",
        _BASE_TS,
        "当日",
        source,
        now=_BASE_TS + 3600,  # 窗口内第 1 小时
    )

    assert result["data_status"] == "pending"
    assert source.calls == []


def test_compute_outcome_invalid_horizon_and_fetch_error() -> None:
    """非法 horizon 与拉取异常均返回 unavailable 并附 error，不向调用方抛异常。

    参数：无

    返回：
        None，断言两种失败路径的 data_status 与 error 说明
    """
    bad_horizon = compute_outcome(
        "BTC_USDT", _BASE_TS, "24h", _StubCandleSource([]), now=_BASE_TS + 90000
    )
    assert bad_horizon["data_status"] == "unavailable"
    assert "24h" in bad_horizon["error"]

    failing = _StubCandleSource([], error=RuntimeError("网关超时"))
    failed = compute_outcome("BTC_USDT", _BASE_TS, "当日", failing, now=_BASE_TS + 90000)
    assert failed["data_status"] == "unavailable"
    assert "网关超时" in failed["error"]


def test_compute_outcome_fetches_1h_window_and_computes() -> None:
    """到期后按 from/to 拉取 1h K 线并委托纯函数计算。

    参数：无

    返回：
        None，断言拉取参数（1h 周期与窗口界）与计算结果状态
    """
    source = _StubCandleSource(_window_candles(72))
    result = compute_outcome("ETH_USDT", _BASE_TS, "3日", source, now=_BASE_TS + 300_000)

    assert source.calls == [
        {
            "contract": "ETH_USDT",
            "interval": "1h",
            "from_ts": int(_BASE_TS),
            "to_ts": int(_BASE_TS + 259200),
        }
    ]
    assert result["data_status"] == "complete"
    assert result["candles_actual"] == 72
