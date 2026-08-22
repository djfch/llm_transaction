"""维持保证金强平：保证金率低于维持保证金率时按标记价强制平仓。

保证金率 = (margin + 未实现盈亏) / 名义价值（逐仓简化模型）。
强平亏损以该仓保证金为限，剩余部分（如有）返还可用余额。
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from ..gateway.base import PositionNotFound
from .account import FillRecord, PaperAccount, PaperPosition


class LiquidationEvent(BaseModel):
    """一次强平记录。loss 为亏损掉的保证金，returned_margin 为强平后返还部分。"""

    contract: str
    size: Decimal  # 被强平的仓位（正多负空）
    mark_price: Decimal
    loss: Decimal
    returned_margin: Decimal


def unrealised_pnl(pos: PaperPosition, mark_price: Decimal, quanto: Decimal) -> Decimal:
    """按标记价计算持仓的未实现盈亏（多头涨为盈、空头跌为盈）。

    参数：
        pos: PaperPosition，持仓对象（读取其开仓均价与持仓张数）
        mark_price: Decimal，当前标记价
        quanto: Decimal，合约乘数（每张合约对应的标的数量）

    返回：
        Decimal：未实现盈亏金额；持仓为 0 时按空头方向计算但结果恒为 0
    """
    direction = Decimal(1) if pos.size > 0 else Decimal(-1)
    return (mark_price - pos.entry_price) * abs(pos.size) * quanto * direction


def margin_ratio(pos: PaperPosition, mark_price: Decimal, quanto: Decimal) -> Decimal:
    """保证金率；名义价值为 0 时视为无限大（不会触发强平）。

    参数：
        pos: PaperPosition，当前合约持仓；无持仓时为 None
        mark_price: Decimal，当前标记价格
        quanto: Decimal，合约乘数
    返回：
        Decimal，保证金率；名义价值为 0 时视为无限大（不会触发强平）
    """
    notional = abs(pos.size) * mark_price * quanto
    if notional == 0:
        return Decimal("Infinity")
    return (pos.margin + unrealised_pnl(pos, mark_price, quanto)) / notional


def should_liquidate(
    pos: PaperPosition, mark_price: Decimal, quanto: Decimal, maintenance_rate: Decimal
) -> bool:
    """判断持仓是否触及强平条件：有持仓且保证金率已低于维持保证金率。

    参数：
        pos: PaperPosition，持仓对象（无持仓时直接返回 False）
        mark_price: Decimal，当前标记价
        quanto: Decimal，合约乘数（每张合约对应的标的数量）
        maintenance_rate: Decimal，维持保证金率阈值

    返回：
        bool：True 表示应触发强平，False 表示保证金仍充足或没有持仓
    """
    return pos.size != 0 and margin_ratio(pos, mark_price, quanto) < maintenance_rate


def estimate_liq_price(pos: PaperPosition, quanto: Decimal, maintenance_rate: Decimal) -> Decimal:
    """强平价估算（逐仓简化线性公式，仅估值，不作强平依据）。

    参数：
        pos: PaperPosition，当前合约持仓；无持仓时为 None
        quanto: Decimal，合约乘数
        maintenance_rate: Decimal，维持保证金率
    返回：
        Decimal，强平价估算（逐仓简化线性公式，仅估值，不作强平依据）
    """
    if pos.size == 0 or pos.entry_price == 0:
        return Decimal(0)
    per = pos.margin / (abs(pos.size) * quanto)  # 每张合约占用的保证金
    if pos.size > 0:
        price = (pos.entry_price - per) / (1 - maintenance_rate)
    else:
        price = (pos.entry_price + per) / (1 + maintenance_rate)
    return max(price, Decimal(0))


def liquidate(
    account: PaperAccount, contract: str, mark_price: Decimal, quanto: Decimal
) -> LiquidationEvent:
    """按 mark_price 强平：结算盈亏（亏损以保证金为限），持仓清零并记录事件。

    参数：
        account: PaperAccount，账户快照
        contract: str，合约标识
        mark_price: Decimal，当前标记价格
        quanto: Decimal，合约乘数
    返回：
        LiquidationEvent，按 mark_price 强平：结算盈亏（亏损以保证金为限），持仓清零并记录事件
    异常：
        PositionNotFound，目标合约不存在可强平持仓时抛出
    """
    pos = account.position(contract)
    if pos is None:
        raise PositionNotFound(f"持仓不存在: {contract}", label="POSITION_NOT_FOUND")
    pnl = unrealised_pnl(pos, mark_price, quanto)
    returned = max(pos.margin + pnl, Decimal(0))
    realized = returned - pos.margin  # 统计与余额同口径：亏损以保证金为限
    event = LiquidationEvent(
        contract=contract,
        size=pos.size,
        mark_price=mark_price,
        loss=pos.margin - returned,
        returned_margin=returned,
    )
    account.fill_seq += 1
    account.available += returned
    account.total_realized += realized
    account.fills.append(
        FillRecord(
            order_id="liquidation",
            contract=contract,
            size=-pos.size,
            price=mark_price,
            fee=Decimal(0),
            realized_pnl=realized,
            maker=False,
            is_close=True,  # 强平属平仓成交
            trade_id=f"liquidation:{account.fill_seq}",
        )
    )
    pos.size = Decimal(0)
    pos.entry_price = Decimal(0)
    pos.margin = Decimal(0)
    return event
