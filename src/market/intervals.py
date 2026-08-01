"""Gate 永续合约 K 线周期全集（单一数据源）。

依据：gate_api SDK ``list_futures_candlesticks`` docstring（1w=自然周、7d=Unix 纪元对齐、
30d=自然月）与一期实现计划附录的核实记录（10s/1m/5m/15m/30m/1h/4h/8h/1d/7d）。

bootstrap 订阅回补（CANDLE_INTERVALS）、LLM 工具 schema 枚举与参数校验共用本列表；
要增删周期只改这里。不变量「LLM 可请求周期 ⊆ 缓存订阅回补周期」由
tests/test_bootstrap_market.py::test_candle_intervals_single_source 守护。
"""

from __future__ import annotations

GATE_CANDLE_INTERVALS: tuple[str, ...] = (
    "10s",
    "1m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "7d",
    "30d",
    "1w",
)

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def interval_seconds(interval: str) -> int:
    """周期字符串转秒数（如 4h→14400、1w→604800），供判断 K 线窗口是否已结束。"""
    if interval not in GATE_CANDLE_INTERVALS:
        raise ValueError(f"非法 K 线周期: {interval!r}")
    return int(interval[:-1]) * _UNIT_SECONDS[interval[-1]]
