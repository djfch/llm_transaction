"""整仓止盈止损更新：金额风险校验与无裸露窗口替换事务。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.agent.context import compute_equity
from src.agent.contract_specs import fresh_contract
from src.agent.tool_handlers import (
    ToolArgError,
    ToolDeps,
    ToolOutcome,
    _need_decimal,
    _need_str,
    _opt_decimal,
)
from src.agent.tool_trading import _validate_tpsl
from src.audit.logger import get_logger
from src.gateway.async_io import run_gateway_io
from src.gateway.base import Gateway, GatewayError, TpslOrder
from src.risk.stop_risk import planned_stop_loss

logger = get_logger(__name__)


@dataclass
class _TpslSwapResult:
    """TPSL 整组替换的执行结果与失败上下文。"""

    stage: str
    error: str = ""
    rollback_failed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    old_total: int = 0


def _swap_tpsl_group(
    gateway: Gateway,
    contract: str,
    direction: int,
    expected_size: Decimal,
    requested: list[TpslOrder],
) -> _TpslSwapResult:
    """完整创建新保护单组后撤销旧组，并在事务开始时重核持仓。

    参数：
        gateway: Gateway，交易网关
        contract: str，合约标识
        direction: int，持仓方向（1 多 / -1 空）
        expected_size: Decimal，风险校验时读取的整仓张数
        requested: list[TpslOrder]，待创建的完整新保护组

    返回：
        _TpslSwapResult：阶段标识与失败上下文
    """
    pos = next((item for item in gateway.list_positions() if item.contract == contract), None)
    if (
        pos is None
        or pos.size == 0
        or (pos.size > 0) != (direction > 0)
        or pos.size != expected_size
    ):
        return _TpslSwapResult("position_changed")
    old = [item for item in gateway.list_tpsl_orders(contract) if item.direction == direction]
    created: list[TpslOrder] = []
    try:
        for item in requested:
            created.append(gateway.create_tpsl_order(item))
    except GatewayError as exc:
        if exc.label == "TPSL_STATE_UNKNOWN":
            return _TpslSwapResult("create_unknown", str(exc))
        rollback_failed = _rollback_created(gateway, created)
        return _TpslSwapResult("create_failed", str(exc), rollback_failed=rollback_failed)
    return _cancel_old_group(gateway, old, created)


def _rollback_created(gateway: Gateway, created: list[TpslOrder]) -> list[str]:
    """撤销创建新组过程中已经成功创建的保护单。

    参数：
        gateway: Gateway，交易网关
        created: list[TpslOrder]，已经创建成功的新保护单

    返回：
        list[str]：回滚失败的保护单 ID
    """
    failed: list[str] = []
    for item in created:
        try:
            gateway.cancel_tpsl_order(item.id)
        except GatewayError:
            logger.exception("止盈止损回滚失败 id=%s", item.id)
            failed.append(item.id)
    return failed


def _cancel_old_group(
    gateway: Gateway, old: list[TpslOrder], created: list[TpslOrder]
) -> _TpslSwapResult:
    """在新保护组完整创建后撤销旧保护组。

    参数：
        gateway: Gateway，交易网关
        old: list[TpslOrder]，待撤销的旧保护单
        created: list[TpslOrder]，已完整创建的新保护单

    返回：
        _TpslSwapResult：撤销阶段结果
    """
    cancelled: list[str] = []
    try:
        for item in old:
            gateway.cancel_tpsl_order(item.id)
            cancelled.append(item.id)
    except GatewayError as exc:
        if exc.label == "TPSL_STATE_UNKNOWN":
            return _TpslSwapResult("cancel_unknown", str(exc))
        return _TpslSwapResult("cancel_partial", str(exc), cancelled=cancelled, old_total=len(old))
    return _TpslSwapResult("ok", cancelled=cancelled, old_total=len(created))


async def update_tpsl(deps: ToolDeps, args: dict) -> ToolOutcome:
    """校验整仓止损金额，并以完整新组替换旧保护单组。

    参数：
        deps: ToolDeps，工具依赖
        args: dict，含合约、必填止损和可选止盈

    返回：
        ToolOutcome：更新结果、风险拒绝或需要人工核对的失败态

    异常：
        ToolArgError：当前无持仓、权益非正或止盈止损方向非法时抛出
    """
    contract = _need_str(args, "contract")
    stop_loss = _need_decimal(args, "stop_loss_price")
    take_profit = _opt_decimal(args, "take_profit_price")
    positions = await run_gateway_io(deps.gateway.list_positions)
    pos = next((item for item in positions if item.contract == contract), None)
    if pos is None or pos.size == 0:
        raise ToolArgError("当前无持仓，无法设置整仓止盈止损")
    direction = 1 if pos.size > 0 else -1
    meta = await fresh_contract(deps, contract)
    _validate_tpsl(
        direction=direction,
        mark_price=meta.mark_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    account = await run_gateway_io(deps.gateway.get_account)
    equity = compute_equity(account, positions)
    if equity <= 0:
        raise ToolArgError("账户权益非正，无法估算整仓止损风险")
    current_orders = await run_gateway_io(deps.gateway.list_tpsl_orders, contract)
    current_stop = next(
        (
            item.trigger_price
            for item in current_orders
            if item.direction == direction and item.kind == "stop_loss"
        ),
        None,
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
    requested = _requested_group(contract, direction, stop_loss, take_profit)
    swap = await run_gateway_io(
        _swap_tpsl_group,
        deps.gateway,
        contract,
        direction,
        pos.size,
        requested,
        mutation=True,
    )
    failure = _swap_failure_outcome(swap)
    if failure is not None:
        return failure
    return _success_outcome(deps, stop_loss, take_profit, new_risk, current_stop, equity)


def _requested_group(
    contract: str, direction: int, stop_loss: Decimal, take_profit: Decimal | None
) -> list[TpslOrder]:
    """构造完整的新整仓保护单组。

    参数：
        contract: str，合约标识
        direction: int，持仓方向
        stop_loss: Decimal，止损触发价
        take_profit: Decimal | None，可选止盈触发价

    返回：
        list[TpslOrder]：至少包含止损的新保护单组
    """
    requested = [
        TpslOrder(
            id="", contract=contract, direction=direction, kind="stop_loss", trigger_price=stop_loss
        )
    ]
    if take_profit is not None:
        requested.append(
            TpslOrder(
                id="",
                contract=contract,
                direction=direction,
                kind="take_profit",
                trigger_price=take_profit,
            )
        )
    return requested


def _swap_failure_outcome(swap: _TpslSwapResult) -> ToolOutcome | None:
    """把保护组交换失败阶段转换为模型可执行的结果文本。

    参数：
        swap: _TpslSwapResult，交换事务结果

    返回：
        ToolOutcome | None：失败结果；交换成功时返回 None
    """
    if swap.stage == "ok":
        return None
    if swap.stage == "position_changed":
        return ToolOutcome("止盈止损未更新：持仓张数或方向已变化，请重新计算")
    if swap.stage == "create_unknown":
        return ToolOutcome(f"更新状态未知；旧保护单未撤销，请人工核对且不要重试：{swap.error}")
    if swap.stage == "create_failed":
        if swap.rollback_failed:
            ids = ", ".join(swap.rollback_failed)
            return ToolOutcome(f"更新失败且新保护单 {ids} 回滚失败，请人工核对：{swap.error}")
        return ToolOutcome(f"更新失败；新保护单已回滚，旧保护单未变更：{swap.error}")
    if swap.stage == "cancel_unknown":
        return ToolOutcome(f"新保护已设置，撤销旧保护状态未知，请人工核对且不要重试：{swap.error}")
    return ToolOutcome(
        f"新保护已设置，但旧保护仅撤销 {len(swap.cancelled)}/{swap.old_total} 个，请人工核对：{swap.error}"
    )


def _success_outcome(
    deps: ToolDeps,
    stop_loss: Decimal,
    take_profit: Decimal | None,
    new_risk: Decimal,
    current_stop: Decimal | None,
    equity: Decimal,
) -> ToolOutcome:
    """组装保护单更新成功后的风险回显和首次保护警告。

    参数：
        deps: ToolDeps，工具依赖
        stop_loss: Decimal，新止损价
        take_profit: Decimal | None，新止盈价
        new_risk: Decimal，新整仓计划止损估算
        current_stop: Decimal | None，修改前止损价
        equity: Decimal，账户权益

    返回：
        ToolOutcome：允许结果与风险估算
    """
    text = f"止损已更新为 {stop_loss}；整仓计划止损估算 {new_risk} USDT" + (
        f"；止盈已更新为 {take_profit}" if take_profit else "；止盈未设置"
    )
    limit = Decimal(str(deps.risk_config.max_position_stop_risk_pct)) * equity
    if current_stop is None and new_risk > limit:
        text += "；警告：首次保护止损仍超过风险上限，请继续收紧"
    return ToolOutcome(text, "allow")
