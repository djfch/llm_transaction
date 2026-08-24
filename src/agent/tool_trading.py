"""交易工具共享的风控、杠杆解析、止盈止损方向与订单落库辅助。

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
from decimal import Decimal

from pydantic import ValidationError

from src.agent.context import compute_equity, position_snapshots
from src.agent.tool_handlers import ToolArgError, ToolDeps, ToolOutcome
from src.audit.logger import get_logger
from src.gateway.async_io import PRIORITY_NORMAL, run_gateway_io
from src.gateway.base import Account, Contract, OrderRequest, OrderResult, Position
from src.risk.models import AccountSnapshot, OpenOrderIntent, TradeIntent

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
    exclude_order_id: str | None = None,
    planned_stop_risk: Decimal | None = None,
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
        exclude_order_id: str | None，改单时从挂单敞口中排除的原订单 ID
        planned_stop_risk: Decimal | None，本单成交后整仓计划止损估算
    返回：
        ToolOutcome | None，构造 TradeIntent 过风控；拒绝返回 deny 文本，放行返回 None
    异常：
        ToolArgError，交易意图字段未通过模型校验时抛出
    """
    meta = await run_gateway_io(deps.gateway.get_contract, contract, priority=priority)
    account = await run_gateway_io(deps.gateway.get_account, priority=priority)
    positions = await run_gateway_io(deps.gateway.list_positions, priority=priority)
    # 未成交挂单计入敞口（issue #58）：风控看到的必须是"持仓+挂单+本单"的完整账本
    resting = await run_gateway_io(deps.gateway.list_orders, contract, "open", priority=priority)
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
        resting=resting,
        priority=priority,
        exclude_order_id=exclude_order_id,
        planned_stop_risk=planned_stop_risk,
    )


async def _risk_check_snapshots(
    deps: ToolDeps,
    contract: str,
    *,
    size: Decimal,
    price: Decimal | None,
    is_close: bool,
    leverage: int,
    meta: Contract,
    account: Account,
    positions: list[Position],
    resting: list[OrderResult],
    priority: int = PRIORITY_NORMAL,
    exclude_order_id: str | None = None,
    planned_stop_risk: Decimal | None = None,
) -> ToolOutcome | None:
    """使用同一锁内读取的账户、持仓、规格和挂单快照执行完整风控。

    参数：
        deps: ToolDeps，工具依赖
        contract: str，合约标识
        size: Decimal，本单内部张数
        price: Decimal | None，限价或市价标志
        is_close: bool，是否为纯降险订单
        leverage: int，请求杠杆
        meta: Contract，已读取的合约规格
        account: Account，已读取的账户快照
        positions: list[Position]，已读取的持仓快照
        resting: list[OrderResult]，已读取的未成交订单
        priority: int，其他合约规格读取优先级
        exclude_order_id: str | None，改单时排除的原订单 ID
        planned_stop_risk: Decimal | None，成交后整仓计划止损估算

    返回：
        ToolOutcome | None：风控拒绝结果；全部规则通过时返回 None

    异常：
        ToolArgError：交易意图字段未通过模型校验时抛出
    """
    open_orders = [
        OpenOrderIntent(
            contract=contract,
            price=order.price,
            size_left=order.left,
            quanto_multiplier=meta.quanto_multiplier,
        )
        for order in resting
        if order.left and order.left > 0 and order.id != exclude_order_id
    ]
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
            planned_stop_risk=planned_stop_risk,
        )
    except ValidationError as e:
        raise ToolArgError(f"交易意图不合法：{e.errors()[0]['msg']}") from e
    verdict = deps.risk_engine.check(
        intent,
        snap,
        await position_snapshots(
            deps.gateway, positions, priority=priority, metadata={contract: meta}
        ),
        daily,
        deps.watchlist,
        deps.risk_config,
        gate_direction,
        open_orders=open_orders,
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
