"""研报逐标的市场快照：闭合 K 线、技术指标、资金费率与持仓量变化。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Protocol

from src.gateway.base import Candle, Contract
from src.gateway.market_stats import OpenInterestPoint
from src.market import indicators
from src.market.intervals import interval_seconds

INTERVALS = ("4h", "1d")
HISTORY_LIMIT = 200
COMPLETE_FRAME_FIELDS = (
    "ema20",
    "ema50",
    "ema20_slope_pct_per_bar",
    "ema50_slope_pct_per_bar",
    "atr14",
    "volume_ratio",
    "oi_current",
    "oi_change_pct",
)
SLOPE_BARS = 3
ZERO = Decimal(0)


class CandleCacheLike(Protocol):
    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        """从 K 线缓存读取最近若干根 K 线。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，K 线周期（如 4h、1d）
            n: int，最多返回的 K 线根数

        返回：
            list[Candle]：最近 n 根 K 线，缓存数据不足时可能少于 n 根
        """
        ...


class MarketStatsGateway(Protocol):
    def get_contract(self, contract: str) -> Contract:
        """读取合约元信息（含当前资金费率）。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            Contract：合约元信息，funding_rate 字段为当前资金费率
        """
        ...

    def fetch_open_interest_history(
        self, contract: str, interval: str, limit: int = 3
    ) -> list[OpenInterestPoint]:
        """读取合约持仓量历史数据点。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，统计周期（如 4h、1d）
            limit: int，最多返回的数据点个数，省略时为 3

        返回：
            list[OpenInterestPoint]：持仓量历史数据点列表
        """
        ...


def _text(value: Decimal | None) -> str | None:
    """把数值格式化成普通十进制字符串，None 原样透传。

    参数：
        value: Decimal | None，待格式化的数值

    返回：
        str | None：去掉多余尾零的十进制字符串；入参为 None 时返回 None
    """
    return None if value is None else format(value.normalize(), "f")


def _latest(series: list[Decimal | None]) -> Decimal | None:
    """取指标序列的最新一个值。

    参数：
        series: list[Decimal | None]，指标历史序列

    返回：
        Decimal | None：序列末尾的值；序列为空时返回 None
    """
    return series[-1] if series else None


def _slope(series: list[Decimal | None]) -> Decimal | None:
    """计算指标序列最近几根 K 线的平均每根变化率（百分比）。

    参数：
        series: list[Decimal | None]，指标历史序列（如 EMA 序列），None 会被跳过

    返回：
        Decimal | None：最近 SLOPE_BARS 根每根平均变化百分比；有效值不足或基准值为 0 时返回 None
    """
    values = [value for value in series if value is not None]
    if len(values) <= SLOPE_BARS:
        return None
    base = values[-SLOPE_BARS - 1]
    if base == 0:
        return None
    return (values[-1] / base - 1) * 100 / SLOPE_BARS


def _trend(ema20_slope: Decimal | None, ema50_slope: Decimal | None) -> str:
    """根据两条 EMA 斜率判断价格趋势方向。

    参数：
        ema20_slope: Decimal | None，EMA20 每根平均变化百分比
        ema50_slope: Decimal | None，EMA50 每根平均变化百分比

    返回：
        str：两条斜率同正为「上涨」、同负为「下跌」，其余为「震荡」；任一斜率为 None 时返回「不可用」
    """
    if ema20_slope is None or ema50_slope is None:
        return "不可用"
    if ema20_slope > ZERO and ema50_slope > ZERO:
        return "上涨"
    if ema20_slope < ZERO and ema50_slope < ZERO:
        return "下跌"
    return "震荡"


def _volume_state(ratio: Decimal | None) -> str:
    """把量比划分为缩量、放量或正常。

    参数：
        ratio: Decimal | None，最近成交量与均量的比值

    返回：
        str：不大于 0.8 为「缩量」，不小于 1.2 为「放量」，其间为「正常」；为 None 时返回「不可用」
    """
    if ratio is None:
        return "不可用"
    if ratio <= Decimal("0.8"):
        return "缩量"
    if ratio >= Decimal("1.2"):
        return "放量"
    return "正常"


def _oi_state(change: Decimal | None) -> str:
    """把持仓量变化率划分为增仓、减仓或持平。

    参数：
        change: Decimal | None，持仓量变化百分比

    返回：
        str：大于 0.5 为「增仓」，小于 -0.5 为「减仓」，其间为「持平」；为 None 时返回「不可用」
    """
    if change is None:
        return "不可用"
    if change > Decimal("0.5"):
        return "增仓"
    if change < Decimal("-0.5"):
        return "减仓"
    return "持平"


def _divergence(
    ema20_slope: Decimal | None,
    ema50_slope: Decimal | None,
    volume_ratio: Decimal | None,
    oi_change: Decimal | None,
) -> dict:
    """综合价格趋势、量能与持仓量变化，识别背离与风险信号。

    参数：
        ema20_slope: Decimal | None，EMA20 每根平均变化百分比
        ema50_slope: Decimal | None，EMA50 每根平均变化百分比
        volume_ratio: Decimal | None，最近成交量与均量的比值
        oi_change: Decimal | None，持仓量变化百分比

    返回：
        dict：price_trend(价格趋势)、volume_state(量能状态)、oi_state(持仓状态)
        与 flags(信号列表，如量价背离、空头回补风险、多头清算驱动风险、趋势确认)
    """
    price = _trend(ema20_slope, ema50_slope)
    volume = _volume_state(volume_ratio)
    oi = _oi_state(oi_change)
    flags: list[str] = []
    directional = price in ("上涨", "下跌")
    if directional and volume == "缩量":
        flags.append("量价背离")
    if price == "上涨" and oi == "减仓":
        flags.append("空头回补风险")
    if price == "下跌" and oi == "减仓":
        flags.append("多头清算驱动风险")
    if directional and volume == "放量" and oi == "增仓":
        flags.append("趋势确认")
    return {"price_trend": price, "volume_state": volume, "oi_state": oi, "flags": flags}


def _candle_item(candle: Candle, span: int, now: float) -> dict:
    """把单根 K 线转成可 JSON 序列化的字典。

    参数：
        candle: Candle，K 线数据（开高低收、成交量与秒级时间戳）
        span: int，K 线周期秒数，用于判断该根是否已收盘
        now: float，当前时间戳（秒）

    返回：
        dict：时间与字符串化的开高低收量，及 closed(该 K 线是否已收盘)
    """
    return {
        "time": candle.t,
        "open": str(candle.o),
        "high": str(candle.h),
        "low": str(candle.l),
        "close": str(candle.c),
        "volume": str(candle.v),
        "closed": candle.t + span <= now,
    }


def _aligned_oi(
    points: list[OpenInterestPoint], end_ts: int
) -> tuple[Decimal | None, Decimal | None]:
    """把持仓量数据点对齐到已收盘 K 线末尾，算出当前值与环比变化率。

    参数：
        points: list[OpenInterestPoint]，持仓量历史数据点
        end_ts: int，对齐截止的秒级时间戳，晚于它的数据点会被丢弃

    返回：
        tuple[Decimal | None, Decimal | None]：(当前持仓量, 相对前一数据点的百分比变化)；
        无可用数据点时为 (None, None)，只有一个数据点或前值为 0 时变化率为 None
    """
    eligible = sorted((point for point in points if point.time <= end_ts), key=lambda p: p.time)
    if not eligible:
        return None, None
    current = eligible[-1].value
    if len(eligible) < 2 or eligible[-2].value == 0:
        return current, None
    return current, (current / eligible[-2].value - 1) * 100


class ResearchMarketDataService:
    """一次生成一个合约的 4h/1d 研报市场输入。"""

    def __init__(
        self,
        candles: CandleCacheLike,
        gateway: MarketStatsGateway,
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        """注入 K 线缓存、交易所网关与时钟。

        参数：
            candles: CandleCacheLike，K 线缓存，提供历史 K 线
            gateway: MarketStatsGateway，交易所网关，提供合约元信息与持仓量历史
            now_fn: Callable[[], float]，取当前时间戳（秒）的函数，省略时用 time.time（测试可注入假时钟）

        返回：
            None，仅把依赖保存为实例属性
        """
        self._candles = candles
        self._gateway = gateway
        self._now = now_fn

    async def snapshot(self, contract: str, limit: int = 30) -> dict:
        """生成单个合约的研报市场快照（资金费率 + 4h/1d 两个周期的数据帧）。

        参数：
            contract: str，合约名（如 BTC_USDT）
            limit: int，每个周期返回的 K 线根数，省略时为 30，须在 1-100 之间

        返回：
            dict：快照字典，含 contract(合约名)、requested_limit(请求根数)、
            generated_at(生成时间戳)、funding_rate(资金费率)、data_status(数据状态)、
            missing(缺失项说明列表)与 timeframes(各周期数据帧)

        异常：
            ValueError：limit 不在 1-100 之间时抛出
        """
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1-100 之间")
        now = self._now()
        missing: list[str] = []
        funding = await self._funding_rate(contract, missing)
        frames: dict[str, dict] = {}
        for interval in INTERVALS:
            frames[interval] = await self._timeframe(contract, interval, limit, now, missing)
        status = self._status(frames, funding)
        return {
            "contract": contract,
            "requested_limit": limit,
            "generated_at": int(now),
            "funding_rate": _text(funding),
            "data_status": status,
            "missing": missing,
            "timeframes": frames,
        }

    async def _funding_rate(self, contract: str, missing: list[str]) -> Decimal | None:
        """读取合约当前资金费率，读取失败时记录缺失并返回 None。

        参数：
            contract: str，合约名（如 BTC_USDT）
            missing: list[str]，缺失项说明列表，读取失败时追加说明

        返回：
            Decimal | None：当前资金费率；网关读取失败时返回 None
        """
        try:
            meta = await asyncio.to_thread(self._gateway.get_contract, contract)
            return meta.funding_rate
        except Exception:
            missing.append("funding_rate: 数据不可用")
            return None

    async def _timeframe(
        self, contract: str, interval: str, limit: int, now: float, missing: list[str]
    ) -> dict:
        """组装单个周期（4h 或 1d）的市场数据帧。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，K 线周期（4h 或 1d）
            limit: int，返回的 K 线根数上限
            now: float，当前时间戳（秒），用于划分已收盘 K 线
            missing: list[str]，缺失项说明列表，数据缺失时追加说明

        返回：
            dict：该周期的数据帧，含 K 线明细、技术指标、持仓量与背离信号
        """
        span = interval_seconds(interval)
        history = sorted(
            self._candles.get_recent(contract, interval, HISTORY_LIMIT), key=lambda candle: candle.t
        )
        closed = [candle for candle in history if candle.t + span <= now]
        raw = history[-limit:]
        oi_points = await self._oi_points(contract, interval, missing)
        end_ts = closed[-1].t + span if closed else int(now)
        oi_current, oi_change = _aligned_oi(oi_points, end_ts)
        if not closed:
            missing.append(f"{interval}: 无已收盘K线")
        if oi_current is None:
            missing.append(f"{interval}: 无持仓量历史")
        frame = self._frame_values(closed, raw, span, now, oi_current, oi_change)
        self._record_frame_missing(interval, frame, missing)
        return frame

    async def _oi_points(
        self, contract: str, interval: str, missing: list[str]
    ) -> list[OpenInterestPoint]:
        """读取持仓量历史数据点，读取失败时记录缺失并返回空列表。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，统计周期（4h 或 1d）
            missing: list[str]，缺失项说明列表，读取失败时追加说明

        返回：
            list[OpenInterestPoint]：最近 3 个持仓量数据点；网关读取失败时返回空列表
        """
        try:
            return await asyncio.to_thread(
                self._gateway.fetch_open_interest_history, contract, interval, 3
            )
        except Exception:
            missing.append(f"{interval}: 持仓量历史读取失败")
            return []

    @staticmethod
    def _frame_values(
        closed: list[Candle],
        raw: list[Candle],
        span: int,
        now: float,
        oi_current: Decimal | None,
        oi_change: Decimal | None,
    ) -> dict:
        """用已收盘 K 线计算单个周期的全部指标字段，组装成数据帧。

        参数：
            closed: list[Candle]，已收盘 K 线（按时间升序），指标只基于它们计算
            raw: list[Candle]，要原样输出的 K 线明细（含未收盘的最后一根）
            span: int，K 线周期秒数
            now: float，当前时间戳（秒）
            oi_current: Decimal | None，对齐后的当前持仓量
            oi_change: Decimal | None，持仓量环比变化百分比

        返回：
            dict：该周期数据帧，含 K 线明细、EMA20/EMA50 及其斜率、ATR14、量比、
            持仓量与变化率、背离信号；无法计算的字段为 None
        """
        closes = [candle.c for candle in closed]
        ema20 = indicators.ema_series(closes, 20)
        ema50 = indicators.ema_series(closes, 50)
        ema20_slope = _slope(ema20)
        ema50_slope = _slope(ema50)
        atr14 = _latest(indicators.atr_series(closed, 14))
        volume_ratio = _latest(indicators.vol_ratio_series(closed, 20))
        return {
            "candles": [_candle_item(candle, span, now) for candle in raw],
            "closed_candle_count": len(closed),
            "ema20": _text(_latest(ema20)),
            "ema50": _text(_latest(ema50)),
            "ema20_slope_pct_per_bar": _text(ema20_slope),
            "ema50_slope_pct_per_bar": _text(ema50_slope),
            "atr14": _text(atr14),
            "volume_ratio": _text(volume_ratio),
            "oi_current": _text(oi_current),
            "oi_change_pct": _text(oi_change),
            "divergence": _divergence(ema20_slope, ema50_slope, volume_ratio, oi_change),
        }

    @staticmethod
    def _record_frame_missing(interval: str, frame: dict, missing: list[str]) -> None:
        """检查数据帧完整性，把缺失情况追加到缺失项说明列表。

        参数：
            interval: str，K 线周期（4h 或 1d），用于拼接说明文字
            frame: dict，单个周期的数据帧
            missing: list[str]，缺失项说明列表

        返回：
            None，就地向 missing 追加技术指标不完整或 OI 变化率不可用的说明
        """
        technical_fields = COMPLETE_FRAME_FIELDS[:-2]
        absent = [field for field in technical_fields if frame[field] is None]
        if frame["closed_candle_count"] and absent:
            missing.append(f"{interval}: 技术指标不完整({','.join(absent)})")
        if frame["oi_current"] is not None and frame["oi_change_pct"] is None:
            missing.append(f"{interval}: OI变化率不可用(统计点不足或前值为0)")

    @staticmethod
    def _status(frames: dict[str, dict], funding: Decimal | None) -> str:
        """汇总两个周期与资金费率的完整性，给出整体数据状态。

        参数：
            frames: dict[str, dict]，各周期（4h/1d）的数据帧
            funding: Decimal | None，当前资金费率

        返回：
            str：所有周期都无已收盘 K 线为「不可用」；资金费率齐备且各周期关键字段
            齐全为「完整」；其余为「部分缺失」
        """
        if all(frame["closed_candle_count"] == 0 for frame in frames.values()):
            return "不可用"
        complete = funding is not None and all(
            all(frame[field] is not None for field in COMPLETE_FRAME_FIELDS)
            for frame in frames.values()
        )
        return "完整" if complete else "部分缺失"
