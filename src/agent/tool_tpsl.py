"""整仓止盈止损更新：金额风险校验与无裸露窗口替换事务。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.agent.tool_handlers import (
    ToolArgError,
    ToolDeps,
    ToolOutcome,
    _need_decimal,
    _need_str,
    _opt_decimal,
)
from src.agent.tool_tpsl_risk import TpslRiskAssessment, assess_tpsl_risk
from src.audit.logger import get_logger
from src.gateway.async_io import run_gateway_io
from src.gateway.base import Gateway, GatewayError, TpslOrder

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
    expected_group: tuple[tuple, ...],
    requested: list[TpslOrder],
) -> _TpslSwapResult:
    """完整创建新保护单组后撤销旧组，并在事务开始时重核持仓。

    参数：
        gateway: Gateway，交易网关
        contract: str，合约标识
        direction: int，持仓方向（1 多 / -1 空）
        expected_size: Decimal，风险校验时读取的整仓张数
        expected_group: tuple[tuple, ...]，风险校验时同方向保护单指纹
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
    if _tpsl_group_fingerprint(old, direction) != expected_group:
        return _TpslSwapResult("protection_changed")
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


def _tpsl_group_fingerprint(orders: list[TpslOrder], direction: int) -> tuple[tuple, ...]:
    """提取指定持仓方向的完整保护单集合指纹。

    参数：
        orders: list[TpslOrder]，保护单快照
        direction: int，持仓方向（1 多 / -1 空）

    返回：
        tuple[tuple, ...]：按订单 ID 排序的类型、方向与触发价指纹
    """
    return tuple(
        sorted(
            (item.id, item.contract, item.direction, item.kind, item.trigger_price)
            for item in orders
            if item.direction == direction
        )
    )


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
    current_orders = await run_gateway_io(deps.gateway.list_tpsl_orders, contract)
    assessment = await assess_tpsl_risk(
        deps=deps,
        contract=contract,
        positions=positions,
        pos=pos,
        direction=direction,
        stop_loss=stop_loss,
        take_profit=take_profit,
        current_orders=current_orders,
    )
    if isinstance(assessment, ToolOutcome):
        return assessment
    requested = _requested_group(contract, direction, stop_loss, take_profit)
    expected_group = _tpsl_group_fingerprint(current_orders, direction)
    swap = await run_gateway_io(
        _swap_tpsl_group,
        deps.gateway,
        contract,
        direction,
        pos.size,
        expected_group,
        requested,
        mutation=True,
    )
    failure = _swap_failure_outcome(swap)
    if failure is not None:
        return failure
    return _success_outcome(deps, stop_loss, take_profit, assessment)


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
    if swap.stage == "protection_changed":
        return ToolOutcome("止盈止损未更新：保护单集合在校验期间发生变化，请重新计算")
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
    assessment: TpslRiskAssessment,
) -> ToolOutcome:
    """组装保护单更新成功后的风险回显和首次保护警告。

    参数：
        deps: ToolDeps，工具依赖
        stop_loss: Decimal，新止损价
        take_profit: Decimal | None，新止盈价
        assessment: TpslRiskAssessment，风险估算与降级说明

    返回：
        ToolOutcome：允许结果与风险估算
    """
    estimate = (
        f"整仓计划止损估算 {assessment.new_risk} USDT"
        if assessment.new_risk is not None
        else "整仓计划止损估算因规格不可用暂不可得"
    )
    text = f"止损已更新为 {stop_loss}；{estimate}" + (
        f"；止盈已更新为 {take_profit}" if take_profit is not None else "；止盈未设置"
    )
    if assessment.warning:
        text += f"；警告：{assessment.warning}"
    limit = Decimal(str(deps.risk_config.max_position_stop_risk_pct)) * assessment.equity
    if (
        assessment.current_stop is None
        and assessment.new_risk is not None
        and assessment.new_risk > limit
    ):
        text += "；警告：首次保护止损仍超过风险上限，请继续收紧"
    return ToolOutcome(text, "allow")
