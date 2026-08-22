"""paper 行情读取（K 线 / ticker 合成）：自 engine.py 拆出以控制文件体量。

纯委托逻辑，无状态：candle_provider / ticker_provider 未注入时按内存快照降级。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.gateway.base import Candle, Ticker

from .convert import synth_ticker

if TYPE_CHECKING:
    from .engine import PaperGateway


def read_candlesticks(
    gw: "PaperGateway",
    contract: str,
    interval: str = "1m",
    limit: int | None = None,
    from_ts: int | None = None,
    to_ts: int | None = None,
) -> list[Candle]:
    """读取 K 线数据，委托给网关注入的 candle_provider；未注入时返回空列表。

    参数：
        gw: PaperGateway，模拟网关实例
        contract: str，合约名
        interval: str，K 线周期，默认 1m
        limit: int | None，返回条数；与 from/to 互斥
        from_ts: int | None，起始时间戳
        to_ts: int | None，结束时间戳

    返回：
        list[Candle]：K 线列表；未注入 provider 时为空列表

    异常：
        ValueError：limit 与 from/to 同时传入时抛出
    """
    if limit is not None and (from_ts is not None or to_ts is not None):
        raise ValueError("limit 与 from/to 互斥，不能同时传")
    if gw._candle_provider is None:
        return []
    return gw._candle_provider(contract, interval, limit, from_ts, to_ts)


def read_tickers(gw: "PaperGateway") -> list[Ticker]:
    """读取全部 ticker；优先走网关注入的 ticker_provider，否则由行情快照合成。

    参数：
        gw: PaperGateway，模拟网关实例

    返回：
        list[Ticker]：ticker 列表
    """
    if gw._ticker_provider is not None:
        return gw._ticker_provider()
    return [synth_ticker(n, s, gw._contracts.get(n)) for n, s in gw._snaps.items()]
