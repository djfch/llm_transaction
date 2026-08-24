"""未成交订单价格修改：保持金额不变并重新执行完整风险校验。"""

from __future__ import annotations

from decimal import Decimal

from src.agent.contract_specs import fresh_contract
from src.agent.tool_handlers import ToolArgError, ToolDeps, ToolOutcome, _need_decimal, _need_str
from src.agent.tool_leverage import _amend_unless_close_intervened
from src.agent.tool_order_reduction import reduction_risk_check
from src.agent.tool_trading import _risk_check_snapshots
from src.gateway.async_io import run_gateway_io
from src.gateway.base import Gateway, OrderRequest, OrderResult, Position
from src.risk.stop_risk import planned_stop_loss, projected_position


async def amend_order(deps: ToolDeps, args: dict) -> ToolOutcome:
    """只修改未成交挂单价格，并按改价后的整仓风险重新校验。

    参数：
        deps: ToolDeps，工具依赖
        args: dict，含合约、订单 ID 和新价格

    返回：
        ToolOutcome：价格修改结果、风险拒绝或状态变化中止结果

    异常：
        ToolArgError：尝试修改张数、未提供价格或找不到原订单时抛出
    """
    contract = _need_str(args, "contract")
    order_id = _need_str(args, "order_id")
    if "size" in args:
        raise ToolArgError("amend_order 只允许修改价格；改变金额请撤单后重新提交")
    price = _need_decimal(args, "price")
    epoch0 = deps.close_epochs.get(contract, 0)
    reset0 = deps.reset_epoch[0]
    open_orders = await run_gateway_io(deps.gateway.list_orders, contract, "open")
    original = next((item for item in open_orders if item.id == order_id), None)
    if original is None:
        raise ToolArgError(f"未找到未成交订单 {order_id}")
    positions = await run_gateway_io(deps.gateway.list_positions)
    pos = next((item for item in positions if item.contract == contract), None)
    signed_left = original.left if original.size >= 0 else -original.left
    is_close = _is_reduction(original, pos, signed_left)
    deny = await _amend_risk_check(
        deps,
        contract=contract,
        order_id=order_id,
        price=price,
        size=signed_left,
        leverage=max(int(pos.leverage), 1) if pos is not None and pos.leverage else 1,
        is_close=is_close,
        original=original,
        positions=positions,
        open_orders=open_orders,
    )
    if deny is not None:
        return deny
    result = await _submit_amend(
        deps,
        contract,
        order_id,
        price,
        pos.size if pos is not None else Decimal(0),
        epoch0,
        reset0,
    )
    if result is None:
        return ToolOutcome(
            f"已中止：{contract} 在风控校验期间持仓、人工平仓状态或账户代际发生变化，请重新评估",
            "deny",
            "校验期间状态变化",
        )
    await _persist_amend(deps, result, contract, order_id, price, signed_left)
    return ToolOutcome(
        f"改单成功：订单 {result.id}，剩余 {result.left}，状态 {result.status}", "allow"
    )


def _is_reduction(original: OrderResult, position: Position | None, signed_left: Decimal) -> bool:
    """判断原挂单是否只会降低当前持仓风险。

    参数：
        original: OrderResult，原订单快照
        position: Position | None，当前持仓
        signed_left: Decimal，带方向的剩余张数

    返回：
        bool：纯减仓或平仓订单返回 True
    """
    if original.reduce_only:
        return True
    return bool(
        position is not None
        and position.size != 0
        and (position.size > 0) != (signed_left > 0)
        and abs(signed_left) <= abs(position.size)
    )


async def _submit_amend(
    deps: ToolDeps,
    contract: str,
    order_id: str,
    price: Decimal,
    expected_position_size: Decimal,
    epoch0: int,
    reset0: int,
) -> OrderResult | None:
    """最终核对持仓代际与张数后提交价格修改。

    参数：
        deps: ToolDeps，工具依赖
        contract: str，合约标识
        order_id: str，订单 ID
        price: Decimal，新价格
        expected_position_size: Decimal，风险校验时持仓张数
        epoch0: int，进入工具时的人工平仓代际
        reset0: int，进入工具时的账户重置代际

    返回：
        OrderResult | None：改单结果；状态发生变化时返回 None
    """
    return await run_gateway_io(
        _amend_unless_close_intervened,
        deps.gateway,
        contract,
        order_id,
        price,
        None,
        deps.close_epochs,
        epoch0,
        resets=deps.reset_epoch,
        reset0=reset0,
        expected_position_size=expected_position_size,
        mutation=True,
    )


