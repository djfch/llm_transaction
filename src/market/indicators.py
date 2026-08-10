"""技术指标纯函数库：输入 K 线（或 Decimal 序列），输出 Decimal，数据不足返回 None。

所有 *_series 函数返回与输入等长的序列（数据不足的前段为 None），内部增量推进
O(n)，供服务层逐根对齐输出；单值函数取序列末值。全程 Decimal，禁止 float。
"""

from __future__ import annotations

from decimal import Decimal
from typing import TypeVar

from ..gateway.base import Candle

T = TypeVar("T")


def _last(seq: list[T | None]) -> T | None:
    """序列末值；空序列返回 None。

    参数：
        seq: list[T | None]，输入数值序列
    返回：
        T | None，序列末值；空序列返回 None
    """
    return seq[-1] if seq else None


def sma_series(values: list[Decimal], period: int) -> list[Decimal | None]:
    """简单均线序列（滚动和增量推进）。

    参数：
        values: list[Decimal]，待对齐或计算的数值序列
        period: int，指标计算周期
    返回：
        list[Decimal | None]，简单均线序列（滚动和增量推进）
    """
    out: list[Decimal | None] = [None] * len(values)
    window = Decimal(0)
    for i, v in enumerate(values):
        window += v
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema_series(values: list[Decimal], period: int) -> list[Decimal | None]:
    """指数均线序列：前 period 根 SMA 播种，之后按 alpha=2/(period+1) 递推。

    参数：
        values: list[Decimal]，待对齐或计算的数值序列
        period: int，指标计算周期
    返回：
        list[Decimal | None]，指数均线序列：前 period 根 SMA 播种，之后按 alpha=2/(period+1) 递推
    """
    out: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = Decimal(2) / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def _rsi_value(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    """由平均涨跌幅算 RSI；avg_loss 为 0 时分两种：avg_gain 也为 0（横盘无方向）取中性 50，否则持续上涨取 100。

    参数：
        avg_gain: Decimal，平均上涨幅度
        avg_loss: Decimal，平均下跌幅度
    返回：
        Decimal，由平均涨跌幅算 RSI；avg_loss 为 0 时分两种：avg_gain 也为 0（横盘无方向）取中性 50，否则持续上涨取 100
    """
    if avg_loss == 0:
        return Decimal(50) if avg_gain == 0 else Decimal(100)
    return Decimal(100) - Decimal(100) / (1 + avg_gain / avg_loss)


def rsi_series(closes: list[Decimal], period: int = 14) -> list[Decimal | None]:
    """RSI 序列（Wilder 平滑）：前 period 个涨跌量简单平均播种，之后递推。

    参数：
        closes: list[Decimal]，收盘价序列
        period: int，指标计算周期
    返回：
        list[Decimal | None]，RSI 序列（Wilder 平滑）：前 period 个涨跌量简单平均播种，之后递推
    """
    out: list[Decimal | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    changes = [closes[i] - closes[i - 1] for i in range(1, period + 1)]
    avg_gain = sum((max(c, Decimal(0)) for c in changes), Decimal(0)) / period
    avg_loss = sum((max(-c, Decimal(0)) for c in changes), Decimal(0)) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, Decimal(0))) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, Decimal(0))) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _realign(source: list[Decimal | None], computed: list[Decimal | None]) -> list[Decimal | None]:
    """把 source 非 None 段上的计算结果按原索引放回（前段 None 保持）。

    参数：
        source: list[Decimal | None]，成交来源分类
        computed: list[Decimal | None]，已计算的非空结果序列
    返回：
        list[Decimal | None]，把 source 非 None 段上的计算结果按原索引放回（前段 None 保持）
    """
    it = iter(computed)
    return [None if v is None else next(it) for v in source]


def macd_series(
    closes: list[Decimal], fast: int = 12, slow: int = 26, signal: int = 9
) -> list[tuple[Decimal, Decimal, Decimal] | None]:
    """MACD 序列 (dif, dea, hist)：dif=快EMA-慢EMA，dea=dif 的 signal 周期 EMA。

    参数：
        closes: list[Decimal]，收盘价序列
        fast: int，快速均线周期
        slow: int，慢速均线周期
        signal: int，信号均线周期
    返回：
        list[tuple[Decimal, Decimal, Decimal] | None]，MACD 序列 (dif, dea, hist)：dif=快EMA-慢EMA，dea=dif 的 signal 周期 EMA
    """
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    dif: list[Decimal | None] = [
        None if a is None or b is None else a - b for a, b in zip(ema_fast, ema_slow)
    ]
    dea = _realign(dif, ema_series([d for d in dif if d is not None], signal))
    return [None if d is None or e is None else (d, e, d - e) for d, e in zip(dif, dea)]


