"""PaperGateway 的内部数据结构与共享模型转换。"""

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
    """把模拟账户持仓转换为网关统一持仓模型并估算强平价格与未实现盈亏。

    参数：
        pos: PaperPosition，模拟账户内部持仓
        contract_meta: Contract，合约乘数与资金费率等元数据
        mark: Decimal，当前标记价格
        maintenance_rate: Decimal，维持保证金率
        account: PaperAccount，提供未实现盈亏计算的模拟账户

    返回：
        Position，供网关接口返回的标准持仓快照
    """
    quanto = contract_meta.quanto_multiplier
    is_cross = pos.margin_mode == "cross"
    return Position(
        contract=pos.contract,
        size=pos.size,
        entry_price=pos.entry_price,
        mark_price=mark,
        liq_price=estimate_liq_price(pos, quanto, maintenance_rate),
        leverage=Decimal(0) if is_cross else pos.leverage,  # 与 Gate 口径一致：0=全仓
        margin=pos.margin,
        unrealised_pnl=account.unrealised(pos.contract, mark, quanto),
        margin_mode=pos.margin_mode,
        cross_leverage_limit=pos.leverage if is_cross else None,
    )


def synth_ticker(name: str, snap: PriceSnap, contract_meta: Contract | None) -> Ticker:
    """在无外部行情提供器时，用当前价格快照合成标准行情对象。

    参数：
        name: str，合约名称
        snap: PriceSnap，当前标记价与买卖价快照
        contract_meta: Contract | None，可选合约元数据，用于读取资金费率

    返回：
        Ticker，以快照价格填充 24 小时字段的标准行情对象
    """
    return Ticker(
        contract=name,
        last=snap.mark,
        mark_price=snap.mark,
        funding_rate=contract_meta.funding_rate if contract_meta else Decimal(0),
        high_24h=snap.mark,
        low_24h=snap.mark,
        change_percentage=Decimal(0),
    )


def position_of(gw, pos) -> Position:
    """把内部持仓记录转换为对外 Position（按最新标记价估值）。

    自 engine.py 拆出（文件体量门禁）：估值依赖合约元数据、标记价与维持
    保证金率，均经网关实例读取。

    参数：
        gw: PaperGateway，模拟网关实例
        pos: PaperAccount 内部持仓记录

    返回：
        Position：对外持仓对象
    """
    c = gw.get_contract(pos.contract)
    mark = gw._mark(pos.contract, pos.entry_price)
    return to_position(pos, c, mark, gw._maint(pos.contract), gw.account)
