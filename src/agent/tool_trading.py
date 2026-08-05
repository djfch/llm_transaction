"""交易类工具（place_order / update_tpsl / amend_order / cancel_order）。

硬规范：任何到达网关的交易动作必须先过 RiskEngine——风控拒绝返回理由文本，绝不放行。
本模块独立维护交易工具以控制文件体量；参数校验辅助与 ToolDeps 经 tool_handlers 共享。

落库约定：
- 订单落 orders 表（is_close 置位供 daily_stats 排除平仓单；trade_source 标记下单方，
  manual_close 传 user_close，供交易所真实成交回报分类归属）；改单更新原行不重复计数
- trades 表不由本模块写入：paper 模式由决策循环/行情 drain 成交缓冲统一落库；
  testnet/live 模式由 ExchangeFillSync 按交易所真实成交回报落库（见 agent/fill_sync）
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import ValidationError

from src.agent.context import compute_equity, position_snapshots
from src.agent.tool_handlers import (
    ToolArgError,
    ToolDeps,
    ToolOutcome,
    _need_decimal,
    _need_str,
    _opt_decimal,
    _opt_enum,
    _opt_int,
)
from src.audit.logger import get_logger
from src.gateway.base import GatewayError, OrderRequest, OrderResult, Position, TpslOrder
from src.risk.models import AccountSnapshot, TradeIntent

logger = get_logger(__name__)


# ---------- 风控辅助 ----------


async def _risk_check(
    deps: ToolDeps,
    contract: str,
    *,
    size: Decimal,
    price: Decimal | None,
    is_close: bool,
    leverage: int,
) -> ToolOutcome | None:
    """构造 TradeIntent 过风控；拒绝返回 deny 文本，放行返回 None。"""
    meta = deps.gateway.get_contract(contract)
    account = deps.gateway.get_account()
    positions = deps.gateway.list_positions()
    equity = compute_equity(account, positions)
    if equity <= 0:
        return ToolOutcome("风控拒绝：账户权益非正，禁止交易", "deny", "账户权益非正")
    snap = AccountSnapshot(equity=equity, unrealised_pnl=account.unrealised_pnl)
    daily = await deps.daily_stats_fn()
    try:
        intent = TradeIntent(
            contract=contract,
            side_size=size,
            price=price,
            is_close=is_close,
            leverage=leverage,
            mark_price=meta.mark_price,
            quanto_multiplier=meta.quanto_multiplier,
        )
    except ValidationError as e:
        raise ToolArgError(f"交易意图不合法：{e.errors()[0]['msg']}") from e
    verdict = deps.risk_engine.check(
        intent,
        snap,
        position_snapshots(deps.gateway, positions),
        daily,
        deps.watchlist,
        deps.risk_config,
    )
    if not verdict.allowed:
        reason = "；".join(verdict.reasons)
        return ToolOutcome(f"风控拒绝：{reason}", "deny", reason)
    return None


def _resolve_leverage(
    contract: str, declared: int | None, positions: list[Position]
) -> tuple[int, int | None]:
    """返回风控杠杆与下单前需实际设置的杠杆；声明值总会由 place_order 生效。"""
    pos = next((p for p in positions if p.contract == contract), None)
    if declared is not None:
        return declared, declared
    if pos is not None:
        return max(int(pos.leverage), 1), None  # leverage=0 表示全仓，按 1 参与判定
    return 1, None


def _position_after(positions: list[Position], contract: str, size: Decimal) -> Decimal:
    pos = next((p for p in positions if p.contract == contract), None)
    return (pos.size if pos is not None else Decimal(0)) + size


def _opens_exposure(
    positions: list[Position], contract: str, size: Decimal, close: bool, reduce_only: bool
) -> bool:
    """只把纯平仓/纯减仓视为免止损；反手残余仓属于新敞口。"""
    if close:
        return False
    pos = next((p for p in positions if p.contract == contract), None)
    if pos is None or pos.size == 0:
        return not reduce_only
    if (pos.size > 0) == (size > 0):
        return True
    return abs(size) > abs(pos.size)


def _validate_tpsl(
    *, direction: int, mark_price: Decimal, stop_loss: Decimal, take_profit: Decimal | None
) -> None:
    if stop_loss <= 0 or (take_profit is not None and take_profit <= 0):
        raise ToolArgError("止损价与止盈价必须为正数")
    if direction > 0:
        if stop_loss >= mark_price:
            raise ToolArgError("多仓止损价必须低于标记价")
        if take_profit is not None and take_profit <= mark_price:
            raise ToolArgError("多仓止盈价必须高于标记价")
    else:
        if stop_loss <= mark_price:
            raise ToolArgError("空仓止损价必须高于标记价")
        if take_profit is not None and take_profit >= mark_price:
            raise ToolArgError("空仓止盈价必须低于标记价")


# ---------- 落库辅助 ----------


async def _record_order(
    deps: ToolDeps,
    result: OrderResult,
    req: OrderRequest,
    *,
    trade_source: str = "",
) -> str:
    """落 orders 表；本地落库失败返回告警文本，成功返回空串。

    订单已提交到交易所：本地记录失败绝不向上抛异常（LLM 看到"内部错误"会重试，
    重试即重单），返回文本明确"禁止重试"。
    trade_source 透传给 orders.trade_source（manual_close 传 user_close），
    供成交回报分类归属；trades 表由 fill_persist/fill_sync 两条路径写入，此处不落。
    """
    try:
        # 不变量：网关同步返回与本入队之间不得插入 await——aiosqlite 单连接 FIFO
        # 保证 orders 行先于任何成交归属查询（fill_sync 乱序分类）可见
        await deps.repo.save_order(
            order_id=result.id,
            round_id=deps.round_id,
            mode=deps.mode,
            contract=req.contract,
            side_size=req.size,
            price=req.price,
            tif=req.tif or ("ioc" if req.price is None else "gtc"),
            text=result.text,
            status=result.status,
            finish_as=result.finish_as,
            is_close=req.close or req.reduce_only,
            trade_source=trade_source,
        )
    except Exception as e:
        logger.exception("订单 %s 本地落库失败", result.id)
        return (
            f"订单已提交（id={result.id}，状态 {result.status}），"
            f"仅本地记录失败（{type(e).__name__}: {e}），禁止重试"
        )
    return ""


# ---------- 工具执行函数 ----------


async def place_order(deps: ToolDeps, args: dict) -> ToolOutcome:
    """下单：先构造 TradeIntent 过风控，放行才调网关并落订单记录。"""
    contract = _need_str(args, "contract")
    close = bool(args.get("close", False))
    reduce_only = bool(args.get("reduce_only", False))
    size = Decimal(0) if close else _need_decimal(args, "size")
    if not close and size == 0:
        raise ToolArgError("size 不能为 0（平仓请用 close=true）")
    price = _opt_decimal(args, "price")
    tif = _opt_enum(args, "tif", {"gtc", "ioc", "poc", "fok"})
    declared = _opt_int(args, "leverage", None)  # None = 未声明
    positions = deps.gateway.list_positions()
    opens_exposure = _opens_exposure(positions, contract, size, close, reduce_only)
    stop_loss = _opt_decimal(args, "stop_loss_price")
    take_profit = _opt_decimal(args, "take_profit_price")
    after = _position_after(positions, contract, size)
    if opens_exposure:
        if stop_loss is None:
            raise ToolArgError("开仓、加仓或反手新开仓必须提供 stop_loss_price（止损价）")
        _validate_tpsl(
            direction=1 if after > 0 else -1,
            mark_price=deps.gateway.get_contract(contract).mark_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
    elif stop_loss is not None or take_profit is not None:
        raise ToolArgError("纯平仓或纯减仓不接受止盈止损参数，请使用 update_tpsl 更新存量仓位")
    if declared is not None and declared <= 0:
        raise ToolArgError("leverage 必须为正整数")
    margin_mode = _opt_enum(args, "margin_mode", {"isolated", "cross"}) or "isolated"
    leverage, apply_leverage = _resolve_leverage(contract, declared, positions)
    deny = await _risk_check(
        deps,
        contract,
        size=size,
        price=None if close else price,  # close 单 price 被网关忽略，不带入偏离判定
        is_close=close or reduce_only,
        leverage=leverage,
    )
    if deny is not None:
        return deny
    if apply_leverage is not None:
        deps.gateway.set_leverage(contract, apply_leverage, margin_mode)
    req = OrderRequest(
        contract=contract,
        size=size,
        price=price,
        tif=tif,
        reduce_only=reduce_only,
        close=close,
        stop_loss_price=stop_loss,
        take_profit_price=take_profit,
    )
    result = deps.gateway.place_order(req)
    warning = await _record_order(deps, result, req)
    kind = "市价" if price is None else f"限价 {price}"
    text = (
        f"下单成功：{contract} size={size} {kind}，订单号 {result.id}，"
        f"状态 {result.status}，成交均价 {result.fill_price}"
    )
    if opens_exposure:
        text += f"；止损 {stop_loss}" + (f"，止盈 {take_profit}" if take_profit else "")
    if warning:
        text += f"；警告：{warning}"
    return ToolOutcome(text, "allow")


async def update_tpsl(deps: ToolDeps, args: dict) -> ToolOutcome:
    """整仓保护替换：完整新组落地后才撤销同方向旧组，避免裸露窗口。"""
    contract = _need_str(args, "contract")
    stop_loss = _need_decimal(args, "stop_loss_price")
    take_profit = _opt_decimal(args, "take_profit_price")
    positions = deps.gateway.list_positions()
    pos = next((item for item in positions if item.contract == contract), None)
    if pos is None or pos.size == 0:
        raise ToolArgError("当前无持仓，无法设置整仓止盈止损")
    direction = 1 if pos.size > 0 else -1
    _validate_tpsl(
        direction=direction,
        mark_price=deps.gateway.get_contract(contract).mark_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    deny = await _risk_check(
        deps,
        contract,
        size=-pos.size,
        price=None,
        is_close=True,
        leverage=max(int(pos.leverage), 1),
    )
    if deny is not None:
        return deny
    old = [
        order for order in deps.gateway.list_tpsl_orders(contract) if order.direction == direction
    ]
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
    created: list[TpslOrder] = []
    try:
        for item in requested:
            created.append(deps.gateway.create_tpsl_order(item))
    except GatewayError as exc:
        if exc.label == "TPSL_STATE_UNKNOWN":
            return ToolOutcome(
                f"更新止盈止损状态未知；旧保护单未撤销，请人工核对且不要盲目重试：{exc}"
            )
        rollback_failed: list[str] = []
        for item in created:
            try:
                deps.gateway.cancel_tpsl_order(item.id)
            except GatewayError:
                logger.exception("止盈止损回滚失败 id=%s", item.id)
                rollback_failed.append(item.id)
        if rollback_failed:
            return ToolOutcome(
                "更新止盈止损失败；旧保护单未变更，但以下新保护单回滚失败，"
                f"可能与旧单并存，请人工核对：{', '.join(rollback_failed)}；原因：{exc}"
            )
        return ToolOutcome(f"更新止盈止损失败，新保护单已回滚，旧保护单未变更：{exc}")
    cancelled: list[str] = []
    try:
        for item in old:
            deps.gateway.cancel_tpsl_order(item.id)
            cancelled.append(item.id)
    except GatewayError as exc:
        if exc.label == "TPSL_STATE_UNKNOWN":
            return ToolOutcome(
                f"新止盈止损已设置，但撤销旧保护单状态未知，请人工核对且不要盲目重试：{exc}"
            )
        return ToolOutcome(
            "新止盈止损已设置，但旧保护单仅撤销 "
            f"{len(cancelled)}/{len(old)} 个；其余旧单与新单的实际状态需人工核对：{exc}"
        )
    text = f"止损已更新为 {stop_loss}" + (
        f"；止盈已更新为 {take_profit}" if take_profit else "；止盈未设置"
    )
    return ToolOutcome(text, "allow")


async def amend_order(deps: ToolDeps, args: dict) -> ToolOutcome:
    """改单：按改后参数过风控（与下单同一引擎），放行才调网关并同步落库。"""
    contract = _need_str(args, "contract")
    order_id = _need_str(args, "order_id")
    price = _opt_decimal(args, "price")
    size = _opt_decimal(args, "size")
    if price is None and size is None:
        raise ToolArgError("price 与 size 至少提供一个")
    is_close, effective_size = _amend_direction(deps, contract, order_id, size)
    deny = await _risk_check(
        deps,
        contract,
        size=effective_size,
        price=price,
        is_close=is_close,
        leverage=1,  # 改单不变杠杆，杠杆规则以 1 参与（恒放行）
    )
    if deny is not None:
        return deny
    result = deps.gateway.amend_order(contract, order_id, price=price, size=size)
    if not await deps.repo.update_order_after_amend(order_id, price=price, side_size=size):
        await deps.repo.save_order(  # 本地无记录（如改的是手工单）：补一行
            order_id=result.id,
            round_id=deps.round_id,
            mode=deps.mode,
            contract=contract,
            side_size=size if size is not None else result.left,
            price=price,
            status=result.status,
            finish_as=result.finish_as,
        )
    return ToolOutcome(
        f"改单成功：订单 {result.id}，剩余 {result.left}，状态 {result.status}", "allow"
    )


def _amend_direction(
    deps: ToolDeps, contract: str, order_id: str, size: Decimal | None
) -> tuple[bool, Decimal]:
    """推断改后（是否平仓方向, 参与风控的有效张数）。

    size 给定时按与持仓的方向与数量关系判定：同向改单、以及反向数量超过持仓的
    反手翻仓，都属新敞口，不豁免（必须过全套风控）；仅反向且数量不超过持仓
    才是纯减仓/平仓（豁免，与 place_order 的 _opens_exposure 同一约定）。
    未给 size 时方向不可知，保守按开仓处理（不豁免），张数取挂单剩余量评估占比。
    """
    pos = next((p for p in deps.gateway.list_positions() if p.contract == contract), None)
    if size is not None:
        if pos is None or pos.size == 0:
            return False, size
        is_close = (pos.size > 0) != (size > 0) and abs(size) <= abs(pos.size)
        return is_close, size
    left = Decimal(0)
    for order in deps.gateway.list_orders(contract, "open"):
        if order.id == order_id:
            left = order.left
            break
    return False, left


async def cancel_order(deps: ToolDeps, args: dict) -> ToolOutcome:
    contract = _need_str(args, "contract")
    order_id = _need_str(args, "order_id")
    result = deps.gateway.cancel_order(contract, order_id)
    await deps.repo.update_order_status(order_id, result.status, result.finish_as or "cancelled")
    return ToolOutcome(f"撤单成功：订单 {order_id}，状态 {result.status}")