async def _persist_amend(
    deps: ToolDeps,
    result: OrderResult,
    contract: str,
    order_id: str,
    price: Decimal,
    signed_left: Decimal,
) -> None:
    """更新本地订单记录；手工订单无原记录时补建一行。

    参数：
        deps: ToolDeps，工具依赖
        result: OrderResult，交易所改单结果
        contract: str，合约标识
        order_id: str，订单 ID
        price: Decimal，新价格
        signed_left: Decimal，带方向剩余张数

    返回：
        None，更新或新增本地订单记录
    """
    if await deps.repo.update_order_after_amend(order_id, price=price, side_size=None):
        return
    await deps.repo.save_order(
        order_id=result.id,
        round_id=deps.round_id,
        mode=deps.mode,
        contract=contract,
        side_size=signed_left,
        price=price,
        status=result.status,
        finish_as=result.finish_as,
    )


async def _amend_risk_check(
    deps: ToolDeps,
    *,
    contract: str,
    order_id: str,
    price: Decimal,
    size: Decimal,
    leverage: int,
    is_close: bool,
    original: OrderResult,
    positions: list[Position],
    open_orders: list[OrderResult],
) -> ToolOutcome | None:
    """按改价后的剩余张数重算名义敞口和整仓计划止损风险。

    参数：
        deps: ToolDeps，工具依赖
        contract: str，合约标识
        order_id: str，被修改订单 ID
        price: Decimal，新委托价
        size: Decimal，带方向的剩余张数
        leverage: int，当前合约杠杆
        is_close: bool，是否为纯减仓挂单
        original: OrderResult，原订单快照
        positions: list[Position]，当前持仓快照
        open_orders: list[OrderResult]，当前未成交订单

    返回：
        ToolOutcome | None：风险拒绝结果；通过时返回 None
    """
    pos = next((item for item in positions if item.contract == contract), None)
    if is_close and pos is not None:
        request = OrderRequest(contract=contract, size=size, price=price, reduce_only=True)
        return await reduction_risk_check(deps, request, pos, positions)
    meta = await fresh_contract(deps, contract)
    account = await run_gateway_io(deps.gateway.get_account)
    planned_risk: Decimal | None = None
    if not is_close:
        result = _amended_stop_risk(
            original, positions, contract, size, price, meta.quanto_multiplier
        )
        if isinstance(result, ToolOutcome):
            return result
        planned_risk = result
    return await _risk_check_snapshots(
        deps,
        contract,
        size=size,
        price=price,
        is_close=is_close,
        leverage=leverage,
        meta=meta,
        account=account,
        positions=positions,
        resting=open_orders,
        exclude_order_id=order_id,
        planned_stop_risk=planned_risk,
    )


def _amended_stop_risk(
    original: OrderResult,
    positions: list[Position],
    contract: str,
    size: Decimal,
    price: Decimal,
    multiplier: Decimal,
) -> Decimal | ToolOutcome:
    """计算增仓挂单改价后的预计整仓止损金额。

    参数：
        original: OrderResult，原订单快照
        positions: list[Position]，当前持仓快照
        contract: str，合约标识
        size: Decimal，带方向剩余张数
        price: Decimal，新价格
        multiplier: Decimal，实时合约乘数

    返回：
        Decimal | ToolOutcome：计划止损金额，或缺少止损/反手风险拒绝
    """
    if original.stop_loss_price is None:
        return ToolOutcome("风控拒绝：增仓挂单缺少止损，不能改单", "deny", "缺少止损")
    pos = next((item for item in positions if item.contract == contract), None)
    try:
        projected_size, projected_entry = projected_position(
            current_size=pos.size if pos is not None else Decimal(0),
            current_entry_price=pos.entry_price if pos is not None else Decimal(0),
            added_size=size,
            added_entry_price=price,
        )
    except ValueError as exc:
        return ToolOutcome(f"风控拒绝：{exc}", "deny", str(exc))
    return planned_stop_loss(
        entry_price=projected_entry,
        stop_loss_price=original.stop_loss_price,
        size=projected_size,
        multiplier=multiplier,
    )


async def _amend_direction(
    gateway: Gateway, contract: str, order_id: str, size: Decimal | None
) -> tuple[bool, Decimal]:
    """兼容读取旧测试所需的改单方向与有效张数判断。

    参数：
        gateway: Gateway，交易网关
        contract: str，合约标识
        order_id: str，交易所订单标识
        size: Decimal | None，可选新张数

    返回：
        tuple[bool, Decimal]：是否纯减仓与参与风控的带方向张数
    """
    positions = await run_gateway_io(gateway.list_positions)
    pos = next((item for item in positions if item.contract == contract), None)
    if size is not None:
        if pos is None or pos.size == 0:
            return False, size
        return (pos.size > 0) != (size > 0) and abs(size) <= abs(pos.size), size
    for order in await run_gateway_io(gateway.list_orders, contract, "open"):
        if order.id == order_id:
            return False, order.left
    return False, Decimal(0)
