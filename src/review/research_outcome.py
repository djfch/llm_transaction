"""研报复盘的客观行情结果计算（issue #113）。

按研报结论的 horizon 窗口拉取历史 15m K 线，计算方向批改所需的客观指标
（起止价、区间最高最低、涨跌幅、最大有利/不利变动）。纯函数
（outcome_from_candles）与 IO（compute_outcome）分离：K 线拉取走
AsyncCandleSource 异步窄协议，复盘侧不持有完整 Gateway；生产装配用
GatewayAsyncCandleSource 包同步网关（run_gateway_io 卸载 + 真 from/to
窗口透传 + 超 2000 根分段拉取）。

窗口边界纪律（15m K 线）：只有完整落在窗口内的 K 线参与全部指标
（起价 open/止价 close/最高/最低），相交但不完整的 K 线一概不参与计算
——严格不用窗口外任何价格，避免起始/末端部分 K 线把窗口外走势带进
批改依据（R3）。complete 判定用时间戳覆盖口径：首根完整 K 线起点距
created_at 不超过 1 个 interval、末根完整 K 线终点距 window_end 不超过
1 个 interval、中间无断档，否则 partial。

data_status 四态：pending（窗口未到期）/ unavailable（窗口内无 K 线或
horizon 非法、拉取失败）/ partial（有完整 K 线但窗口覆盖不达标）/
complete（覆盖完整窗口）。金额与百分比一律 Decimal 计算，JSON 落库前转 str。
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Protocol

from src.gateway.async_io import run_gateway_io
from src.gateway.base import Candle
from src.market.intervals import interval_seconds
from src.research.payload_v2 import HORIZON_SECONDS

_CANDLE_INTERVAL = "15m"
_CANDLE_SECONDS = 900
_PAGE_LIMIT = 2000  # 网关单次 from/to 窗口的最大 K 线根数上限

# partial 放行覆盖率下限（R1）：完整落窗 K 线覆盖时长须 ≥ 窗口时长的 80%。
# 窗口已到期但 K 线过稀时，少数样本算出的起止价/涨跌幅不具批改代表性，
# 用过稀数据落库会把候选永久闭合在低质量结论上；拒绝后候选留在队列里，
# 后续轮次行情回补（或覆盖达标）仍可复盘。80% 在"零星缺根仍可利用"
# 与"样本足以代表窗口走势"之间取折中。
PARTIAL_MIN_COVERAGE_PCT = 80


class AsyncCandleSource(Protocol):
    """K 线只读来源异步窄协议：与 Gateway.get_candlesticks 同参数的协程版本。"""

    async def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """读取合约历史 K 线（参数口径与 Gateway 协议一致）。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，K 线周期；本模块固定传 15m
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
        data_status: str，数据状态（pending/unavailable；窗口有相交 K 线但
            无完整落窗 K 线时为 partial）
        window_start: float，窗口起始时间戳
        window_end: float，窗口结束时间戳
        expected: int，窗口期望 15m K 线根数
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
    """纯函数：只用完整落在 horizon 窗口内的 K 线计算客观结果。

    边界纪律（严格不用窗口外任何价格）：仅完整落窗 K 线
    （created_at <= c.t 且 c.t + 900 <= window_end）参与全部指标——起价取
    首根完整 K 线开盘、止价取末根完整 K 线收盘、最高/最低取完整 K 线极值；
    相交但不完整的 K 线一概不参与计算（R3）。complete 判定为时间戳覆盖
    口径：首根完整 K 线起点距 created_at ≤1 个 interval、末根完整 K 线
    终点距 window_end ≤1 个 interval、中间相邻起点间距均 ≤1 个 interval
    （无断档），三者同时满足才 complete，否则 partial。

    参数：
        candles: list[Candle]，候选 K 线列表（允许含窗口外数据，函数内过滤）
        created_at: float，研报创建时间戳（窗口起点）
        horizon: str，结论时间范围（当日/3日/周，须在 HORIZON_SECONDS 内）

    返回：
        dict[str, Any]：客观结果字典；窗口内零相交 K 线时 unavailable，
        有相交但无完整落窗 K 线时 partial 且价格字段全 None
    """
    seconds = HORIZON_SECONDS[horizon]
    window_end = created_at + seconds
    expected = seconds // _CANDLE_SECONDS
    if not any(c.t < window_end and c.t + _CANDLE_SECONDS > created_at for c in candles):
        return _empty_outcome(
            "unavailable",
            window_start=created_at,
            window_end=window_end,
            expected=expected,
            error="窗口内无 K 线数据",
        )
    inside = sorted(
        (c for c in candles if created_at <= c.t and c.t + _CANDLE_SECONDS <= window_end),
        key=lambda c: c.t,
    )
    if not inside:
        return _empty_outcome(
            "partial",
            window_start=created_at,
            window_end=window_end,
            expected=expected,
            error="窗口内有相交 K 线但无完整落窗 K 线",
        )
    start, end = inside[0].o, inside[-1].c
    high = max(c.h for c in inside)
    low = min(c.l for c in inside)
    complete = (
        inside[0].t - created_at <= _CANDLE_SECONDS
        and window_end - (inside[-1].t + _CANDLE_SECONDS) <= _CANDLE_SECONDS
        and all(inside[i + 1].t - inside[i].t <= _CANDLE_SECONDS for i in range(len(inside) - 1))
    )
    return {
        "data_status": "complete" if complete else "partial",
        "window_start": created_at,
        "window_end": window_end,
        "candles_expected": expected,
        "candles_actual": len(inside),
        "start_price": str(start),
        "end_price": str(end),
        "high": str(high),
        "low": str(low),
        "return_pct": _pct(end - start, start),
        "max_up_pct": _pct(high - start, start),
        "max_down_pct": _pct(low - start, start),
        "error": "" if complete else f"窗口覆盖不完整（完整落窗 {len(inside)}/{expected} 根）",
    }


