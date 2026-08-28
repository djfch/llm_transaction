"""研报复盘客观结果计算测试：时长映射、15m 窗口边界纪律、数据状态四态。

K 线来源用内存桩（满足 AsyncCandleSource 异步结构协议），不触真实网关；
GatewayAsyncCandleSource 适配器用假同步网关验证窗口透传、超宽分段拼合
与执行线程卸载。
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.gateway.base import Candle
from src.research.payload_v2 import HORIZON_SECONDS
from src.review.research_outcome import (
    PARTIAL_MIN_COVERAGE_PCT,
    GatewayAsyncCandleSource,
    compute_outcome,
    outcome_from_candles,
    partial_acceptable,
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


def _iso(ts: float) -> str:
    """测试辅助：Unix 秒 → UTC ISO 字符串（与 research_outcome._iso_utc 同格式）。

    参数：
        ts: float，Unix 秒时间戳

    返回：
        str：UTC ISO 字符串
    """
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


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
    assert result["price_start_at"] == _iso(_BASE_TS)  # 首根完整 K 线开盘时刻
    assert result["price_end_at"] == _iso(_BASE_TS + 86400)  # 末根收盘时刻 = 窗口终点
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


def test_outcome_unaligned_created_at_excludes_partial_bounds() -> None:
    """非整点 created_at：相交但不完整的首末 K 线不参与任何指标（R3）。

    created_at 落在第 1 根 K 线中段：i=0 相交不完整（剔除），窗口终点切断
    i=96（剔除）；全部指标只取完整落窗的 i=1..95——起点距 created_at
    600s、末根终点距 window_end 300s，均在 1 个 interval 内且无断档，
    仍判 complete。

    参数：无

    返回：
        None，断言起价/止价/高低只来自完整落窗根、actual 不计相交根
    """
    created_at = _BASE_TS + 300  # 窗口起点在第 1 根 K 线中段
    candles = _window_candles(97)  # i=0..96：首末根均只与窗口相交
    result = outcome_from_candles(candles, created_at, "当日")

    assert result["candles_expected"] == 96
    assert result["candles_actual"] == 95  # 只计完整落窗的 i=1..95
    assert result["start_price"] == "101"  # i=1（首根完整 K 线）开盘价
    assert result["end_price"] == "196"  # i=95（末根完整 K 线）收盘
    assert result["high"] == "200"  # i=95 最高；i=96 的 201 在窗口外，不得参与
    assert result["low"] == "96"  # i=1 最低 = 100 + 1 - 5
    assert result["data_status"] == "complete"  # 时间戳覆盖达标：首尾间隙 ≤1 根且无断档
    assert Decimal(result["return_pct"]) == pytest.approx(
        Decimal(95) / Decimal(101) * 100
    )  # (196-101)/101


def test_outcome_mid_window_gap_is_partial() -> None:
    """中间断档（抽掉一根）即使首尾贴边也判 partial（时间戳覆盖口径，R3）。

    参数：无

    返回：
        None，断言断档窗口 data_status=partial 且 error 提示覆盖不完整
    """
    candles = [c for i, c in enumerate(_window_candles(96)) if i != 40]  # 抽掉 i=40
    result = outcome_from_candles(candles, _BASE_TS, "当日")

    assert result["data_status"] == "partial"
    assert result["candles_actual"] == 95
    assert "覆盖不完整" in result["error"]


def test_outcome_no_complete_candle_yields_all_none() -> None:
    """窗口内只有相交但不完整的 K 线：价格字段全 None，partial 并附说明（R3）。

    参数：无

    返回：
        None，断言起价/止价/高低/涨跌幅全为 None、actual=0
    """
    created_at = _BASE_TS + 300  # 第 1 根 K 线中段的窗口起点
    result = outcome_from_candles([_window_candles(1)[0]], created_at, "当日")

    assert result["data_status"] == "partial"
    assert result["candles_actual"] == 0  # 相交但不完整的 K 线不参与计算
    assert result["start_price"] is None
    assert result["high"] is None
    assert result["low"] is None
    assert result["end_price"] is None
    assert result["return_pct"] is None
    assert "无完整落窗" in result["error"]


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


# ---------- partial 放行门槛（R1） ----------


def _outcome_dict(status: str, actual: int = 96, **overrides) -> dict:
    """构造 partial_acceptable 测试用的最小客观结果字典（当日窗口期望 96 根）。

    参数：
        status: str，data_status 取值
        actual: int，完整落窗 K 线根数
        overrides: dict，需要覆盖默认值的字段

    返回：
        dict：含 data_status/窗口端点/价格时点/计数/价格字段的结果字典
    """
    base = {
        "data_status": status,
        "window_start": _BASE_TS,
        "window_end": _BASE_TS + 86400,
        "price_start_at": _iso(_BASE_TS),  # 默认两端贴窗（端点缺口 0）
        "price_end_at": _iso(_BASE_TS + 86400),
        "candles_expected": 96,
        "candles_actual": actual,
        "start_price": "100",
        "end_price": "196",
        "return_pct": "96",
    }
    base.update(overrides)
    return base


def test_partial_acceptable_status_matrix() -> None:
    """complete 一律放行；pending/unavailable 一律拒绝。

    参数：无

    返回：
        None，断言四种 data_status 的门槛判定
    """
    assert partial_acceptable(_outcome_dict("complete")) is True
    assert partial_acceptable(_outcome_dict("pending")) is False
    assert partial_acceptable(_outcome_dict("unavailable")) is False


def test_partial_acceptable_requires_prices_and_coverage() -> None:
    """partial 须起止价/涨跌幅齐全且覆盖率 ≥80%：缺价或过稀一律拒绝。

    参数：无

    返回：
        None，断言缺止价、80.2% 达标、10.4% 过稀三种 partial 的门槛判定
    """
    assert partial_acceptable(_outcome_dict("partial", end_price=None, return_pct=None)) is False
    assert partial_acceptable(_outcome_dict("partial", actual=77)) is True  # 77/96 ≈ 80.2%
    assert partial_acceptable(_outcome_dict("partial", actual=10)) is False  # 10/96 ≈ 10.4%
    assert PARTIAL_MIN_COVERAGE_PCT == 80


def test_partial_acceptable_endpoint_gap_tolerance() -> None:
    """端点约束：首/末价格时点距窗口端点 ≤2 个 interval 放行，达到 3 个即拒（头/尾各验）。

    参数：无

    返回：
        None，断言头部与尾部缺口在容忍边界两侧的门槛判定
    """
    end = _BASE_TS + 86400
    # 头部缺口：2 根（1800s）放行、3 根（2700s）拒绝
    assert (
        partial_acceptable(_outcome_dict("partial", price_start_at=_iso(_BASE_TS + 1800))) is True
    )
    assert (
        partial_acceptable(_outcome_dict("partial", price_start_at=_iso(_BASE_TS + 2700))) is False
    )
    # 尾部缺口：2 根放行、3 根拒绝
    assert partial_acceptable(_outcome_dict("partial", price_end_at=_iso(end - 1800))) is True
    assert partial_acceptable(_outcome_dict("partial", price_end_at=_iso(end - 2700))) is False


def test_partial_acceptable_legacy_record_without_price_points_rejected() -> None:
    """缺价格时点字段（或值非法）的旧落库记录一律不达标（宁缺毋滥，倒逼重算）。

    参数：无

    返回：
        None，断言缺键、None 值与非法 ISO 字符串三种形态的门槛判定
    """
    legacy = _outcome_dict("partial")
    del legacy["price_start_at"], legacy["price_end_at"]
    assert partial_acceptable(legacy) is False
    assert partial_acceptable(_outcome_dict("partial", price_start_at=None)) is False
    assert partial_acceptable(_outcome_dict("partial", price_end_at="not-a-date")) is False


def test_outcome_missing_tail_fails_endpoint_constraint() -> None:
    """集成：当日窗只拿到前 80 根——覆盖率 83% 达标但尾部缺 16 根，端点约束拒绝。

    参数：无

    返回：
        None，断言 data_status=partial、价格时点位置与 partial_acceptable=False
    """
    result = outcome_from_candles(_window_candles(80), _BASE_TS, "当日")

    assert result["data_status"] == "partial"
    assert result["candles_actual"] * 100 >= result["candles_expected"] * PARTIAL_MIN_COVERAGE_PCT
    assert result["price_start_at"] == _iso(_BASE_TS)
    assert result["price_end_at"] == _iso(_BASE_TS + 80 * 900)
    assert partial_acceptable(result) is False


def test_outcome_missing_head_fails_endpoint_constraint() -> None:
    """集成：头部缺 3 根（覆盖率 96.9%）——端点约束拒绝，不得闭合为正常复盘。

    参数：无

    返回：
        None，断言 data_status=partial 且 partial_acceptable=False
    """
    result = outcome_from_candles(_window_candles(96)[3:], _BASE_TS, "当日")

    assert result["data_status"] == "partial"
    assert result["candles_actual"] == 93
    assert partial_acceptable(result) is False
