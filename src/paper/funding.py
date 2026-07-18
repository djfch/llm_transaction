"""资金费结算：rate 为正时多头付、空头收；rate 为负时反向。

由外部按合约 funding_interval 定时触发 PaperGateway.settle_funding，
本模块只负责纯记账，不关心时间调度。
"""

from __future__ import annotations

from decimal import Decimal

from .account import PaperAccount


def funding_cash_delta(
    size: Decimal, mark_price: Decimal, quanto: Decimal, rate: Decimal
) -> Decimal:
    """资金费引起的余额变化（负=支出）。多头付款 = rate × 名义价值。"""
    if size == 0:
        return Decimal(0)
    payment = rate * abs(size) * mark_price * quanto
    return -payment if size > 0 else payment


def settle_funding(
    account: PaperAccount, contract: str, rate: Decimal, mark_price: Decimal, quanto: Decimal
) -> Decimal:
    """对当前持仓结算一次资金费，返回余额变化；无持仓返回 0。"""
    pos = account.position(contract)
    if pos is None:
        return Decimal(0)
    delta = funding_cash_delta(pos.size, mark_price, quanto, rate)
    account.available += delta
    account.total_funding += delta
    return delta
