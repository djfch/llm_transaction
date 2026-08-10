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
    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]: ...


class MarketStatsGateway(Protocol):
    def get_contract(self, contract: str) -> Contract: ...

    def fetch_open_interest_history(
        self, contract: str, interval: str, limit: int = 3
    ) -> list[OpenInterestPoint]: ...


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _latest(series: list[Decimal | None]) -> Decimal | None:
    return series[-1] if series else None


def _slope(series: list[Decimal | None]) -> Decimal | None:
    values = [value for value in series if value is not None]
    if len(values) <= SLOPE_BARS:
        return None
    base = values[-SLOPE_BARS - 1]
    if base == 0:
        return None
    return (values[-1] / base - 1) * 100 / SLOPE_BARS


def _trend(ema20_slope: Decimal | None, ema50_slope: Decimal | None) -> str:
    if ema20_slope is None or ema50_slope is None:
        return "不可用"
    if ema20_slope > ZERO and ema50_slope > ZERO:
        return "上涨"
    if ema20_slope < ZERO and ema50_slope < ZERO:
        return "下跌"
    return "震荡"


def _volume_state(ratio: Decimal | None) -> str:
    if ratio is None:
        return "不可用"
    if ratio <= Decimal("0.8"):
        return "缩量"
    if ratio >= Decimal("1.2"):
        return "放量"
    return "正常"


def _oi_state(change: Decimal | None) -> str:
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
        self._candles = candles
        self._gateway = gateway
        self._now = now_fn

    async def snapshot(self, contract: str, limit: int = 30) -> dict:
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
        try:
            meta = await asyncio.to_thread(self._gateway.get_contract, contract)
            return meta.funding_rate
        except Exception:
            missing.append("funding_rate: 数据不可用")
            return None

    async def _timeframe(
        self, contract: str, interval: str, limit: int, now: float, missing: list[str]
    ) -> dict:
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
        technical_fields = COMPLETE_FRAME_FIELDS[:-2]
        absent = [field for field in technical_fields if frame[field] is None]
        if frame["closed_candle_count"] and absent:
            missing.append(f"{interval}: 技术指标不完整({','.join(absent)})")
        if frame["oi_current"] is not None and frame["oi_change_pct"] is None:
            missing.append(f"{interval}: OI变化率不可用(统计点不足或前值为0)")

    @staticmethod
    def _status(frames: dict[str, dict], funding: Decimal | None) -> str:
        if all(frame["closed_candle_count"] == 0 for frame in frames.values()):
            return "不可用"
        complete = funding is not None and all(
            all(frame[field] is not None for field in COMPLETE_FRAME_FIELDS)
            for frame in frames.values()
        )
        return "完整" if complete else "部分缺失"
