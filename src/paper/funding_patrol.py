"""paper 模式资金费定时巡检（自 bootstrap 拆出：结算调度属 paper 域）。

funding.py 只做纯记账；本模块负责"到点结算"的时间调度：
每分钟巡检一次持仓合约，到达各自 funding_interval 即结算一次。
"""

from __future__ import annotations

import asyncio
import time

from src.gateway.base import Gateway
from src.paper.engine import PaperGateway


def settle_due_funding(
    gateway: PaperGateway, last_settled: dict[str, float], now: float
) -> list[str]:
    """对到达 funding_interval 的持仓合约结算一次资金费，返回本次结算的合约名。

    水线为 Unix 墙钟时间并与结算边界对齐：首次观察某合约不立即结算，只登记
    其所在周期的上一个边界，从下一个边界开始收取——重启后内存水线清零，若
    立即结算会把停机前已结过的费用重复收取（issue #75；宁可少收不可多收）。
    边界以 Unix 纪元取模计算，假设交易所资金费边界为 UTC 整点对齐（Gate
    8h 周期即 00:00/08:00/16:00 UTC）；若实际边界有相位偏移，仅结算时刻
    随之偏移，结算次数与金额不受影响。

    参数：
        gateway: PaperGateway，交易网关
        last_settled: dict[str, float]，合约到上次资金费结算边界的映射（Unix 秒）
        now: float，当前 Unix 时间戳

    返回：
        list[str]，对到达 funding_interval 的持仓合约结算一次资金费，返回本次结算的合约名
    """
    settled: list[str] = []
    for position in gateway.list_positions():
        contract = gateway.get_contract(position.contract)
        if contract.funding_interval <= 0:
            continue  # 非法周期（Gate 元数据兜底 0 值）：跳过，防取模零错误杀死巡检协程
        boundary = now - (now % contract.funding_interval)  # 当前所在周期的起点
        last = last_settled.get(position.contract)
        if last is None:
            # 首次观察：只登记边界不结算，下一边界到期才开始收（见 docstring）
            last_settled[position.contract] = boundary
            continue
        if now - last < contract.funding_interval:
            continue  # 未到该合约结算周期
        gateway.settle_funding(position.contract, contract.funding_rate)
        last_settled[position.contract] = boundary
        settled.append(position.contract)
    return settled


async def funding_loop(gateway: Gateway) -> None:
    """paper 模式资金费结算：按各合约 funding_interval 周期结算（Gate 惯例 8h）。

    参数：
        gateway: Gateway，交易所网关

    返回：
        None，paper 模式资金费结算：按各合约 funding_interval 周期结算（Gate 惯例 8h）
    """
    if not isinstance(gateway, PaperGateway):
        return
    last_settled: dict[str, float] = {}
    while True:
        await asyncio.sleep(60)  # 每分钟巡检是否到达各合约结算周期
        settle_due_funding(gateway, last_settled, time.time())
