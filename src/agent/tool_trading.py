"""交易类工具（place_order / update_tpsl / amend_order / cancel_order）。

硬规范：任何到达网关的交易动作必须先过 RiskEngine——风控拒绝返回理由文本，绝不放行。
本模块独立维护交易工具以控制文件体量；参数校验辅助与 ToolDeps 经 tool_handlers 共享，
杠杆设置/回滚/对账的安全边界在 tool_leverage（下单失败回滚、核验与风控锁联动）。

落库约定：
- 订单落 orders 表（is_close 置位供 daily_stats 排除平仓单；trade_source 标记下单方，
  manual_close 传 user_close，供交易所真实成交回报分类归属）；改单更新原行不重复计数
- trades 表不由本模块写入：paper 模式由决策循环/行情 drain 成交缓冲统一落库；
  testnet/live 模式由 ExchangeFillSync 按交易所真实成交回报落库（见 agent/fill_sync）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import ValidationError

from src.agent.context import compute_equity, position_snapshots
from src.agent.tool_handlers import (
    ToolArgError,
    ToolDeps,
    ToolOutcome,
    _need_decimal,
    _need_str,
    _opt_bool,
    _opt_decimal,
    _opt_enum,
    _opt_int,
)
from src.agent.tool_leverage import (
    _engage_kill,
    _locked_leverage_transaction,
    _prev_leverage_state,
)
from src.audit.logger import get_logger
from src.gateway.async_io import PRIORITY_HIGH, PRIORITY_NORMAL, run_gateway_io
from src.gateway.base import Gateway, GatewayError, OrderRequest, OrderResult, Position, TpslOrder
from src.risk.models import AccountSnapshot, TradeIntent

logger = get_logger(__name__)


# ---------- 风控辅助 ----------


async def _research_gate_direction(deps: ToolDeps, contract: str) -> str | None:
    """按订单合约读取 v2 研报；仅可靠的高置信催化结论进入硬闸门。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        contract: str，合约标识
    返回：
        str | None，按订单合约读取 v2 研报；仅可靠的高置信催化结论进入硬闸门
    """
    cfg = deps.research_config
    if cfg is None or not cfg.gate_enabled:
        return None
    try:
        view = await deps.repo.research.latest_asset_view(contract)
    except Exception:
        logger.exception("研报方向闸门读取逐标的结论失败，降级为不约束方向")
        return None
    if view is None or view.confidence != "高":
        return None
    if view.direction not in ("偏多", "偏空"):
        return None
    if view.basis_type not in ("事件驱动", "宏观驱动", "混合"):
        return None
    if view.data_status == "不可用":
        return None
    if view.technical_confirmation in ("冲突", "不可用"):
        return None
    if time.time() - view.created_at > cfg.gate_max_age_hours * 3600:
        return None
    return view.direction


async def _risk_check(
    deps: ToolDeps,
    contract: str,
    *,
    size: Decimal,
    price: Decimal | None,
    is_close: bool,
    leverage: int,
    priority: int = PRIORITY_NORMAL,
) -> ToolOutcome | None:
    """构造 TradeIntent 过风控；拒绝返回 deny 文本，放行返回 None。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        contract: str，合约标识
        size: Decimal，订单张数
        price: Decimal | None，委托价格；None 表示市价
        is_close: bool，是否为纯平仓或减仓
        leverage: int，请求杠杆倍数
        priority: int，风控内网关读取的卸载优先级；手动安全操作传 PRIORITY_HIGH，
            避免人工平仓在风控阶段掉回普通队尾（PR #84 评审 P1）
    返回：
        ToolOutcome | None，构造 TradeIntent 过风控；拒绝返回 deny 文本，放行返回 None
    异常：
        ToolArgError，交易意图字段未通过模型校验时抛出
    """
    meta = await run_gateway_io(deps.gateway.get_contract, contract, priority=priority)
    account = await run_gateway_io(deps.gateway.get_account, priority=priority)
    positions = await run_gateway_io(deps.gateway.list_positions, priority=priority)
    equity = compute_equity(account, positions)
    if equity <= 0:
        return ToolOutcome("风控拒绝：账户权益非正，禁止交易", "deny", "账户权益非正")
    snap = AccountSnapshot(equity=equity, unrealised_pnl=account.unrealised_pnl)
    daily = await deps.daily_stats_fn()
    gate_direction = await _research_gate_direction(deps, contract)
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
        await position_snapshots(deps.gateway, positions, priority=priority),
        daily,
        deps.watchlist,
        deps.risk_config,
        gate_direction,
    )
    if not verdict.allowed:
        reason = "；".join(verdict.reasons)
        return ToolOutcome(f"风控拒绝：{reason}", "deny", reason)
    return None


def _resolve_leverage(
    contract: str, declared: int | None, positions: list[Position]
) -> tuple[int, int | None]:
    """返回风控杠杆与下单前需实际设置的杠杆；声明值总会由 place_order 生效。

    参数：
        contract: str，合约标识
        declared: int | None，调用方显式声明的杠杆
        positions: list[Position]，当前持仓列表
    返回：
        tuple[int, int | None]，风控杠杆与下单前需实际设置的杠杆；声明值总会由 place_order 生效
    """
    pos = next((p for p in positions if p.contract == contract), None)
    if declared is not None:
        return declared, declared
    if pos is not None:
        if pos.margin_mode == "cross":
            limit = pos.cross_leverage_limit
            if limit is not None and limit > 0:
                return max(int(limit), 1), None  # 全仓有效杠杆以 cross_leverage_limit 为准
        return max(int(pos.leverage), 1), None  # leverage=0 表示全仓，缺失时按 1 参与判定
    return 1, None


def _position_after(positions: list[Position], contract: str, size: Decimal) -> Decimal:
    """预估本单成交后的持仓张数，供 place_order 判定持仓方向以校验止盈止损。

    参数：
        positions: list[Position]，当前全部持仓列表
        contract: str，合约名（如 BTC_USDT）
        size: Decimal，本次下单张数（正多负空）

    返回：
        Decimal：预估下单后的持仓张数（含方向，正多负空）；该合约无持仓时按 0 起算
    """
    pos = next((p for p in positions if p.contract == contract), None)
    return (pos.size if pos is not None else Decimal(0)) + size


def _opens_exposure(
    positions: list[Position], contract: str, size: Decimal, close: bool, reduce_only: bool
) -> bool:
    """只把纯平仓/纯减仓视为免止损；反手残余仓属于新敞口。

    参数：
        positions: list[Position]，当前持仓列表
        contract: str，合约标识
        size: Decimal，订单张数
        close: bool，是否全平当前持仓
        reduce_only: bool，是否只允许减少持仓
    返回：
        bool，只把纯平仓/纯减仓视为免止损；反手残余仓属于新敞口
    """
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
    """校验止损/止盈价：必须为正数，且相对标记价的方向与持仓方向匹配。

    参数：
        direction: int，持仓方向（1=多仓，-1=空仓）
        mark_price: Decimal，合约当前标记价
        stop_loss: Decimal，止损价（多仓须低于标记价，空仓须高于标记价）
        take_profit: Decimal | None，止盈价（与止损反向）；None 表示不设置、跳过校验

    返回：
        None，纯校验无副作用；校验不通过时抛异常

    异常：
        ToolArgError：价格非正，或止损/止盈价相对标记价的方向与持仓方向矛盾时抛出
    """
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

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        result: OrderResult，网关返回的订单结果
        req: OrderRequest，原始订单请求
        trade_source: str，订单成交来源
    返回：
        str，落 orders 表；本地落库失败返回告警文本，成功返回空串
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
    """下单：先构造 TradeIntent 过风控，放行才调网关并落订单记录。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        args: dict，工具调用参数
    返回：
        ToolOutcome，下单：先构造 TradeIntent 过风控，放行才调网关并落订单记录
    异常：
        ToolArgError，张数为零、开敞口缺止损、纯平减仓携带止盈止损或杠杆非法时抛出
    """
    contract = _need_str(args, "contract")
    close = _opt_bool(args, "close")
    reduce_only = _opt_bool(args, "reduce_only")
    size = Decimal(0) if close else _need_decimal(args, "size")
    if not close and size == 0:
        raise ToolArgError("size 不能为 0（平仓请用 close=true）")
    price = _opt_decimal(args, "price")
    tif = _opt_enum(args, "tif", {"gtc", "ioc", "poc", "fok"})
    declared = _opt_int(args, "leverage", None)  # None = 未声明
    positions = await run_gateway_io(deps.gateway.list_positions)
    opens_exposure = _opens_exposure(positions, contract, size, close, reduce_only)
    # 平仓代际锚点：增仓单在风控 await 窗口前捕获，最终下单于 executor 线程内比对，
    # 窗口内高优人工平仓介入则放弃下单（PR #84 评审 P1）；平仓/减仓降风险不校验
    close_epoch = deps.close_epochs.get(contract, 0) if opens_exposure else None
    stop_loss = _opt_decimal(args, "stop_loss_price")
    take_profit = _opt_decimal(args, "take_profit_price")
    after = _position_after(positions, contract, size)
    if opens_exposure:
        if stop_loss is None:
            raise ToolArgError("开仓、加仓或反手新开仓必须提供 stop_loss_price（止损价）")
        meta = await run_gateway_io(deps.gateway.get_contract, contract)
        _validate_tpsl(
            direction=1 if after > 0 else -1,
            mark_price=meta.mark_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
    elif stop_loss is not None or take_profit is not None:
        raise ToolArgError("纯平仓或纯减仓不接受止盈止损参数，请使用 update_tpsl 更新存量仓位")
    if declared is not None and declared <= 0:
        raise ToolArgError("leverage 必须为正整数")
    prev_state = _prev_leverage_state(positions, contract)
    has_position = any(p.contract == contract and p.size != 0 for p in positions)
    if opens_exposure and has_position and prev_state is None:
        # 有持仓但读不出可信杠杆（全仓实际杠杆缺失或不可精确回滚）：无论是否声明杠杆，
        # 新增敞口一律 fail closed；平仓/减仓不修改杠杆，不受此守卫拦截
        await _engage_kill(deps, f"{contract} 当前杠杆状态未知，无法快照/回滚杠杆状态")
        return ToolOutcome(
            "当前杠杆状态未知（全仓实际杠杆缺失或不可精确回滚），"
            "已开启风控锁，拒绝新增敞口，请人工核对",
            "deny",
            "杠杆状态未知",
        )
    margin_mode = _opt_enum(args, "margin_mode", {"isolated", "cross"}) or (
        prev_state[1] if prev_state is not None else "isolated"
    )
    leverage, apply_leverage = _resolve_leverage(contract, declared, positions)
    if close or reduce_only:
        apply_leverage = None  # 平仓/减仓无需调整杠杆，避免无谓的交易所调用与失败面
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
    # 合约级锁内完成杠杆写事务（重读核验 → 调杠杆 → 下单前确认 → 下单 → 失败回滚）：
    # 防止并发调用交错改杠杆、用旧快照绕过 max_leverage，或回滚覆盖他人的有效修改
    placed = await _locked_leverage_transaction(
        deps,
        req,
        prev_state=prev_state,
        verify=apply_leverage is not None or opens_exposure,
        apply_leverage=apply_leverage,
        margin_mode=margin_mode,
        close_epoch=close_epoch,
    )
    if isinstance(placed, ToolOutcome):
        return placed
    result = placed
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


@dataclass
class _TpslSwapResult:
    """TPSL 整组替换的执行结果：阶段标识 + 各失败态所需上下文（由 update_tpsl 映射文案）。"""

    stage: str  # ok / position_changed / create_unknown / create_failed / cancel_unknown / cancel_partial
    error: str = ""
    rollback_failed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    old_total: int = 0


def _swap_tpsl_group(
    gateway: Gateway, contract: str, direction: int, requested: list[TpslOrder]
) -> _TpslSwapResult:
    """TPSL 整组替换事务：列旧组 → 建完整新组（失败回滚）→ 撤旧组，全程不可交错。

    同步函数，仅经统一卸载层 run_gateway_io 作为**单个**任务调用：真实网关由唯一
    executor 线程串行保证原子；paper 命中 __gateway_io_inline__ 标记在事件循环
    线程内联执行，协程间同样不可交错。两个并发 update_tpsl 因此按"后写覆盖"语义
    串行生效，不会留下两套新保护单；高优 manual close 只会排在整个交换之前或之后
    （PR #84 评审 P1）。

    事务开头先重读持仓：update_tpsl 的持仓快照是在风控 await 窗口之前读取的，
    窗口内高优平仓/反手可能已完成；持仓不存在、为零或方向与 direction 不一致时
    返回 position_changed，不创建任何新保护单（PR #84 评审 P1，防在已平仓
    合约上建单并报"已更新"）。

    参数：
        gateway: Gateway，交易网关
        contract: str，合约标识
        direction: int，持仓方向（1 多 / -1 空），仅处理同方向旧组
        requested: list[TpslOrder]，待创建的完整新保护组

    返回：
        _TpslSwapResult，阶段标识与失败上下文；不抛 GatewayError（映射为 stage）
    """
    pos = next((p for p in gateway.list_positions() if p.contract == contract), None)
    if pos is None or pos.size == 0 or (pos.size > 0) != (direction > 0):
        return _TpslSwapResult("position_changed")
    old = [order for order in gateway.list_tpsl_orders(contract) if order.direction == direction]
    created: list[TpslOrder] = []
    try:
        for item in requested:
            created.append(gateway.create_tpsl_order(item))
    except GatewayError as exc:
        if exc.label == "TPSL_STATE_UNKNOWN":
            return _TpslSwapResult("create_unknown", str(exc))
        rollback_failed: list[str] = []
        for item in created:
            try:
                gateway.cancel_tpsl_order(item.id)
            except GatewayError:
                logger.exception("止盈止损回滚失败 id=%s", item.id)
                rollback_failed.append(item.id)
        return _TpslSwapResult("create_failed", str(exc), rollback_failed=rollback_failed)
    cancelled: list[str] = []
    try:
        for item in old:
            gateway.cancel_tpsl_order(item.id)
            cancelled.append(item.id)
    except GatewayError as exc:
        if exc.label == "TPSL_STATE_UNKNOWN":
            return _TpslSwapResult("cancel_unknown", str(exc))
        return _TpslSwapResult("cancel_partial", str(exc), cancelled=cancelled, old_total=len(old))
    return _TpslSwapResult("ok", cancelled=cancelled)


async def update_tpsl(deps: ToolDeps, args: dict) -> ToolOutcome:
    """整仓保护替换：完整新组落地后才撤销同方向旧组，避免裸露窗口。

    替换事务（列旧组→建新组→撤旧组）经 _swap_tpsl_group 作为单个卸载任务执行，
    并发更新与 manual close 均不可交错插入交换中段。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        args: dict，工具调用参数
    返回：
        ToolOutcome，整仓保护替换：完整新组落地后才撤销同方向旧组，避免裸露窗口
    异常：
        ToolArgError，当前合约没有持仓时抛出
    """
    contract = _need_str(args, "contract")
    stop_loss = _need_decimal(args, "stop_loss_price")
    take_profit = _opt_decimal(args, "take_profit_price")
    positions = await run_gateway_io(deps.gateway.list_positions)
    pos = next((item for item in positions if item.contract == contract), None)
    if pos is None or pos.size == 0:
        raise ToolArgError("当前无持仓，无法设置整仓止盈止损")
    direction = 1 if pos.size > 0 else -1
    meta = await run_gateway_io(deps.gateway.get_contract, contract)
    _validate_tpsl(
        direction=direction,
        mark_price=meta.mark_price,
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
    swap = await run_gateway_io(
        _swap_tpsl_group, deps.gateway, contract, direction, requested, mutation=True
    )
    if swap.stage == "position_changed":
        return ToolOutcome("止盈止损未更新：持仓已平仓或方向已变化，请核对后重试")
    if swap.stage == "create_unknown":
        return ToolOutcome(
            f"更新止盈止损状态未知；旧保护单未撤销，请人工核对且不要盲目重试：{swap.error}"
        )
    if swap.stage == "create_failed":
        if swap.rollback_failed:
            return ToolOutcome(
                "更新止盈止损失败；旧保护单未变更，但以下新保护单回滚失败，"
                f"可能与旧单并存，请人工核对：{', '.join(swap.rollback_failed)}；原因：{swap.error}"
            )
        return ToolOutcome(f"更新止盈止损失败，新保护单已回滚，旧保护单未变更：{swap.error}")
    if swap.stage == "cancel_unknown":
        return ToolOutcome(
            f"新止盈止损已设置，但撤销旧保护单状态未知，请人工核对且不要盲目重试：{swap.error}"
        )
    if swap.stage == "cancel_partial":
        return ToolOutcome(
            "新止盈止损已设置，但旧保护单仅撤销 "
            f"{len(swap.cancelled)}/{swap.old_total} 个；其余旧单与新单的实际状态需人工核对：{swap.error}"
        )
    text = f"止损已更新为 {stop_loss}" + (
        f"；止盈已更新为 {take_profit}" if take_profit else "；止盈未设置"
    )
    return ToolOutcome(text, "allow")


async def amend_order(deps: ToolDeps, args: dict) -> ToolOutcome:
    """改单：按改后参数过风控（与下单同一引擎），放行才调网关并同步落库。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        args: dict，工具调用参数
    返回：
        ToolOutcome，改单：按改后参数过风控（与下单同一引擎），放行才调网关并同步落库
    异常：
        ToolArgError，价格和张数均未提供时抛出
    """
    contract = _need_str(args, "contract")
    order_id = _need_str(args, "order_id")
    price = _opt_decimal(args, "price")
    size = _opt_decimal(args, "size")
    if price is None and size is None:
        raise ToolArgError("price 与 size 至少提供一个")
    is_close, effective_size = await _amend_direction(deps.gateway, contract, order_id, size)
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
    result = await run_gateway_io(
        deps.gateway.amend_order, contract, order_id, price=price, size=size, mutation=True
    )
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


async def _amend_direction(
    gateway: Gateway, contract: str, order_id: str, size: Decimal | None
) -> tuple[bool, Decimal]:
    """推断改后（是否平仓方向, 参与风控的有效张数）。

    持仓与挂单两次真实网关读取各自经统一卸载层独立调度（不打包成单个
    复合任务）：HIGH 人工平仓可在两次读取之间插队（PR #84 评审 P1）。
    paper 的纯内存方法命中内联标记不进 executor，保持单线程语义。

    size 给定时按与持仓的方向与数量关系判定：同向改单、以及反向数量超过持仓的
    反手翻仓，都属新敞口，不豁免（必须过全套风控）；仅反向且数量不超过持仓
    才是纯减仓/平仓（豁免，与 place_order 的 _opens_exposure 同一约定）。
    未给 size 时方向不可知，保守按开仓处理（不豁免），张数取挂单剩余量评估占比。

    参数：
        gateway: Gateway，交易网关
        contract: str，合约标识
        order_id: str，交易所订单标识
        size: Decimal | None，订单张数
    返回：
        tuple[bool, Decimal]，推断改后（是否平仓方向, 参与风控的有效张数）
    """
    positions = await run_gateway_io(gateway.list_positions)
    pos = next((p for p in positions if p.contract == contract), None)
    if size is not None:
        if pos is None or pos.size == 0:
            return False, size
        is_close = (pos.size > 0) != (size > 0) and abs(size) <= abs(pos.size)
        return is_close, size
    left = Decimal(0)
    for order in await run_gateway_io(gateway.list_orders, contract, "open"):
        if order.id == order_id:
            left = order.left
            break
    return False, left


async def cancel_order(deps: ToolDeps, args: dict) -> ToolOutcome:
    """撤单：调网关撤销指定订单，并把最新订单状态同步落库到 orders 表。

    参数：
        deps: ToolDeps，工具依赖集合，使用其中的 gateway 撤单、repo 更新本地订单状态
        args: dict，工具入参，须含 contract（合约名）与 order_id（待撤销的订单 ID）

    返回：
        ToolOutcome：执行结果，text 为撤单结果文本（含订单号与最新状态）
    """
    contract = _need_str(args, "contract")
    order_id = _need_str(args, "order_id")
    result = await run_gateway_io(
        deps.gateway.cancel_order, contract, order_id, priority=PRIORITY_HIGH, mutation=True
    )
    await deps.repo.update_order_status(order_id, result.status, result.finish_as or "cancelled")
    return ToolOutcome(f"撤单成功：订单 {order_id}，状态 {result.status}")
