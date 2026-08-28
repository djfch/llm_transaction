"""研报复盘客观结果计算测试：时长映射、15m 窗口边界纪律、数据状态四态。

K 线来源用内存桩（满足 AsyncCandleSource 异步结构协议），不触真实网关；
GatewayAsyncCandleSource 适配器用假同步网关验证窗口透传、超宽分段拼合
与执行线程卸载。
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from src.gateway.base import Candle
from src.research.payload_v2 import HORIZON_SECONDS
from src.review.research_outcome import (
    GatewayAsyncCandleSource,
    compute_outcome,
    outcome_from_candles,
)

_BASE_TS = 1_700_000_100.0  # 固定窗口起点（900 的整数倍，与 15m K 线对齐）


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
    """构造 count 根连续 15m K 线：每 900 秒一根，价格每根 +1。

    参数：
        count: int，K 线根数
        start_price: int，首根开盘价

    返回：
        list[Candle]：从 _BASE_TS 起每 900 秒一根的连续 K 线
    """
    return [
        _candle(
            _BASE_TS + i * 900,
            str(start_price + i),
            str(start_price + i + 5),
            str(start_price + i - 5),
            str(start_price + i + 1),
        )
        for i in range(count)
    ]


class _StubCandleSource:
    """内存 K 线桩：满足 AsyncCandleSource 异步结构协议，记录调用参数。"""

    def __init__(self, candles: list[Candle], error: Exception | None = None) -> None:
        """保存固定返回的 K 线或待抛出的异常。

        参数：
            candles: list[Candle]，被调用时返回的 K 线列表
            error: Exception | None，非空时被调用即抛出（模拟网关故障）
        """
        self._candles = candles
        self._error = error
        self.calls: list[dict] = []

    async def get_candlesticks(
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


class _FakeSyncGateway:
    """同步网关替身：记录调用参数与执行线程 id，按 from/to 生成含尾 K 线。

    不含 __gateway_io_inline__ 标记，因此经 GatewayAsyncCandleSource 调用时
    必然走 run_gateway_io 的单线程 executor（线程卸载断言的依据）。from/to
    查询返回 [from_ts, to_ts] 闭区间每 900 秒一根——段边界端点在相邻两段中
    重复出现，用于验证拼合去重。
    """

    def __init__(self) -> None:
        """初始化空调用记录与线程记录。

        参数：无

        返回：
            None，仅初始化实例属性
        """
        self.calls: list[dict] = []
        self.thread_ids: list[int] = []

    def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """记录参数与线程后按窗口生成 K 线（limit 查询返回单根占位）。

        参数：
            contract: str，合约名（仅对齐接口签名）
            interval: str，K 线周期
            limit: int | None，最近 N 根
            from_ts: int | None，窗口起点（含）
            to_ts: int | None，窗口终点（含，模拟网关含尾语义）

        返回：
            list[Candle]：limit 查询为单根占位；窗口查询为闭区间内每 900 秒一根
        """
        self.calls.append(
            {"interval": interval, "limit": limit, "from_ts": from_ts, "to_ts": to_ts}
        )
        self.thread_ids.append(threading.get_ident())
        if from_ts is None or to_ts is None:
            return [_candle(1, "1", "1", "1", "1")]
        return [_candle(t, "1", "2", "0", "1") for t in range(from_ts, to_ts + 1, 900)]


def test_horizon_seconds_mapping() -> None:
    """horizon→秒数映射固定为当日 86400 / 3日 259200 / 周 604800。

    参数：无

    返回：
        None，断言三个枚举的秒数映射
    """
    assert HORIZON_SECONDS == {"当日": 86400, "3日": 259200, "周": 604800}


def test_outcome_complete_window_metrics() -> None:
    """完整窗口（当日 96 根 15m）：起价取首根开盘、止价取末根收盘，高低与三类百分比正确。

    参数：无

    返回：
        None，断言 data_status=complete 且各指标数值符合手工计算结果
    """
    candles = _window_candles(96)
    result = outcome_from_candles(candles, _BASE_TS, "当日")

    assert result["data_status"] == "complete"
    assert result["candles_expected"] == 96
    assert result["candles_actual"] == 96
    assert result["start_price"] == "100"
    assert result["end_price"] == "196"  # 第 96 根收盘 = 100 + 95 + 1
    assert result["high"] == "200"  # 第 96 根最高 = 100 + 95 + 5
    assert result["low"] == "95"  # 第 1 根最低 = 100 - 5
    assert Decimal(result["return_pct"]) == pytest.approx(Decimal(96))  # (196-100)/100
    assert Decimal(result["max_up_pct"]) == pytest.approx(Decimal(100))
    assert Decimal(result["max_down_pct"]) == pytest.approx(Decimal(-5))


def test_outcome_ignores_candles_outside_window() -> None:
    """与窗口不相交的 K 线（尾恰在起点、头恰在终点）不参与起止价与高低计算。

    参数：无

    返回：
        None，断言混入窗口外极端价格后结果与纯窗口内一致
    """
    inside = _window_candles(96)
    outside = [
        _candle(_BASE_TS - 900, "1", "1", "1", "1"),  # 尾 = 窗口起点，不相交
        _candle(_BASE_TS + 86400, "9999", "9999", "9999", "9999"),  # 头 = 窗口终点，不相交
        _candle(_BASE_TS + 90000, "9999", "9999", "9999", "9999"),  # 窗口后
    ]
    result = outcome_from_candles(inside + outside, _BASE_TS, "当日")

    assert result["data_status"] == "complete"
    assert result["candles_actual"] == 96
    assert result["start_price"] == "100"
    assert result["high"] == "200"


def test_outcome_unaligned_created_at_uses_intersecting_bounds() -> None:
    """非整点 created_at：相交 K 线参与起价与高低，止价只取完整落在窗口内的末根。

    created_at 落在第 1 根 K 线中段：该根相交但不完整（参与起价/高低、不作
    止价）；窗口终点切断最后一根（参与高低、不作止价），止价取倒数第二根
    （最后一根完整 K 线）收盘。

    参数：无

    返回：
        None，断言起价/止价/高低的来源根与 complete 判定
    """
    created_at = _BASE_TS + 300  # 窗口起点在第 1 根 K 线中段
    candles = _window_candles(97)  # i=0..96：首末根均只与窗口相交
    result = outcome_from_candles(candles, created_at, "当日")

    assert result["candles_expected"] == 96
    assert result["candles_actual"] == 97  # 首末相交根都计入
    assert result["start_price"] == "100"  # 第 1 根（相交不完整）开盘价
    assert result["end_price"] == "196"  # i=95（最后一根完整 K 线）收盘
    assert result["high"] == "201"  # i=96（相交不完整）最高仍参与
    assert result["low"] == "95"
    assert result["data_status"] == "complete"  # 根数足且有完整止价
    assert Decimal(result["return_pct"]) == pytest.approx(Decimal(96))


def test_outcome_partial_when_no_complete_end_candle() -> None:
    """窗口内只有相交但不完整的 K 线：止价/涨跌幅缺失，partial 并附说明。

    参数：无

    返回：
        None，断言 end_price/return_pct 为 None、error 说明止价缺失
    """
    created_at = _BASE_TS + 300  # 第 1 根 K 线中段的窗口起点
    result = outcome_from_candles([_window_candles(1)[0]], created_at, "当日")

    assert result["data_status"] == "partial"
    assert result["candles_actual"] == 1
    assert result["start_price"] == "100"  # 相交根仍给出起价与区间高低
    assert result["high"] == "105"
    assert result["low"] == "95"
    assert result["end_price"] is None
    assert result["return_pct"] is None
    assert "止价缺失" in result["error"]


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
    assert empty["candles_expected"] == 672  # 604800 / 900
    assert empty["start_price"] is None
    assert empty["return_pct"] is None


async def test_compute_outcome_pending_without_fetch() -> None:
    """窗口未到期返回 pending 且不发起 K 线拉取。

    参数：无

    返回：
        None，断言 data_status=pending 且 K 线桩无任何调用记录
    """
    source = _StubCandleSource(_window_candles(96))
    result = await compute_outcome(
        "BTC_USDT",
        _BASE_TS,
        "当日",
        source,
        now=_BASE_TS + 3600,  # 窗口内第 1 小时
    )

    assert result["data_status"] == "pending"
    assert source.calls == []


async def test_compute_outcome_invalid_horizon_and_fetch_error() -> None:
    """非法 horizon 与拉取异常均返回 unavailable 并附 error，不向调用方抛异常。

    参数：无

    返回：
        None，断言两种失败路径的 data_status 与 error 说明
    """
    bad_horizon = await compute_outcome(
        "BTC_USDT", _BASE_TS, "24h", _StubCandleSource([]), now=_BASE_TS + 90000
    )
    assert bad_horizon["data_status"] == "unavailable"
    assert "24h" in bad_horizon["error"]

    failing = _StubCandleSource([], error=RuntimeError("网关超时"))
    failed = await compute_outcome("BTC_USDT", _BASE_TS, "当日", failing, now=_BASE_TS + 90000)
    assert failed["data_status"] == "unavailable"
    assert "网关超时" in failed["error"]


async def test_compute_outcome_fetches_15m_window_and_computes() -> None:
    """到期后按 from/to 拉取 15m K 线并委托纯函数计算。

    参数：无

    返回：
        None，断言拉取参数（15m 周期与窗口界）与计算结果状态
    """
    source = _StubCandleSource(_window_candles(288))
    result = await compute_outcome("ETH_USDT", _BASE_TS, "3日", source, now=_BASE_TS + 300_000)

    assert source.calls == [
        {
            "contract": "ETH_USDT",
            "interval": "15m",
            "from_ts": int(_BASE_TS),
            "to_ts": int(_BASE_TS + 259200),
        }
    ]
    assert result["data_status"] == "complete"
    assert result["candles_expected"] == 288  # 259200 / 900
    assert result["candles_actual"] == 288


async def test_adapter_limit_passthrough() -> None:
    """纯 limit 查询直通底层（不拼窗口、不分段）。

    参数：无

    返回：
        None，断言底层收到原样 limit 参数且结果直通
    """
    gateway = _FakeSyncGateway()
    source = GatewayAsyncCandleSource(gateway)
    got = await source.get_candlesticks("BTC_USDT", interval="15m", limit=10)
    assert [c.t for c in got] == [1]
    assert gateway.calls == [{"interval": "15m", "limit": 10, "from_ts": None, "to_ts": None}]


async def test_adapter_window_single_segment_passthrough() -> None:
    """窗口跨距不超单段上限：from/to 原样透传底层，一次调用完成。

    参数：无

    返回：
        None，断言底层恰好收到一次 from/to 透传调用
    """
    gateway = _FakeSyncGateway()
    source = GatewayAsyncCandleSource(gateway)
    got = await source.get_candlesticks("BTC_USDT", interval="15m", from_ts=1000, to_ts=2000)
    assert gateway.calls == [{"interval": "15m", "limit": None, "from_ts": 1000, "to_ts": 2000}]
    assert [c.t for c in got] == [1000, 1900]  # 假网关含尾：每 900 秒一根


async def test_adapter_paginates_wide_window_and_dedupes() -> None:
    """超宽窗口按 page_limit×周期秒数分段拉取，段间重复端点按时间戳去重升序拼合。

    参数：无

    返回：
        None，断言分段调用参数序列与拼合结果（升序、无重复）
    """
    gateway = _FakeSyncGateway()
    source = GatewayAsyncCandleSource(gateway, page_limit=2)  # 单段 2×900=1800 秒
    got = await source.get_candlesticks("BTC_USDT", interval="15m", from_ts=0, to_ts=5400)

    assert gateway.calls == [
        {"interval": "15m", "limit": None, "from_ts": 0, "to_ts": 1800},
        {"interval": "15m", "limit": None, "from_ts": 1800, "to_ts": 3600},
        {"interval": "15m", "limit": None, "from_ts": 3600, "to_ts": 5400},
    ]
    # 含尾语义下段边界 t=1800/3600 在相邻段重复，拼合后升序且各出现一次
    assert [c.t for c in got] == [0, 900, 1800, 2700, 3600, 4500, 5400]


async def test_adapter_offloads_sync_gateway_off_event_loop() -> None:
    """底层同步网关调用经 run_gateway_io 卸载到 executor 线程，不占事件循环线程。

    参数：无

    返回：
        None，断言假网关记录的执行线程 id 与事件循环线程不同
    """
    gateway = _FakeSyncGateway()
    source = GatewayAsyncCandleSource(gateway)
    loop_ident = threading.get_ident()

    await source.get_candlesticks("BTC_USDT", interval="15m", from_ts=1000, to_ts=2000)

    assert gateway.thread_ids != []
    assert all(ident != loop_ident for ident in gateway.thread_ids)
