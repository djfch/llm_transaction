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
    """对到达 funding_interval 的持仓合约结算一次资金费，返回本次结算的合约名。"""
    settled: list[str] = []
    for position in gateway.list_positions():
        contract = gateway.get_contract(position.contract)
        last = last_settled.get(position.contract)
        if last is not None and now - last < contract.funding_interval:
            continue  # 未到该合约结算周期
        gateway.settle_funding(position.contract, contract.funding_rate)
        last_settled[position.contract] = now
        settled.append(position.contract)
    return settled


async def funding_loop(gateway: Gateway) -> None:
    """paper 模式资金费结算：按各合约 funding_interval 周期结算（Gate 惯例 8h）。"""
    if not isinstance(gateway, PaperGateway):
        return
    last_settled: dict[str, float] = {}
    while True:
        await asyncio.sleep(60)  # 每分钟巡检是否到达各合约结算周期
        settle_due_funding(gateway, last_settled, time.monotonic())
