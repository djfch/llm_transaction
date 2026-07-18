"""风控引擎：逐条执行规则，汇总拒绝理由。规则为纯代码，LLM 无法绕过。"""

from __future__ import annotations

from src.config import RiskConfig
from src.risk.models import AccountSnapshot, DailyStats, PositionSnapshot, TradeIntent, Verdict
from src.risk.rules import ALL_RULES, RuleInput


class RiskEngine:
    """无状态风控引擎：同一输入永远得到同一判定。"""

    def check(
        self,
        intent: TradeIntent,
        account: AccountSnapshot,
        positions: list[PositionSnapshot],
        daily_stats: DailyStats,
        watchlist: list[str],
        config: RiskConfig,
    ) -> Verdict:
        """逐条跑规则；任一拒绝即 Deny，reasons 汇总全部命中理由（不只返回第一条）。"""
        ctx = RuleInput(intent, account, positions, daily_stats, watchlist, config)
        reasons = [reason for rule in ALL_RULES if (reason := rule(ctx))]
        if reasons:
            return Verdict.deny(reasons)
        return Verdict.allow()
