"""研报复盘的客观行情结果计算（issue #113）。

按研报结论的 horizon 窗口拉取历史 1h K 线，计算方向批改所需的客观指标
（起止价、区间最高最低、涨跌幅、最大有利/不利变动）。纯函数
（outcome_from_candles）与 IO（compute_outcome）分离：K 线拉取走
CandleSource 窄协议，复盘侧不持有完整 Gateway。注意 Gate 网关 from/to
区间路径当前不可用（见 gate_rest docstring），生产装配应包一层
RecentWindowCandleSource（最近 N 根 + 客户端窗口过滤）而非直传网关。

data_status 四态：pending（窗口未到期）/ unavailable（窗口内无 K 线或
horizon 非法、拉取失败）/ partial（有 K 线但不足窗口期望根数）/
complete（覆盖完整窗口）。金额与百分比一律 Decimal 计算，JSON 落库前转 str。
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Protocol

from src.gateway.base import Candle
from src.research.payload_v2 import HORIZON_SECONDS

_CANDLE_INTERVAL = "1h"
_CANDLE_SECONDS = 3600


class CandleSource(Protocol):
    """K 线只读来源窄协议：与 Gateway.get_candlesticks 同签名，结构子类型满足。"""

    def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """读取合约历史 K 线（签名与 Gateway 协议一致，便于直接透传网关实例）。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，K 线周期；本模块固定传 1h
            limit: int | None，最近 N 根；与 from_ts/to_ts 互斥
            from_ts: int | None，起始秒级时间戳
            to_ts: int | None，结束秒级时间戳

        返回：
            list[Candle]：K 线列表；无数据时返回空列表
        """
        ...


def _empty_outcome(
    data_status: str,
    *,
    window_start: float,
    window_end: float,
    expected: int,
    actual: int = 0,
    error: str = "",
) -> dict[str, Any]:
    """构造无行情数据时的结果骨架（价格类字段一律 None）。

    参数：
        data_status: str，数据状态（pending/unavailable）
        window_start: float，窗口起始时间戳
        window_end: float，窗口结束时间戳
        expected: int，窗口期望 1h K 线根数
        actual: int，实际拿到的窗口内 K 线根数
        error: str，补充说明（如 horizon 非法、拉取异常）；无则为空串

    返回：
        dict[str, Any]：除计数与状态外价格字段全为 None 的结果字典
    """
    return {
        "data_status": data_status,
        "window_start": window_start,
        "window_end": window_end,
        "candles_expected": expected,
        "candles_actual": actual,
        "start_price": None,
        "end_price": None,
        "high": None,
        "low": None,
        "return_pct": None,
        "max_up_pct": None,
        "max_down_pct": None,
        "error": error,
    }


def _pct(part: Decimal, base: Decimal) -> str:
    """计算相对基准价的百分比变动字符串（base 为 0 时返回 '0' 防除零）。

    参数：
        part: Decimal，变动量（目标价减基准价）
        base: Decimal，基准价

    返回：
        str：百分比数值字符串（保留 Decimal 全精度）
    """
    if base == 0:
        return "0"
    return str(part / base * 100)


def outcome_from_candles(candles: list[Candle], created_at: float, horizon: str) -> dict[str, Any]:
    """纯函数：给定 K 线列表，过滤出 horizon 窗口内的部分并计算客观结果。

    边界纪律：窗口 [created_at, created_at + 窗口秒数) 之外的 K 线一律丢弃，
    起止价/最高最低只来自窗口内行情。

    参数：
        candles: list[Candle]，候选 K 线列表（允许含窗口外数据，函数内过滤）
        created_at: float，研报创建时间戳（窗口起点）
        horizon: str，结论时间范围（当日/3日/周，须在 HORIZON_SECONDS 内）

    返回：
        dict[str, Any]：客观结果字典；窗口内无 K 线时 data_status=unavailable，
        不足期望根数时 partial，否则 complete
    """
    seconds = HORIZON_SECONDS[horizon]
    window_end = created_at + seconds
    expected = seconds // _CANDLE_SECONDS
    window = sorted((c for c in candles if created_at <= c.t < window_end), key=lambda c: c.t)
    if not window:
        return _empty_outcome(
            "unavailable",
            window_start=created_at,
            window_end=window_end,
            expected=expected,
            error="窗口内无 K 线数据",
        )
    start = window[0].o
    end = window[-1].c
    high = max(c.h for c in window)
    low = min(c.l for c in window)
    return {
        "data_status": "complete" if len(window) >= expected else "partial",
        "window_start": created_at,
        "window_end": window_end,
        "candles_expected": expected,
        "candles_actual": len(window),
        "start_price": str(start),
        "end_price": str(end),
        "high": str(high),
        "low": str(low),
        "return_pct": _pct(end - start, start),
        "max_up_pct": _pct(high - start, start),
        "max_down_pct": _pct(low - start, start),
        "error": "",
    }


def compute_outcome(
    contract: str,
    created_at: float,
    horizon: str,
    candle_source: CandleSource,
    now: float | None = None,
) -> dict[str, Any]:
    """IO 层：按 horizon 窗口拉取历史 1h K 线并计算客观结果。

    窗口未到期直接返回 pending（不拉取）；horizon 非法或拉取异常返回
    unavailable 并附 error 说明——单候选数据问题不向调用方抛异常，由
    data_status 表达，避免拖垮整轮复盘。

    参数：
        contract: str，合约名
        created_at: float，研报创建时间戳（窗口起点）
        horizon: str，结论时间范围（当日/3日/周）
        candle_source: CandleSource，K 线只读来源（网关或 paper 引擎）
        now: float | None，当前时间戳（测试可注入）；None 时取真实当前时间

    返回：
        dict[str, Any]：客观结果字典（结构同 outcome_from_candles）
    """
    seconds = HORIZON_SECONDS.get(horizon)
    if seconds is None:
        return _empty_outcome(
            "unavailable",
            window_start=created_at,
            window_end=created_at,
            expected=0,
            error=f"未知 horizon：{horizon}",
        )
    window_end = created_at + seconds
    if (now if now is not None else time.time()) < window_end:
        return _empty_outcome(
            "pending",
            window_start=created_at,
            window_end=window_end,
            expected=seconds // _CANDLE_SECONDS,
            error="horizon 窗口未到期",
        )
    try:
        candles = candle_source.get_candlesticks(
            contract, interval=_CANDLE_INTERVAL, from_ts=int(created_at), to_ts=int(window_end)
        )
    except Exception as exc:  # 网关/网络异常不拖垮整轮复盘，以 unavailable 表达
        return _empty_outcome(
            "unavailable",
            window_start=created_at,
            window_end=window_end,
            expected=seconds // _CANDLE_SECONDS,
            error=f"K 线拉取失败：{exc}",
        )
    return outcome_from_candles(candles, created_at, horizon)


class RecentWindowCandleSource:
    """CandleSource 窗口适配器：from/to 历史窗口改走「最近 N 根 + 客户端过滤」。

    存在原因：Gate 网关的 from/to 区间参数当前未正确映射（见
    gate_rest.get_candlesticks docstring，传历史区间会被 gate-api 拒绝），
    而 limit 路径稳定可用。复盘最长窗口为「周」（7 天）加调度延迟，默认
    回拉最近 300 根（1h 约 12.5 天）覆盖；更早的窗口以实际覆盖根数表达
    （partial/unavailable），不向调用方抛异常。paper 引擎同路径生效。
    """

    def __init__(self, gateway: CandleSource, *, recent_limit: int = 300) -> None:
        """绑定底层 K 线来源与回拉根数。

        参数：
            gateway: CandleSource，实际 K 线来源（真实网关或 paper 引擎）
            recent_limit: int，from/to 查询时回拉的最近 K 线根数（钳制 1~2000，
                与网关单次上限一致）

        返回：
            None，仅保存依赖与配置，不触发任何 IO
        """
        self._gateway = gateway
        self._recent_limit = min(max(1, recent_limit), 2000)

    def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """读取 K 线：纯 limit 查询直通底层；from/to 窗口以最近 N 根过滤实现。

        参数：
            contract: str，合约名
            interval: str，K 线周期（透传底层）
            limit: int | None，最近 N 根；未传 from/to 时直通底层
            from_ts: int | None，窗口起始秒级时间戳（含）
            to_ts: int | None，窗口结束秒级时间戳（不含）

        返回：
            list[Candle]：窗口内（[from_ts, to_ts)）的 K 线；窗口超出回拉
            范围时只返回实际覆盖部分
        """
        if from_ts is None and to_ts is None:
            return self._gateway.get_candlesticks(contract, interval=interval, limit=limit)
        candles = self._gateway.get_candlesticks(
            contract, interval=interval, limit=self._recent_limit
        )
        lo = float("-inf") if from_ts is None else from_ts
        hi = float("inf") if to_ts is None else to_ts
        return [c for c in candles if lo <= c.t < hi]
