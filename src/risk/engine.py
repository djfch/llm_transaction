"""风控引擎：逐条执行规则，汇总拒绝理由。规则为纯代码，LLM 无法绕过。"""

from __future__ import annotations

from src.config import RiskConfig
from src.risk.models import (
    AccountSnapshot,
    DailyStats,
    OpenOrderIntent,
    PositionSnapshot,
    TradeIntent,
    Verdict,
)
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
        research_direction: str | None = None,
        open_orders: list[OpenOrderIntent] | None = None,
    ) -> Verdict:
        """逐条跑规则；任一拒绝即 Deny，reasons 汇总全部命中理由（不只返回第一条）。

        research_direction：高置信研报方向（偏多/偏空），供方向闸门规则判定；
        缺省 None 表示闸门不约束（兼容既有调用方）。
        open_orders：该合约未成交挂单快照（issue #58 敞口完整性）；缺省空表。

        参数：
            intent: TradeIntent，待校验的交易意图
            account: AccountSnapshot，账户快照
            positions: list[PositionSnapshot]，当前持仓列表
            daily_stats: DailyStats，当日交易统计
            watchlist: list[str]，允许交易的合约白名单
            config: RiskConfig，风险配置
            research_direction: str | None，高置信研报方向；None 表示不约束
            open_orders: list[OpenOrderIntent] | None，未成交挂单快照；None 视为空
        返回：
            Verdict，逐条跑规则；任一拒绝即 Deny，reasons 汇总全部命中理由（不只返回第一条）
        """
        ctx = RuleInput(
            intent,
            account,
            positions,
            daily_stats,
            watchlist,
            config,
            research_direction,
            open_orders or [],
        )
        reasons = [reason for rule in ALL_RULES if (reason := rule(ctx))]
        if reasons:
            return Verdict.deny(reasons)
        return Verdict.allow()
