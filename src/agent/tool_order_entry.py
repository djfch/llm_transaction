"""保证金下单与降险下单：LLM 不接触 Gate 张数。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.agent.context import compute_equity
from src.agent.contract_specs import fresh_contract
from src.agent.position_sizing import (
    PositionSizing,
    calculate_position_sizing,
)
from src.agent.tool_handlers import (
    ToolArgError,
    ToolDeps,
    ToolOutcome,
    _need_decimal,
    _need_enum,
    _need_str,
    _opt_bool,
    _opt_decimal,
    _opt_enum,
)
from src.agent.tool_leverage import (
    _apply_leverage_and_place,
    _contract_lock,
    _engage_kill,
    _prev_leverage_state,
)
from src.agent.tool_order_reduction import place_reduction
from src.agent.tool_trading import _record_order, _risk_check_snapshots, _validate_tpsl
from src.gateway.async_io import run_gateway_io
from src.gateway.base import Account, Contract, GatewayError, OrderRequest, OrderResult, Position
from src.risk.stop_risk import planned_stop_loss, projected_position


@dataclass(frozen=True)
class _ExposureArgs:
    """已通过形状校验的新增敞口参数。"""

    contract: str
    direction: int
    margin_usdt: Decimal
    leverage: int
    price: Decimal | None
    tif: str | None
    stop_loss: Decimal
    take_profit: Decimal | None


@dataclass(frozen=True)
class _PreparedExposure:
    """锁内最终快照计算出的网关请求与回显指标。"""

    request: OrderRequest
    sizing: PositionSizing
    planned_risk: Decimal
    equity: Decimal
    prev_state: tuple[int, str] | None
    state: _ExposureState


@dataclass(frozen=True)
class _ExposureState:
    """必须在风险校验前后保持不变的离散账户交易状态。"""

    available: Decimal
    positions: tuple[tuple, ...]
    resting_orders: tuple[tuple, ...]


def _parse_exposure_args(args: dict) -> _ExposureArgs:
    """解析开仓或同向加仓参数，并禁止 LLM 直接提交张数。

    参数：
        args: dict，LLM 工具参数

    返回：
        _ExposureArgs：已校验的新增敞口参数

    异常：
        ToolArgError：存在旧张数字段、缺少必填项或杠杆不是正整数时抛出
    """
    deprecated = sorted(
        key for key in ("size", "reduce_only", "margin_mode", "text") if key in args
    )
    if deprecated:
        size_note = "place_order 不接受 size；" if "size" in deprecated else ""
        raise ToolArgError(
            f"{size_note}place_order 新增敞口不接受这些旧执行字段：{', '.join(deprecated)}；"
            "请只提交保证金、杠杆、方向与止盈止损"
        )
    side = _need_enum(args, "side", {"long", "short"})
    leverage_value = _need_decimal(args, "leverage")
    if leverage_value <= 0 or leverage_value != leverage_value.to_integral_value():
        raise ToolArgError("leverage 必须为正整数")
    return _ExposureArgs(
        contract=_need_str(args, "contract"),
        direction=1 if side == "long" else -1,
        margin_usdt=_need_decimal(args, "margin_usdt"),
        leverage=int(leverage_value),
        price=_opt_decimal(args, "price"),
        tif=_opt_enum(args, "tif", {"gtc", "ioc", "poc", "fok"}),
        stop_loss=_need_decimal(args, "stop_loss_price"),
        take_profit=_opt_decimal(args, "take_profit_price"),
    )


def _position_for(positions: list[Position], contract: str) -> Position | None:
    """从持仓列表读取指定合约的非零持仓。

    参数：
        positions: list[Position]，当前持仓列表
        contract: str，目标合约

    返回：
        Position | None：非零持仓；没有时返回 None
    """
    return next((item for item in positions if item.contract == contract and item.size != 0), None)


def _is_pending_increase(order: OrderResult, position: Position | None) -> bool:
    """判断未成交订单是否可能增加该合约敞口。

    参数：
        order: OrderResult，未成交订单快照
        position: Position | None，当前持仓

    返回：
        bool：订单可能开仓或同向加仓时为 True
    """
    if order.reduce_only or order.size == 0:
        return False
    if position is None:
        return True
    return (order.size > 0) == (position.size > 0)


def _position_state_denial(position: Position | None, direction: int) -> ToolOutcome | None:
    """拒绝反手和对全仓持仓直接加仓，确保新增敞口固定逐仓。

    参数：
        position: Position | None，当前持仓
        direction: int，请求方向

    返回：
        ToolOutcome | None：违反反手或逐仓规则时返回风控拒绝，否则返回 None
    """
    if position is None:
        return None
    if (position.size > 0) != (direction > 0):
        return ToolOutcome(
            "风控拒绝：不允许直接反手；请先 close=true 平仓，再单独开立反向仓位",
            "deny",
            "不允许直接反手",
        )
    if position.margin_mode != "isolated":
        return ToolOutcome(
            "风控拒绝：当前持仓不是逐仓，不能直接加仓；请先平仓后按逐仓重新开立",
            "deny",
            "当前持仓不是逐仓",
        )
    return None


async def _prepare_exposure(
    deps: ToolDeps, parsed: _ExposureArgs
) -> _PreparedExposure | ToolOutcome:
    """在合约锁内重读最终状态，完成张数、余额和整仓风险校验。

    参数：
        deps: ToolDeps，工具依赖
        parsed: _ExposureArgs，已解析的新增敞口参数

    返回：
        _PreparedExposure | ToolOutcome：可下单请求，或明确的风控拒绝结果
    """
    try:
        meta = await fresh_contract(deps, parsed.contract)
    except (GatewayError, ToolArgError) as exc:
        return ToolOutcome(
            f"风控拒绝：无法实时读取 Gate 合约规格，禁止新增敞口（{exc}）",
            "deny",
            "实时合约规格不可用",
        )
    account = await run_gateway_io(deps.gateway.get_account)
    positions = await run_gateway_io(deps.gateway.list_positions)
    resting = await run_gateway_io(deps.gateway.list_orders, parsed.contract, "open")
    position = _position_for(positions, parsed.contract)
    state = _exposure_state(account, positions, resting)
    state_denial = _position_state_denial(position, parsed.direction)
    if state_denial is not None:
        return state_denial
    if any(_is_pending_increase(order, position) for order in resting):
        return ToolOutcome(
            "风控拒绝：同一合约已有未成交增仓订单；请等待成交、改单或撤单后再新增敞口",
            "deny",
            "已有未成交增仓订单",
        )
    sizing = _sizing(parsed, meta)
    projected_size, projected_entry = _project_position(position, sizing)
    risk = planned_stop_loss(
        entry_price=projected_entry,
        stop_loss_price=parsed.stop_loss,
        size=projected_size,
        multiplier=meta.quanto_multiplier,
    )
    equity = compute_equity(account, positions)
    if sizing.actual_margin + sizing.estimated_fee > account.available:
        return ToolOutcome(
            "风控拒绝：实际保证金与预计手续费超过当前可用余额",
            "deny",
            "可用余额不足",
        )
    deny = await _risk_check_snapshots(
        deps,
        parsed.contract,
        size=sizing.contracts,
        price=parsed.price,
        is_close=False,
        leverage=parsed.leverage,
        meta=meta,
        account=account,
        positions=positions,
        resting=resting,
        planned_stop_risk=risk,
    )
    if deny is not None:
        return deny
    prev_state = _prev_leverage_state(positions, parsed.contract)
    if position is not None and prev_state is None:
        await _engage_kill(deps, f"{parsed.contract} 当前杠杆状态未知，无法安全加仓")
        return ToolOutcome("当前杠杆状态未知，已开启风控锁，拒绝加仓", "deny", "杠杆状态未知")
    request = OrderRequest(
        contract=parsed.contract,
        size=sizing.contracts,
        price=parsed.price,
        tif=parsed.tif,
        stop_loss_price=parsed.stop_loss,
        take_profit_price=parsed.take_profit,
    )
    return _PreparedExposure(request, sizing, risk, equity, prev_state, state)


def _exposure_state(
    account: Account, positions: list[Position], resting: list[OrderResult]
) -> _ExposureState:
    """提取不受正常行情跳动影响的账户、持仓与挂单指纹。

    参数：
        account: Account，当前账户快照
        positions: list[Position]，全部持仓快照
        resting: list[OrderResult]，目标合约未成交订单

    返回：
        _ExposureState：忽略标记价和未实现盈亏的离散交易状态
    """
    position_state = tuple(
        sorted(
            (
                item.contract,
                item.size,
                item.entry_price,
                item.margin,
                item.leverage,
                item.margin_mode,
                item.cross_leverage_limit,
            )
            for item in positions
            if item.size != 0
        )
    )
    order_state = tuple(
        sorted(
            (
                item.id,
                item.size,
                item.left,
                item.price,
                item.reduce_only,
                item.stop_loss_price,
                item.take_profit_price,
            )
            for item in resting
        )
    )
    return _ExposureState(account.available, position_state, order_state)


async def _read_exposure_state(deps: ToolDeps, contract: str) -> _ExposureState:
    """在最终写入前重读离散交易状态。

    参数：
        deps: ToolDeps，工具依赖
        contract: str，目标合约

    返回：
        _ExposureState：当前账户、持仓与挂单指纹
    """
    account = await run_gateway_io(deps.gateway.get_account)
    positions = await run_gateway_io(deps.gateway.list_positions)
    resting = await run_gateway_io(deps.gateway.list_orders, contract, "open")
    return _exposure_state(account, positions, resting)


def _sizing(parsed: _ExposureArgs, meta: Contract) -> PositionSizing:
    """计算实际张数并校验止盈止损方向。

    参数：
        parsed: _ExposureArgs，新增敞口参数
        meta: Contract，实时合约规格

    返回：
        PositionSizing：实际下单换算结果

    异常：
        ToolArgError：张数换算或止盈止损方向非法时抛出
    """
    reference = parsed.price if parsed.price is not None else meta.mark_price
    try:
        result = calculate_position_sizing(
            margin_usdt=parsed.margin_usdt,
            leverage=parsed.leverage,
            reference_price=reference,
            direction=parsed.direction,
            contract=meta,
            is_market=parsed.price is None,
        )
    except ValueError as exc:
        raise ToolArgError(str(exc)) from exc
    _validate_tpsl(
        direction=parsed.direction,
        mark_price=meta.mark_price,
        stop_loss=parsed.stop_loss,
        take_profit=parsed.take_profit,
    )
    return result


def _project_position(position: Position | None, sizing: PositionSizing) -> tuple[Decimal, Decimal]:
    """计算新增订单成交后的整仓张数与均价。

    参数：
        position: Position | None，当前持仓
        sizing: PositionSizing，本单换算结果

    返回：
        tuple[Decimal, Decimal]：预计整仓张数与开仓均价
    """
    return projected_position(
        current_size=position.size if position else Decimal(0),
        current_entry_price=position.entry_price if position else Decimal(0),
        added_size=sizing.contracts,
        added_entry_price=sizing.reference_price,
    )


async def _place_exposure(deps: ToolDeps, args: dict) -> ToolOutcome:
    """在合约级锁内准备并提交开仓或同向加仓订单。

    参数：
        deps: ToolDeps，工具依赖
        args: dict，LLM 工具参数

    返回：
        ToolOutcome：下单结果与换算、计划止损估算
    """
    parsed = _parse_exposure_args(args)
    epoch0 = deps.close_epochs.get(parsed.contract, 0)
    reset0 = deps.reset_epoch[0]
    async with _contract_lock(deps, parsed.contract):
        first = await _prepare_exposure(deps, parsed)
        if isinstance(first, ToolOutcome):
            return first
        prepared = await _prepare_exposure(deps, parsed)
        if isinstance(prepared, ToolOutcome):
            return prepared
        if prepared.state != first.state:
            return ToolOutcome(
                f"已中止：{parsed.contract} 在校验期间账户、持仓或挂单状态发生变化，请重新评估",
                "deny",
                "校验期间状态变化",
            )
        if await _read_exposure_state(deps, parsed.contract) != prepared.state:
            return ToolOutcome(
                f"已中止：{parsed.contract} 在最终提交前账户、持仓或挂单状态发生变化，请重新评估",
                "deny",
                "最终状态变化",
            )
        placed = await _apply_leverage_and_place(
            deps,
            prepared.request,
            apply_leverage=parsed.leverage,
            margin_mode="isolated",
            prev_state=prepared.prev_state,
            close_epoch=epoch0,
            reset_epoch=deps.reset_epoch,
            reset0=reset0,
        )
    if isinstance(placed, ToolOutcome):
        return placed
    warning = await _record_order(deps, placed, prepared.request)
    return _exposure_outcome(parsed, prepared, placed, warning)


def _exposure_outcome(
    parsed: _ExposureArgs,
    prepared: _PreparedExposure,
    result: OrderResult,
    warning: str,
) -> ToolOutcome:
    """组装新增敞口成功后的完整可审计回显。

    参数：
        parsed: _ExposureArgs，请求参数
        prepared: _PreparedExposure，最终换算和风险指标
        result: OrderResult，交易所订单结果
        warning: str，本地落库警告；空串表示成功

    返回：
        ToolOutcome：包含请求/实际保证金、名义价值、张数和计划风险的结果
    """
    ratio = prepared.planned_risk / prepared.equity if prepared.equity > 0 else Decimal(0)
    text = (
        f"下单成功：{parsed.contract}；请求保证金 {prepared.sizing.requested_margin} USDT；"
        f"实际保证金 {prepared.sizing.actual_margin} USDT；杠杆 {parsed.leverage}x；"
        f"实际名义仓位 {prepared.sizing.actual_notional} USDT；"
        f"内部张数 {prepared.sizing.contracts}；计划止损估算 {prepared.planned_risk} USDT；"
        f"权益占比 {ratio:.4%}；订单号 {result.id}，状态 {result.status}"
    )
    if warning:
        text += f"；警告：{warning}"
    return ToolOutcome(text, "allow")


async def place_order(deps: ToolDeps, args: dict) -> ToolOutcome:
    """按 close、reduce_pct 或 margin_usdt 三种互斥语义执行现有下单工具。

    参数：
        deps: ToolDeps，工具依赖
        args: dict，LLM 工具参数

    返回：
        ToolOutcome：平仓、减仓或新增敞口的执行结果

    异常：
        ToolArgError：操作语义冲突或参数不合法时抛出
    """
    close = _opt_bool(args, "close")
    if close:
        return await place_reduction(deps, args, close=True)
    if args.get("reduce_pct") is not None:
        return await place_reduction(deps, args, close=False)
    return await _place_exposure(deps, args)
