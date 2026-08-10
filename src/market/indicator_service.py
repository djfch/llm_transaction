"""指标服务：注册表驱动，把 K 线缓存 / OI 缓存组装成面板、逐根序列与单行文本。

服务只依赖 candle_cache / oi_cache 的鸭子类型接口（见模块内 Protocol），不 import
具体网关或行情实现；面板中的 shortlist 字段由调用方补充，服务本身不读短名单。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..gateway.base import Candle
from . import indicators as ind

# 一次取用的历史根数（缓存有多少用多少），覆盖注册表最大 min_candles（ema50=50）
HISTORY_LIMIT = 200


@dataclass(frozen=True)
class IndicatorDef:
    """指标定义。label 为展示名（含中文释义）；kind 为展示分组。"""

    label: str
    kind: str  # overlay=主图叠加 / pane=副图 / scalar=单值
    fields: tuple[str, ...]  # 子字段名（单字段指标与 key 同名）
    min_candles: int  # 最少 K 线根数，不足则该指标输出 None


REGISTRY: dict[str, IndicatorDef] = {
    "ema9": IndicatorDef("EMA9(指数均线)", "overlay", ("ema9",), 9),
    "ema20": IndicatorDef("EMA20(指数均线)", "overlay", ("ema20",), 20),
    "ema50": IndicatorDef("EMA50(指数均线)", "overlay", ("ema50",), 50),
    "macd": IndicatorDef("MACD(异同均线)", "pane", ("dif", "dea", "hist"), 34),
    "rsi7": IndicatorDef("RSI7(相对强弱)", "pane", ("rsi7",), 8),
    "rsi14": IndicatorDef("RSI14(相对强弱)", "pane", ("rsi14",), 15),
    "kdj": IndicatorDef("KDJ(随机指标)", "pane", ("k", "d", "j"), 9),
    "roc10": IndicatorDef("ROC10(变动率)", "pane", ("roc10",), 11),
    "atr14": IndicatorDef("ATR14(平均真实波幅)", "scalar", ("atr14",), 14),
    "boll": IndicatorDef("BOLL(布林带)", "overlay", ("upper", "mid", "lower"), 20),
    "vol_ratio": IndicatorDef("量比(相对20根均量)", "scalar", ("vol_ratio",), 20),
    "obv": IndicatorDef("OBV(能量潮)", "pane", ("obv",), 2),
    "oi": IndicatorDef("持仓量", "scalar", ("oi",), 0),  # 不来自 K 线，来自 OI 缓存
}


def _closes(candles: list[Candle]) -> list[Decimal]:
    """提取每根 K 线的收盘价，组成与原列表同序的序列。

    参数：
        candles: list[Candle]，K 线列表（按时间升序）

    返回：
        list[Decimal]：各根 K 线的收盘价序列
    """
    return [c.c for c in candles]


# key -> 序列计算函数（一次取数逐根计算；OBV/EMA 类内部增量推进，不做全量重算）
_SERIES_FUNCS: dict[str, Callable[[list[Candle]], list]] = {
    "ema9": lambda cs: ind.ema_series(_closes(cs), 9),
    "ema20": lambda cs: ind.ema_series(_closes(cs), 20),
    "ema50": lambda cs: ind.ema_series(_closes(cs), 50),
    "macd": lambda cs: ind.macd_series(_closes(cs)),
    "rsi7": lambda cs: ind.rsi_series(_closes(cs), 7),
    "rsi14": lambda cs: ind.rsi_series(_closes(cs), 14),
    "kdj": lambda cs: ind.kdj_series(cs),
    "roc10": lambda cs: ind.roc_series(_closes(cs), 10),
    "atr14": lambda cs: ind.atr_series(cs, 14),
    "boll": lambda cs: ind.bollinger_series(_closes(cs)),
    "vol_ratio": lambda cs: ind.vol_ratio_series(cs, 20),
    "obv": lambda cs: ind.obv_series(cs),
}


class CandleCacheLike(Protocol):
    """K 线缓存鸭子类型：与 CandleCache.get_recent 同签名。"""

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        """读取合约某周期最近 n 根 K 线。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，K 线周期（如 15m、4h）
            n: int，期望读取的根数；缓存不足时返回现有全部

        返回：
            list[Candle]：按时间升序的 K 线列表；无缓存数据时为空列表
        """
        ...


class OiCacheLike(Protocol):
    """OI 缓存鸭子类型：与 OpenInterestCache.get 同签名。"""

    def get(self, contract: str) -> Decimal | None:
        """读取合约最新缓存的持仓量。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            Decimal | None：持仓量张数；从未拉取成功或数据源不支持时返回 None
        """
        ...


def _defn(key: str) -> IndicatorDef:
    """取指标定义；未知 key 抛 ValueError（防拼写错误静默产出空数据）。

    参数：
        key: str，指标键

    返回：
        IndicatorDef，取指标定义；未知 key 抛 ValueError（防拼写错误静默产出空数据）

    异常：
        ValueError，指标键不在注册表中时抛出
    """
    try:
        return REGISTRY[key]
    except KeyError:
        raise ValueError(f"未知指标: {key!r}（可选: {', '.join(REGISTRY)}）") from None


def _fmt_values(defn: IndicatorDef, item: object) -> dict[str, str | None]:
    """把序列末项（Decimal 或多字段 tuple）格式化为 {field: str 或 None}。

    参数：
        defn: IndicatorDef，指标定义
        item: object，提供商响应中的工具调用项

    返回：
        dict[str, str | None]，字段名到格式化字符串或无数据值的映射
    """
    if item is None:
        return {f: None for f in defn.fields}
    if len(defn.fields) == 1:
        return {defn.fields[0]: str(item)}
    return {f: str(v) for f, v in zip(defn.fields, item)}


class IndicatorService:
    """指标计算服务：一次取历史逐根计算，输出当前面板 / 对齐序列 / 单行文本。"""

    def __init__(self, candle_cache: CandleCacheLike, oi_cache: OiCacheLike) -> None:
        """注入 K 线缓存与持仓量缓存，组装指标计算服务。

        参数：
            candle_cache: CandleCacheLike，K 线缓存（经 get_recent 提供历史 K 线）
            oi_cache: OiCacheLike，持仓量缓存（经 get 提供最新持仓量）

        返回：
            None，把两个缓存引用保存为实例属性
        """
        self._candle_cache = candle_cache
        self._oi_cache = oi_cache

    def full_panel(self, contract: str, interval: str) -> dict:
        """全部注册指标的当前值；shortlist 恒置 None，由调用方补充。

        参数：
            contract: str，合约标识
            interval: str，K 线周期

        返回：
            dict，全部注册指标的当前值；shortlist 恒置 None，由调用方补充
        """
        candles = self._candle_cache.get_recent(contract, interval, HISTORY_LIMIT)
        return {
            "contract": contract,
            "interval": interval,
            "time": candles[-1].t if candles else None,
            "indicators": {
                key: {
                    "label": defn.label,
                    "kind": defn.kind,
                    "values": self._current_values(key, defn, contract, candles),
                }
                for key, defn in REGISTRY.items()
            },
            "shortlist": None,
        }

    def series(self, contract: str, interval: str, keys: list[str], limit: int) -> dict:
        """每个 key 每个 field 的逐根序列，与最后 limit 根 K 线时间对齐。

        参数：
            contract: str，合约标识
            interval: str，K 线周期
            keys: list[str]，需要查询的指标键列表
            limit: int，返回记录数量上限

        返回：
            dict，每个 key 每个 field 的逐根序列，与最后 limit 根 K 线时间对齐

        异常：
            ValueError，limit 小于 1 时抛出
        """
        if limit < 1:
            raise ValueError("limit 必须 ≥ 1")
        # 取数深度 = max(默认历史, limit + 暖机余量)：大窗口（如 15m×700）也能对齐，
        # +60 覆盖注册表最大 min_candles（ema50=50）的暖机段，窗口前段指标不为空
        candles = self._candle_cache.get_recent(contract, interval, max(HISTORY_LIMIT, limit + 60))
        tail = candles[-limit:]
        offset = len(candles) - len(tail)
        result: dict[str, dict] = {}
        for key in keys:
            defn = _defn(key)
            if key == "oi":  # OI 无 K 线序列：只给 current 值，fields 为空
                result[key] = self._oi_entry(defn, contract)
                continue
            full = _SERIES_FUNCS[key](candles)
            fields = {
                field: [
                    {"time": c.t, "value": v}
                    for c, v in zip(tail, self._field_seq(defn, fi, full)[offset:])
                ]
                for fi, field in enumerate(defn.fields)
            }
            result[key] = {"label": defn.label, "kind": defn.kind, "fields": fields}
        return {"contract": contract, "interval": interval, "series": result}

    def shortlist_line(self, contract: str, interval: str, keys: list[str]) -> str:
        """紧凑单行中文文本（供 LLM 上下文直接嵌入）；无 K 线时降级提示。

        参数：
            contract: str，合约标识
            interval: str，K 线周期
            keys: list[str]，需要查询的指标键列表

        返回：
            str，紧凑单行中文文本（供 LLM 上下文直接嵌入）；无 K 线时降级提示
        """
        candles = self._candle_cache.get_recent(contract, interval, HISTORY_LIMIT)
        if not candles:
            return f"{contract} 指标({interval}): 无K线数据"
        parts = [self._short_item(key, contract, candles) for key in keys]
        return f"{contract} 指标({interval}): " + ", ".join(parts)

    def _current_values(
        self, key: str, defn: IndicatorDef, contract: str, candles: list[Candle]
    ) -> dict[str, str | None]:
        """单指标当前值：oi 走缓存；K 线指标不足 min_candles 时全字段 None。

        参数：
            key: str，指标键
            defn: IndicatorDef，指标定义
            contract: str，合约标识
            candles: list[Candle]，按时间升序的 K 线序列

        返回：
            dict[str, str | None]，单指标当前值：oi 走缓存；K 线指标不足 min_candles 时全字段 None
        """
        if key == "oi":
            value = self._oi_cache.get(contract)
            return {"oi": None if value is None else str(value)}
        if len(candles) < defn.min_candles:
            return {f: None for f in defn.fields}
        seq = _SERIES_FUNCS[key](candles)
        return _fmt_values(defn, seq[-1] if seq else None)

    @staticmethod
    def _field_seq(defn: IndicatorDef, fi: int, full: list) -> list[str | None]:
        """逐根序列取第 fi 个子字段并 str 化（单字段指标直接 str 化）。

        参数：
            defn: IndicatorDef，指标定义
            fi: int，多字段指标的字段下标
            full: list，指标计算得到的完整序列

        返回：
            list[str | None]，逐根序列取第 fi 个子字段并 str 化（单字段指标直接 str 化）
        """
        if len(defn.fields) == 1:
            return [None if v is None else str(v) for v in full]
        return [None if item is None else str(item[fi]) for item in full]

    def _oi_entry(self, defn: IndicatorDef, contract: str) -> dict:
        """series 中 oi 的降级形状：fields 为空，只给 current。

        参数：
            defn: IndicatorDef，指标定义
            contract: str，合约标识

        返回：
            dict，仅含 current 持仓量且 fields 为空的序列项
        """
        value = self._oi_cache.get(contract)
        return {
            "label": defn.label,
            "kind": defn.kind,
            "fields": {},
            "current": None if value is None else str(value),
        }

    def _short_item(self, key: str, contract: str, candles: list[Candle]) -> str:
        """单行文本的单指标片段；数据不足的指标标 =无数据。

        参数：
            key: str，指标键
            contract: str，合约标识
            candles: list[Candle]，按时间升序的 K 线序列

        返回：
            str，单行文本的单指标片段；数据不足的指标标 =无数据
        """
        defn = _defn(key)
        name = defn.label.split("(")[0]
        values = self._current_values(key, defn, contract, candles)
        if all(v is None for v in values.values()):
            return f"{name}=无数据"
        if len(defn.fields) == 1:
            return f"{name}={values[defn.fields[0]]}"
        joined = "/".join(str(values[f]) for f in defn.fields)
        return f"{name}({'/'.join(defn.fields)})={joined}"
