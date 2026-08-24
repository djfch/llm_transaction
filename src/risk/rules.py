"""风控规则集：纯函数，无 IO，LLM 无法绕过。

每条规则签名统一为 (ctx) -> str | None：返回 None 表示通过，否则返回拒绝理由。
边界总约定：超过阈值才拒绝，恰好等于阈值放行。
例外：日下单数为计数型上限，orders_today 达到 max_orders_per_day 即额度用尽，拒绝下一笔开仓。
豁免约定：is_close=True（平仓/减仓）只受价格偏离规则约束，其余规则一律豁免。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal

from src.config import RiskConfig
from src.risk.models import (
    AccountSnapshot,
    DailyStats,
    OpenOrderIntent,
    PositionSnapshot,
    TradeIntent,
)


@dataclass(frozen=True)
class RuleInput:
    """单条规则的判定输入（一次风控检查的完整上下文）。"""

    intent: TradeIntent
    account: AccountSnapshot
    positions: list[PositionSnapshot]
    daily: DailyStats
    watchlist: list[str]
    config: RiskConfig
    # 高置信研报方向（偏多/偏空），由调用方在有效期内传入；None=闸门不约束
    research_direction: str | None = None
    # 该合约未成交挂单快照（issue #58 敞口完整性）；缺省空表兼容既有调用方
    open_orders: list[OpenOrderIntent] = dataclass_field(default_factory=list)


def intent_notional(intent: TradeIntent) -> Decimal:
    """意图名义价值 = |张数| × quanto_multiplier × 价格（市价单用标记价估值）。

    参数：
        intent: TradeIntent，待校验的交易意图
    返回：
        Decimal，意图名义价值 = |张数| × quanto_multiplier × 价格（市价单用标记价估值）
    """
    price = intent.price if intent.price is not None else intent.mark_price
    return abs(intent.side_size) * intent.quanto_multiplier * price


def positions_notional(positions: list[PositionSnapshot]) -> Decimal:
    """全部持仓名义价值合计（多空均按绝对值计）。

    参数：
        positions: list[PositionSnapshot]，当前持仓列表
    返回：
        Decimal，全部持仓名义价值合计（多空均按绝对值计）
    """
    return sum((abs(p.size) * p.quanto_multiplier * p.mark_price for p in positions), Decimal(0))


def _pct(value: float) -> Decimal:
    """配置中的比例（float）转 Decimal，避免浮点直接参与金额比较。

    参数：
        value: float，待转换或校验的配置值
    返回：
        Decimal，配置中的比例（float）转 Decimal，避免浮点直接参与金额比较
    """
    return Decimal(str(value))


def rule_whitelist(ctx: RuleInput) -> str | None:
    """白名单：开仓合约必须在 watchlist 内（平仓豁免，避免移出白名单后无法平仓）。

    参数：
        ctx: RuleInput，风控规则上下文
    返回：
        str | None，白名单：开仓合约必须在 watchlist 内（平仓豁免，避免移出白名单后无法平仓）
    """
    if ctx.intent.is_close or ctx.intent.contract in ctx.watchlist:
        return None
    return f"合约 {ctx.intent.contract} 不在白名单，禁止开仓"


def rule_kill_switch(ctx: RuleInput) -> str | None:
    """kill_switch 开启时禁止一切开仓；平仓永远放行。

    参数：
        ctx: RuleInput，风控规则上下文
    返回：
        str | None，kill_switch 开启时禁止一切开仓；平仓永远放行
    """
    if ctx.config.kill_switch and not ctx.intent.is_close:
        return "kill_switch 已开启，禁止开仓"
    return None


def open_orders_notional(
    open_orders: list[OpenOrderIntent], contract: str | None = None
) -> Decimal:
    """未成交挂单名义价值合计（|剩余张数| × quanto × 挂单价；contract 非空时只计该合约）。

    参数：
        open_orders: list[OpenOrderIntent]，未成交挂单快照
        contract: str | None，合约过滤；None 表示全部合约

    返回：
        Decimal，挂单名义价值合计
    """
    total = Decimal(0)
    for order in open_orders:
        if contract is not None and order.contract != contract:
            continue
        if order.price <= 0:
            continue  # 来源缺价（兜底 0）：无法估值，宁少计不多计
        total += abs(order.size_left) * order.quanto_multiplier * order.price
    return total


def _same_contract_exposure(ctx: RuleInput) -> Decimal:
    """同合约敞口 = 该合约现有持仓名义 + 该合约未成交挂单名义（issue #57/#58）。

    参数：
        ctx: RuleInput，风控规则上下文

    返回：
        Decimal：同合约既有敞口合计（不含本单意图）
    """
    pos = sum(
        (
            abs(p.size) * p.quanto_multiplier * p.mark_price
            for p in ctx.positions
            if p.contract == ctx.intent.contract
        ),
        Decimal(0),
    )
    resting = open_orders_notional(ctx.open_orders, ctx.intent.contract)
    return pos + resting


def rule_position_limit(ctx: RuleInput) -> str | None:
    """单仓敞口 / 账户权益 不得超过 max_position_pct（等于放行；平仓豁免）。

    敞口按"该合约现有持仓 + 该合约未成交挂单 + 本单意图"合计（issue #57/#58），
    防拆单与挂单集中成交绕过上限。

    参数：
        ctx: RuleInput，风控规则上下文
    返回：
        str | None，超过阈值返回拒绝理由；平仓/减仓豁免；恰等于阈值放行
    """
    if ctx.intent.is_close:
        return None
    exposure = _same_contract_exposure(ctx) + intent_notional(ctx.intent)
    if exposure > _pct(ctx.config.max_position_pct) * ctx.account.equity:
        return f"单仓名义价值（含持仓与挂单）超过账户权益上限 {ctx.config.max_position_pct:.0%}"
    return None


def rule_total_position_limit(ctx: RuleInput) -> str | None:
    """（全部持仓名义价值 + 本单名义价值）/ 权益 不得超过 max_total_position_pct（等于放行）。

    参数：
        ctx: RuleInput，风控规则上下文
    返回：
        str | None，（全部持仓名义价值 + 本单名义价值）/ 权益 不得超过 max_total_position_pct（等于放行）
    """
    if ctx.intent.is_close:
        return None
    total = (
        positions_notional(ctx.positions)
        + open_orders_notional(ctx.open_orders)
        + intent_notional(ctx.intent)
    )
    if total > _pct(ctx.config.max_total_position_pct) * ctx.account.equity:
        return f"总持仓名义价值（含挂单）超过账户权益上限 {ctx.config.max_total_position_pct:.0%}"
    return None


def rule_leverage(ctx: RuleInput) -> str | None:
    """请求杠杆不得超过 max_leverage（等于放行；平仓豁免）。

    参数：
        ctx: RuleInput，风控规则上下文
    返回：
        str | None，请求杠杆不得超过 max_leverage（等于放行；平仓豁免）
    """
    if ctx.intent.is_close or ctx.intent.leverage <= ctx.config.max_leverage:
        return None
    return f"杠杆 {ctx.intent.leverage}x 超过上限 {ctx.config.max_leverage}x"


def rule_position_stop_risk(ctx: RuleInput) -> str | None:
    """新增敞口后的整仓计划止损金额不得超过账户权益上限。

    参数：
        ctx: RuleInput，风控规则上下文

    返回：
        str | None：超过上限时的拒绝理由；无计划止损数据、降险操作或达线时返回 None
    """
    risk = ctx.intent.planned_stop_risk
    if ctx.intent.is_close or risk is None:
        return None
    limit = _pct(ctx.config.max_position_stop_risk_pct) * ctx.account.equity
    if risk > limit:
        return (
            f"整仓计划止损估算 {risk} USDT 超过账户权益上限 "
            f"{ctx.config.max_position_stop_risk_pct:.0%}"
        )
    return None


def stop_update_rejection(
    *,
    new_risk: Decimal,
    current_risk: Decimal | None,
    has_current_stop: bool,
    equity: Decimal,
    config: RiskConfig,
) -> str | None:
    """判断整仓止损修改是否因扩大超限风险而应被拒绝。

    参数：
        new_risk: Decimal，新止损对应的整仓计划止损金额
        current_risk: Decimal | None，当前止损对应的整仓计划止损金额；无止损时为 None
        has_current_stop: bool，当前是否已有止损保护
        equity: Decimal，账户权益
        config: RiskConfig，风险配置

    返回：
        str | None：需要拒绝时的理由；达线以内、首次补止损或确实缩小风险时返回 None
    """
    limit = _pct(config.max_position_stop_risk_pct) * equity
    if new_risk <= limit or not has_current_stop:
        return None
    if current_risk is not None and new_risk < current_risk:
        return None
    return (
        f"新止损的整仓计划止损估算 {new_risk} USDT 超过账户权益上限 "
        f"{config.max_position_stop_risk_pct:.0%}，且没有缩小当前风险"
    )


def rule_daily_loss(ctx: RuleInput) -> str | None:
    """日亏损锁仓：当日已实现+未实现亏损超过 daily_loss_limit×权益 时只平不开（等于放行）。

    参数：
        ctx: RuleInput，风控规则上下文
    返回：
        str | None，日亏损锁仓：当日已实现+未实现亏损超过 daily_loss_limit×权益 时只平不开（等于放行）
    """
    if ctx.intent.is_close:
        return None
    total_pnl = ctx.daily.realized_pnl + ctx.account.unrealised_pnl
    if -total_pnl > _pct(ctx.config.daily_loss_limit) * ctx.account.equity:
        return "当日亏损超过锁仓线，只平不开"
    return None


def rule_max_orders(ctx: RuleInput) -> str | None:
    """日下单数达到 max_orders_per_day 后拒绝开仓（平仓豁免；计数型上限，达到即用尽）。

    参数：
        ctx: RuleInput，风控规则上下文
    返回：
        str | None，日下单数达到 max_orders_per_day 后拒绝开仓（平仓豁免；计数型上限，达到即用尽）
    """
    if ctx.intent.is_close or ctx.daily.orders_today < ctx.config.max_orders_per_day:
        return None
    return f"当日下单数已达上限 {ctx.config.max_orders_per_day}，拒绝开仓"


def rule_price_deviation(ctx: RuleInput) -> str | None:
    """委托价与标记价偏离超过 max_deviation 拒绝；市价单豁免（等于放行；平仓不豁免）。

    参数：
        ctx: RuleInput，风控规则上下文
    返回：
        str | None，委托价与标记价偏离超过 max_deviation 拒绝；市价单豁免（等于放行；平仓不豁免）
    """
    if ctx.intent.price is None:
        return None
    deviation = abs(ctx.intent.price - ctx.intent.mark_price) / ctx.intent.mark_price
    if deviation > _pct(ctx.config.max_deviation):
        return f"委托价偏离标记价超过 {ctx.config.max_deviation:.0%}"
    return None


def rule_research_direction(ctx: RuleInput) -> str | None:
    """研报方向闸门：高置信研报有效期内，反向开仓硬拒（平/减仓豁免）。

    参数：
        ctx: RuleInput，风控规则上下文
    返回：
        str | None，研报方向闸门：高置信研报有效期内，反向开仓硬拒（平/减仓豁免）
    """
    if ctx.intent.is_close:
        return None
    d = ctx.research_direction
    if not d or d == "中性":
        return None
    if ctx.intent.side_size > 0 and d == "偏空":
        return "高置信研报偏空，反向开多被闸门拦截"
    if ctx.intent.side_size < 0 and d == "偏多":
        return "高置信研报偏多，反向开空被闸门拦截"
    return None


ALL_RULES = [
    rule_whitelist,
    rule_kill_switch,
    rule_position_limit,
    rule_total_position_limit,
    rule_leverage,
    rule_position_stop_risk,
    rule_daily_loss,
    rule_max_orders,
    rule_price_deviation,
    rule_research_direction,
]
