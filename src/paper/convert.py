"""PaperGateway 的内部数据结构与模型转换（从 engine 拆出以控制单文件行数）。"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from ..gateway.base import Contract, Position, Ticker
from .account import PaperAccount, PaperPosition
from .liquidation import estimate_liq_price


class PriceSnap(BaseModel):
    """最新行情快照。未注入盘口时 bid/ask 取 mark。"""

    mark: Decimal
    bid: Decimal
    ask: Decimal


class RestingOrder(BaseModel):
    """挂着的限价单（只全成或不成，不做部分成交）。"""

    id: str
    contract: str
    size: Decimal  # 正买负卖
    price: Decimal
    reduce_only: bool = False
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    text: str = ""


def to_position(
    pos: PaperPosition,
    contract_meta: Contract,
    mark: Decimal,
    maintenance_rate: Decimal,
    account: PaperAccount,
) -> Position:
    """内部持仓转为 Gateway 接口的 Position 模型（liq_price 为简化估算）。"""
    quanto = contract_meta.quanto_multiplier
    return Position(
        contract=pos.contract,
        size=pos.size,
        entry_price=pos.entry_price,
        mark_price=mark,
        liq_price=estimate_liq_price(pos, quanto, maintenance_rate),
        leverage=pos.leverage,
        margin=pos.margin,
        unrealised_pnl=account.unrealised(pos.contract, mark, quanto),
    )


def synth_ticker(name: str, snap: PriceSnap, contract_meta: Contract | None) -> Ticker:
    """无外部 ticker provider 时，由行情快照合成 ticker（24h 高低/涨跌取快照值）。"""
    return Ticker(
        contract=name,
        last=snap.mark,
        mark_price=snap.mark,
        funding_rate=contract_meta.funding_rate if contract_meta else Decimal(0),
        high_24h=snap.mark,
        low_24h=snap.mark,
        change_percentage=Decimal(0),
    )
