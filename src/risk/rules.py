"""风控规则集：纯函数，无 IO，LLM 无法绕过。

每条规则签名统一为 (ctx) -> str | None：返回 None 表示通过，否则返回拒绝理由。
边界总约定：超过阈值才拒绝，恰好等于阈值放行。
例外：日下单数为计数型上限，orders_today 达到 max_orders_per_day 即额度用尽，拒绝下一笔开仓。
豁免约定：is_close=True（平仓/减仓）只受价格偏离规则约束，其余规则一律豁免。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.config import RiskConfig
from src.risk.models import AccountSnapshot, DailyStats, PositionSnapshot, TradeIntent


@dataclass(frozen=True)
class RuleInput:
    """单条规则的判定输入（一次风控检查的完整上下文）。"""

    intent: TradeIntent
    account: AccountSnapshot
    positions: list[PositionSnapshot]
    daily: DailyStats
    watchlist: list[str]
    config: RiskConfig


def intent_notional(intent: TradeIntent) -> Decimal:
    """意图名义价值 = |张数| × quanto_multiplier × 价格（市价单用标记价估值）。"""
    price = intent.price if intent.price is not None else intent.mark_price
    return abs(intent.side_size) * intent.quanto_multiplier * price


def positions_notional(positions: list[PositionSnapshot]) -> Decimal:
    """全部持仓名义价值合计（多空均按绝对值计）。"""
    return sum((abs(p.size) * p.quanto_multiplier * p.mark_price for p in positions), Decimal(0))


def _pct(value: float) -> Decimal:
    """配置中的比例（float）转 Decimal，避免浮点直接参与金额比较。"""
    return Decimal(str(value))


def rule_whitelist(ctx: RuleInput) -> str | None:
    """白名单：开仓合约必须在 watchlist 内（平仓豁免，避免移出白名单后无法平仓）。"""
    if ctx.intent.is_close or ctx.intent.contract in ctx.watchlist:
        return None
    return f"合约 {ctx.intent.contract} 不在白名单，禁止开仓"


def rule_kill_switch(ctx: RuleInput) -> str | None:
    """kill_switch 开启时禁止一切开仓；平仓永远放行。"""
    if ctx.config.kill_switch and not ctx.intent.is_close:
        return "kill_switch 已开启，禁止开仓"
    return None


def rule_position_limit(ctx: RuleInput) -> str | None:
    """单仓名义价值 / 账户权益 不得超过 max_position_pct（等于放行；平仓豁免）。"""
    if ctx.intent.is_close:
        return None
    if intent_notional(ctx.intent) > _pct(ctx.config.max_position_pct) * ctx.account.equity:
        return f"单仓名义价值超过账户权益上限 {ctx.config.max_position_pct:.0%}"
    return None


def rule_total_position_limit(ctx: RuleInput) -> str | None:
    """（全部持仓名义价值 + 本单名义价值）/ 权益 不得超过 max_total_position_pct（等于放行）。"""
    if ctx.intent.is_close:
        return None
    total = positions_notional(ctx.positions) + intent_notional(ctx.intent)
    if total > _pct(ctx.config.max_total_position_pct) * ctx.account.equity:
        return f"总持仓名义价值超过账户权益上限 {ctx.config.max_total_position_pct:.0%}"
    return None


def rule_leverage(ctx: RuleInput) -> str | None:
    """请求杠杆不得超过 max_leverage（等于放行；平仓豁免）。"""
    if ctx.intent.is_close or ctx.intent.leverage <= ctx.config.max_leverage:
        return None
    return f"杠杆 {ctx.intent.leverage}x 超过上限 {ctx.config.max_leverage}x"


def rule_daily_loss(ctx: RuleInput) -> str | None:
    """日亏损锁仓：当日已实现+未实现亏损超过 daily_loss_limit×权益 时只平不开（等于放行）。"""
    if ctx.intent.is_close:
        return None
    total_pnl = ctx.daily.realized_pnl + ctx.account.unrealised_pnl
    if -total_pnl > _pct(ctx.config.daily_loss_limit) * ctx.account.equity:
        return "当日亏损超过锁仓线，只平不开"
    return None


def rule_max_orders(ctx: RuleInput) -> str | None:
    """日下单数达到 max_orders_per_day 后拒绝开仓（平仓豁免；计数型上限，达到即用尽）。"""
    if ctx.intent.is_close or ctx.daily.orders_today < ctx.config.max_orders_per_day:
        return None
    return f"当日下单数已达上限 {ctx.config.max_orders_per_day}，拒绝开仓"


def rule_price_deviation(ctx: RuleInput) -> str | None:
    """委托价与标记价偏离超过 max_deviation 拒绝；市价单豁免（等于放行；平仓不豁免）。"""
    if ctx.intent.price is None:
        return None
    deviation = abs(ctx.intent.price - ctx.intent.mark_price) / ctx.intent.mark_price
    if deviation > _pct(ctx.config.max_deviation):
        return f"委托价偏离标记价超过 {ctx.config.max_deviation:.0%}"
    return None


ALL_RULES = [
    rule_whitelist,
    rule_kill_switch,
    rule_position_limit,
    rule_total_position_limit,
    rule_leverage,
    rule_daily_loss,
    rule_max_orders,
    rule_price_deviation,
]