def kdj_series(
    candles: list[Candle], period: int = 9
) -> list[tuple[Decimal, Decimal, Decimal] | None]:
    """KDJ 序列 (k, d, j)：RSV=(C-LLV)/(HHV-LLV)*100，K/D 按 1/3-2/3 平滑（初值 50）。

    参数：
        candles: list[Candle]，按时间排序的 K 线序列
        period: int，指标计算周期
    返回：
        list[tuple[Decimal, Decimal, Decimal] | None]，KDJ 序列 (k, d, j)：RSV=(C-LLV)/(HHV-LLV)*100，K/D 按 1/3-2/3 平滑（初值 50）
    """
    out: list[tuple[Decimal, Decimal, Decimal] | None] = [None] * len(candles)
    k = d = Decimal(50)
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1 : i + 1]
        hh = max(c.h for c in window)
        ll = min(c.l for c in window)
        rsv = Decimal(0) if hh == ll else (candles[i].c - ll) / (hh - ll) * 100
        k = (2 * k + rsv) / 3
        d = (2 * d + k) / 3
        out[i] = (k, d, 3 * k - 2 * d)
    return out


def roc_series(closes: list[Decimal], period: int = 10) -> list[Decimal | None]:
    """变动率序列：(close / period 根前收盘 - 1) * 100；基准价为 0 时该点 None。

    参数：
        closes: list[Decimal]，收盘价序列
        period: int，指标计算周期
    返回：
        list[Decimal | None]，变动率序列：(close / period 根前收盘 - 1) * 100；基准价为 0 时该点 None
    """
    out: list[Decimal | None] = [None] * len(closes)
    for i in range(period, len(closes)):
        base = closes[i - period]
        if base != 0:
            out[i] = (closes[i] / base - 1) * 100
    return out


def _tr_series(candles: list[Candle]) -> list[Decimal]:
    """真实波幅序列：首根无前收取 h-l，其后取 max(h-l, |h-前收|, |l-前收|)。

    参数：
        candles: list[Candle]，按时间排序的 K 线序列
    返回：
        list[Decimal]，真实波幅序列：首根无前收取 h-l，其后取 max(h-l, |h-前收|, |l-前收|)
    """
    out: list[Decimal] = []
    prev_c: Decimal | None = None
    for c in candles:
        if prev_c is None:
            out.append(c.h - c.l)
        else:
            out.append(max(c.h - c.l, abs(c.h - prev_c), abs(c.l - prev_c)))
        prev_c = c.c
    return out


