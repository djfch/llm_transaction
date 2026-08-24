"""止盈止损更新的整仓风险读取、降级与判定。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.agent.context import compute_equity
from src.agent.contract_specs import cached_contract, fresh_contract
from src.agent.tool_handlers import ToolArgError, ToolDeps, ToolOutcome
from src.agent.tool_trading import _validate_tpsl
from src.gateway.async_io import run_gateway_io
from src.gateway.base import Contract, GatewayError, Position, TpslOrder
from src.risk.stop_risk import planned_stop_loss


@dataclass(frozen=True)
class TpslRiskAssessment:
    """一次止损更新的风险估算与降级说明。"""

    current_stop: Decimal | None
    new_risk: Decimal | None
    equity: Decimal
    warning: str = ""


def effective_current_stop(orders: list[TpslOrder], direction: int) -> Decimal | None:
    """选择会最先保护当前持仓的最紧旧止损。

    参数：
        orders: list[TpslOrder]，当前合约全部保护单
        direction: int，持仓方向，1 多、-1 空

    返回：
        Decimal | None：多仓最高、空仓最低的旧止损；没有时返回 None
    """
    stops = [
        item.trigger_price
        for item in orders
        if item.direction == direction and item.kind == "stop_loss"
    ]
    if not stops:
        return None
    return max(stops) if direction > 0 else min(stops)


def _degraded_rejection(
    *,
    pos: Position,
    current_stop: Decimal | None,
    new_stop: Decimal,
) -> str | None:
    """规格不可用时只拒绝会扩大已有计划亏损的止损修改。

    参数：
        pos: Position，当前真实持仓
        current_stop: Decimal | None，当前最紧止损
        new_stop: Decimal，新止损价

    返回：
        str | None：扩大风险时的拒绝原因；首次保护或不扩大时返回 None
    """
    if current_stop is None:
        return None
    current_distance = planned_stop_loss(
        entry_price=pos.entry_price,
        stop_loss_price=current_stop,
        size=pos.size,
        multiplier=Decimal(1),
    )
    new_distance = planned_stop_loss(
        entry_price=pos.entry_price,
        stop_loss_price=new_stop,
        size=pos.size,
        multiplier=Decimal(1),
    )
    if new_distance <= current_distance:
        return None
    return "实时合约规格不可用，只允许首次补保护或不扩大当前整仓止损风险"


async def _risk_contract(deps: ToolDeps, contract: str) -> tuple[Contract | None, bool]:
    """优先读取实时规格，失败或下架时退回最近内存规格。

    参数：
        deps: ToolDeps，工具依赖
        contract: str，合约标识

    返回：
        tuple[Contract | None, bool]：可用规格与是否处于降级校验
    """
    try:
        return await fresh_contract(deps, contract), False
    except (GatewayError, ToolArgError):
        return await cached_contract(deps, contract), True


async def assess_tpsl_risk(
    *,
    deps: ToolDeps,
    contract: str,
    positions: list[Position],
    pos: Position,
    direction: int,
    stop_loss: Decimal,
    take_profit: Decimal | None,
    current_orders: list[TpslOrder],
) -> TpslRiskAssessment | ToolOutcome:
    """读取整仓风险并按正常或降级规则裁决止损更新。

    参数：
        deps: ToolDeps，工具依赖
        contract: str，合约标识
        positions: list[Position]，全部真实持仓
        pos: Position，目标合约真实持仓
        direction: int，持仓方向
        stop_loss: Decimal，新止损价
        take_profit: Decimal | None，可选新止盈价
        current_orders: list[TpslOrder]，当前保护单

    返回：
        TpslRiskAssessment | ToolOutcome：风险估算，或明确的风控拒绝

    异常：
        ToolArgError：权益非正或止盈止损方向非法时抛出
    """
    meta, degraded = await _risk_contract(deps, contract)
    mark = (
        pos.mark_price
        if degraded and pos.mark_price > 0
        else meta.mark_price
        if meta
        else pos.mark_price
    )
    _validate_tpsl(
        direction=direction,
        mark_price=mark,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    current_stop = effective_current_stop(current_orders, direction)
    if degraded:
        reason = _degraded_rejection(pos=pos, current_stop=current_stop, new_stop=stop_loss)
        if reason:
            return ToolOutcome(f"风控拒绝：{reason}", "deny", reason)
    account = await run_gateway_io(deps.gateway.get_account)
    equity = compute_equity(account, positions)
    if equity <= 0:
        raise ToolArgError("账户权益非正，无法估算整仓止损风险")
    if meta is None:
        return TpslRiskAssessment(
            current_stop=current_stop,
            new_risk=None,
            equity=equity,
            warning="实时与缓存规格不可用，已按止损方向确认本次未扩大风险",
        )
    new_risk = planned_stop_loss(
        entry_price=pos.entry_price,
        stop_loss_price=stop_loss,
        size=pos.size,
        multiplier=meta.quanto_multiplier,
    )
    current_risk = (
        planned_stop_loss(
            entry_price=pos.entry_price,
            stop_loss_price=current_stop,
            size=pos.size,
            multiplier=meta.quanto_multiplier,
        )
        if current_stop is not None
        else None
    )
    if not degraded:
        verdict = deps.risk_engine.check_stop_update(
            new_risk=new_risk,
            current_risk=current_risk,
            has_current_stop=current_stop is not None,
            equity=equity,
            config=deps.risk_config,
        )
        if not verdict.allowed:
            reason = "；".join(verdict.reasons)
            return ToolOutcome(f"风控拒绝：{reason}", "deny", reason)
    warning = "实时规格不可用，已使用当前持仓缓存规格且仅允许不扩大风险" if degraded else ""
    return TpslRiskAssessment(current_stop, new_risk, equity, warning)
