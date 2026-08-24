"""整仓平仓与按比例减仓：不依赖官方规格刷新完成降险。"""

from __future__ import annotations

from decimal import Decimal

from src.agent.context import compute_equity
from src.agent.contract_specs import reduction_contract
from src.agent.position_sizing import calculate_reduction_size
from src.agent.tool_handlers import (
    ToolArgError,
    ToolDeps,
    ToolOutcome,
    _need_str,
    _opt_decimal,
    _opt_enum,
)
from src.agent.tool_leverage import _locked_leverage_transaction, _prev_leverage_state
from src.agent.tool_trading import _record_order
from src.gateway.async_io import run_gateway_io
from src.gateway.base import OrderRequest, Position
from src.risk.models import AccountSnapshot, TradeIntent


def _parse_reduction(args: dict) -> tuple[str, Decimal | None, Decimal | None, str | None]:
    """解析平仓或部分减仓参数并拒绝新增敞口字段。

    参数：
        args: dict，LLM 工具参数

    返回：
        tuple[str, Decimal | None, Decimal | None, str | None]：合约、比例、价格和 TIF

    异常：
        ToolArgError：携带新增敞口字段时抛出
    """
    forbidden = {
        "size",
        "margin_usdt",
        "leverage",
        "side",
        "stop_loss_price",
        "take_profit_price",
        "margin_mode",
        "reduce_only",
    }
    present = sorted(key for key in forbidden if key in args)
    if present:
        raise ToolArgError(f"平仓或减仓不接受这些参数：{', '.join(present)}")
    return (
        _need_str(args, "contract"),
        _opt_decimal(args, "reduce_pct"),
        _opt_decimal(args, "price"),
        _opt_enum(args, "tif", {"gtc", "ioc", "poc", "fok"}),
    )


async def place_reduction(deps: ToolDeps, args: dict, *, close: bool) -> ToolOutcome:
    """提交不依赖官方规格查询的整仓平仓或部分减仓。

    参数：
        deps: ToolDeps，工具依赖
        args: dict，LLM 工具参数
        close: bool，是否整仓市价平仓

    返回：
        ToolOutcome：降险订单结果

    异常：
        ToolArgError：当前无持仓或降险参数冲突时抛出
    """
    contract, reduce_pct, price, tif = _parse_reduction(args)
    positions = await run_gateway_io(deps.gateway.list_positions)
    position = next(
        (item for item in positions if item.contract == contract and item.size != 0), None
    )
    if position is None:
        raise ToolArgError("当前无持仓，无法平仓或减仓")
    if close and reduce_pct is not None:
        raise ToolArgError("close=true 与 reduce_pct 不能同时使用")
    if close and price is not None:
        raise ToolArgError("close=true 为整仓市价平仓，不接受 price")
    try:
        meta = None if close else await reduction_contract(deps, contract)
        size = (
            Decimal(0)
            if close
            else calculate_reduction_size(position.size, reduce_pct or Decimal(0), meta)
        )
    except ValueError as exc:
        raise ToolArgError(str(exc)) from exc
    request = OrderRequest(
        contract=contract,
        size=size,
        price=None if close else price,
        tif=tif,
        reduce_only=not close,
        close=close,
    )
    denial = await reduction_risk_check(deps, request, position, positions)
    if denial is not None:
        return denial
    placed = await _locked_leverage_transaction(
        deps,
        request,
        prev_state=_prev_leverage_state(positions, contract),
        verify=False,
        apply_leverage=None,
        margin_mode=position.margin_mode,
    )
    if isinstance(placed, ToolOutcome):
        return placed
    warning = await _record_order(deps, placed, request)
    text = f"{'整仓平仓' if close else '部分减仓'}订单已提交：{contract}，内部张数 {size}，订单号 {placed.id}"
    if warning:
        text += f"；警告：{warning}"
    return ToolOutcome(text, "allow")


async def reduction_risk_check(
    deps: ToolDeps,
    request: OrderRequest,
    position: Position,
    positions: list[Position],
) -> ToolOutcome | None:
    """仅执行降险仍适用的价格偏离等硬规则。

    参数：
        deps: ToolDeps，工具依赖
        request: OrderRequest，平仓或减仓请求
        position: Position，目标持仓
        positions: list[Position]，全部持仓

    返回：
        ToolOutcome | None：风险拒绝结果；通过时返回 None
    """
    account = await run_gateway_io(deps.gateway.get_account)
    equity = max(compute_equity(account, positions), Decimal(1))
    mark = position.mark_price if position.mark_price > 0 else request.price or Decimal(1)
    intent = TradeIntent(
        contract=request.contract,
        side_size=request.size,
        price=request.price,
        is_close=True,
        leverage=1,
        mark_price=mark,
        quanto_multiplier=Decimal(1),
    )
    verdict = deps.risk_engine.check(
        intent,
        AccountSnapshot(equity=equity, unrealised_pnl=account.unrealised_pnl),
        [],
        await deps.daily_stats_fn(),
        deps.watchlist,
        deps.risk_config,
    )
    if verdict.allowed:
        return None
    reason = "；".join(verdict.reasons)
    return ToolOutcome(f"风控拒绝：{reason}", "deny", reason)