def partial_acceptable(outcome: dict[str, Any]) -> bool:
    """判断客观结果是否达到可批改门槛（纯函数，R1：partial 不再无条件放行）。

    complete 一律放行；partial 须同时满足：start_price/end_price/return_pct
    全非空，且完整落窗 K 线覆盖时长达到窗口时长的 PARTIAL_MIN_COVERAGE_PCT
    （以 candles_actual/candles_expected 折算，二者同乘 900 秒约去）。
    pending/unavailable 与不达标 partial 一律不可批改，留待后续轮次。

    参数：
        outcome: dict[str, Any]，compute_outcome/outcome_from_candles 的结果字典

    返回：
        bool：True 表示数据足以支撑批改提交
    """
    status = outcome.get("data_status")
    if status == "complete":
        return True
    if status != "partial":
        return False
    if not all(outcome.get(k) for k in ("start_price", "end_price", "return_pct")):
        return False
    expected = outcome.get("candles_expected") or 0
    actual = outcome.get("candles_actual") or 0
    return expected > 0 and actual * 100 >= expected * PARTIAL_MIN_COVERAGE_PCT


async def compute_outcome(
    contract: str,
    created_at: float,
    horizon: str,
    candle_source: AsyncCandleSource,
    now: float | None = None,
) -> dict[str, Any]:
    """IO 层：按 horizon 窗口拉取历史 15m K 线并计算客观结果。

    窗口未到期直接返回 pending（不拉取）；horizon 非法或拉取异常返回
    unavailable 并附 error 说明——单候选数据问题不向调用方抛异常，由
    data_status 表达，避免拖垮整轮复盘。

    参数：
        contract: str，合约名
        created_at: float，研报创建时间戳（窗口起点）
        horizon: str，结论时间范围（当日/3日/周）
        candle_source: AsyncCandleSource，K 线只读来源（异步协议）
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
        candles = await candle_source.get_candlesticks(
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


class GatewayAsyncCandleSource:
    """AsyncCandleSource 生产适配器：同步网关 → 异步真窗口 K 线源。

    所有底层调用经 run_gateway_io 卸载（不占事件循环线程）；from/to 窗口
    真实透传网关；窗口跨距超过单次上限（2000 根 × 周期秒数）时按上限分段
    循环拉取并按时间戳去重拼合。paper 引擎同为同步实现，走同一卸载路径。
    """

    def __init__(self, gateway: Any, *, page_limit: int = _PAGE_LIMIT) -> None:
        """绑定底层同步 K 线来源与单段根数上限。

        参数：
            gateway: Any，同步 K 线来源（真实网关或 paper 引擎，结构满足
                Gateway.get_candlesticks）
            page_limit: int，from/to 窗口单段拉取的最大根数（钳制 1~2000，
                与网关单次上限一致）

        返回：
            None，仅保存依赖与配置，不触发任何 IO
        """
        self._gateway = gateway
        self._page_limit = min(max(1, page_limit), _PAGE_LIMIT)

    async def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """读取 K 线：纯 limit 查询直通底层；from/to 窗口真实透传，超宽窗口分段拼合。

        参数：
            contract: str，合约名
            interval: str，K 线周期（透传底层；分段宽度按 interval_seconds 换算）
            limit: int | None，最近 N 根；未传 from/to 时直通底层
            from_ts: int | None，窗口起始秒级时间戳
            to_ts: int | None，窗口结束秒级时间戳

        返回：
            list[Candle]：窗口内的 K 线，按时间升序去重拼合
        """
        if from_ts is None or to_ts is None:
            return await run_gateway_io(
                self._gateway.get_candlesticks, contract, interval=interval, limit=limit
            )
        span = interval_seconds(interval)
        step = self._page_limit * span
        if to_ts - from_ts <= step:
            return await run_gateway_io(
                self._gateway.get_candlesticks,
                contract,
                interval=interval,
                from_ts=from_ts,
                to_ts=to_ts,
            )
        merged: dict[int, Candle] = {}
        seg_start = from_ts
        while seg_start < to_ts:
            seg_end = min(to_ts, seg_start + step)
            batch = await run_gateway_io(
                self._gateway.get_candlesticks,
                contract,
                interval=interval,
                from_ts=seg_start,
                to_ts=seg_end,
            )
            for candle in batch:
                merged[candle.t] = candle
            seg_start = seg_end
        return [merged[t] for t in sorted(merged)]