def atr_series(candles: list[Candle], period: int = 14) -> list[Decimal | None]:
    """ATR 序列（Wilder）：前 period 个 TR 简单平均播种，之后 (prev*(n-1)+tr)/n 递推。

    参数：
        candles: list[Candle]，按时间排序的 K 线序列
        period: int，指标计算周期
    返回：
        list[Decimal | None]，ATR 序列（Wilder）：前 period 个 TR 简单平均播种，之后 (prev*(n-1)+tr)/n 递推
    """
    trs = _tr_series(candles)
    out: list[Decimal | None] = [None] * len(trs)
    if len(trs) < period:
        return out
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def bollinger_series(
    closes: list[Decimal], period: int = 20, mult: Decimal = Decimal(2)
) -> list[tuple[Decimal, Decimal, Decimal] | None]:
    """布林带序列 (upper, mid, lower)：mid=SMA，带宽 = mult × 总体标准差（除以 n）。

    参数：
        closes: list[Decimal]，收盘价序列
        period: int，指标计算周期
        mult: Decimal，标准差带宽倍数
    返回：
        list[tuple[Decimal, Decimal, Decimal] | None]，布林带序列 (upper, mid, lower)：mid=SMA，带宽 = mult × 总体标准差（除以 n）
    """
    out: list[tuple[Decimal, Decimal, Decimal] | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        mid = sum(window) / period
        var = sum((x - mid) ** 2 for x in window) / period
        sd = var.sqrt()
        out[i] = (mid + mult * sd, mid, mid - mult * sd)
    return out


def vol_ratio_series(candles: list[Candle], period: int = 20) -> list[Decimal | None]:
    """量比序列：当根成交量 / 近 period 根（含当根）均量；均量为 0 时该点 None。

    参数：
        candles: list[Candle]，按时间排序的 K 线序列
        period: int，指标计算周期
    返回：
        list[Decimal | None]，量比序列：当根成交量 / 近 period 根（含当根）均量；均量为 0 时该点 None
    """
    out: list[Decimal | None] = [None] * len(candles)
    window = Decimal(0)
    for i, c in enumerate(candles):
        window += c.v
        if i >= period:
            window -= candles[i - period].v
        if i >= period - 1:
            avg = window / period
            out[i] = None if avg == 0 else c.v / avg
    return out


def obv_series(candles: list[Candle]) -> list[Decimal]:
    """OBV 序列：从 0 起累计，收盘涨加量、跌减量、平盘不变（无 None 值）。

    参数：
        candles: list[Candle]，按时间排序的 K 线序列
    返回：
        list[Decimal]，OBV 序列：从 0 起累计，收盘涨加量、跌减量、平盘不变（无 None 值）
    """
    out: list[Decimal] = []
    total = Decimal(0)
    prev_c: Decimal | None = None
    for c in candles:
        if prev_c is not None:
            if c.c > prev_c:
                total += c.v
            elif c.c < prev_c:
                total -= c.v
        out.append(total)
        prev_c = c.c
    return out


def sma(values: list[Decimal], period: int) -> Decimal | None:
    """最新一根 SMA；不足 period 根返回 None。

    参数：
        values: list[Decimal]，待对齐或计算的数值序列
        period: int，指标计算周期
    返回：
        Decimal | None，最新一根 SMA；不足 period 根返回 None
    """
    return _last(sma_series(values, period))


def ema(values: list[Decimal], period: int) -> Decimal | None:
    """最新一根 EMA；不足 period 根返回 None。

    参数：
        values: list[Decimal]，待对齐或计算的数值序列
        period: int，指标计算周期
    返回：
        Decimal | None，最新一根 EMA；不足 period 根返回 None
    """
    return _last(ema_series(values, period))


def rsi(closes: list[Decimal], period: int = 14) -> Decimal | None:
    """最新一根 RSI（Wilder）；不足 period+1 根返回 None。

    参数：
        closes: list[Decimal]，收盘价序列
        period: int，指标计算周期
    返回：
        Decimal | None，最新一根 RSI（Wilder）；不足 period+1 根返回 None
    """
    return _last(rsi_series(closes, period))


def macd(
    closes: list[Decimal], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Decimal, Decimal, Decimal] | None:
    """最新一根 MACD (dif, dea, hist)；数据不足返回 None。

    参数：
        closes: list[Decimal]，收盘价序列
        fast: int，快速均线周期
        slow: int，慢速均线周期
        signal: int，信号均线周期
    返回：
        tuple[Decimal, Decimal, Decimal] | None，最新一根 MACD (dif, dea, hist)；数据不足返回 None
    """
    return _last(macd_series(closes, fast, slow, signal))


def kdj(candles: list[Candle], period: int = 9) -> tuple[Decimal, Decimal, Decimal] | None:
    """最新一根 KDJ (k, d, j)；不足 period 根返回 None。

    参数：
        candles: list[Candle]，按时间排序的 K 线序列
        period: int，指标计算周期
    返回：
        tuple[Decimal, Decimal, Decimal] | None，最新一根 KDJ (k, d, j)；不足 period 根返回 None
    """
    return _last(kdj_series(candles, period))


def roc(closes: list[Decimal], period: int = 10) -> Decimal | None:
    """最新一根 ROC；不足 period+1 根返回 None。

    参数：
        closes: list[Decimal]，收盘价序列
        period: int，指标计算周期
    返回：
        Decimal | None，最新一根 ROC；不足 period+1 根返回 None
    """
    return _last(roc_series(closes, period))


def atr(candles: list[Candle], period: int = 14) -> Decimal | None:
    """最新一根 ATR（Wilder）；不足 period 根返回 None。

    参数：
        candles: list[Candle]，按时间排序的 K 线序列
        period: int，指标计算周期
    返回：
        Decimal | None，最新一根 ATR（Wilder）；不足 period 根返回 None
    """
    return _last(atr_series(candles, period))


def bollinger(
    closes: list[Decimal], period: int = 20, mult: Decimal = Decimal(2)
) -> tuple[Decimal, Decimal, Decimal] | None:
    """最新一根布林带 (upper, mid, lower)；不足 period 根返回 None。

    参数：
        closes: list[Decimal]，收盘价序列
        period: int，指标计算周期
        mult: Decimal，标准差带宽倍数
    返回：
        tuple[Decimal, Decimal, Decimal] | None，最新一根布林带 (upper, mid, lower)；不足 period 根返回 None
    """
    return _last(bollinger_series(closes, period, mult))


def vol_ratio(candles: list[Candle], period: int = 20) -> Decimal | None:
    """最新一根量比；不足 period 根或均量为 0 返回 None。

    参数：
        candles: list[Candle]，按时间排序的 K 线序列
        period: int，指标计算周期
    返回：
        Decimal | None，最新一根量比；不足 period 根或均量为 0 返回 None
    """
    return _last(vol_ratio_series(candles, period))


def obv(candles: list[Candle]) -> Decimal | None:
    """最新一根 OBV 累计值；空序列返回 None。

    参数：
        candles: list[Candle]，按时间排序的 K 线序列
    返回：
        Decimal | None，最新一根 OBV 累计值；空序列返回 None
    """
    return _last(obv_series(candles))
