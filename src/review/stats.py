"""复盘统计：纯函数口径实现（代码算，LLM 只看不算，见设计 spec §6）。

口径假设（固化，勿漂移）：
- 统计样本 = trades 中 source ∈ {llm_close, tpsl_close, user_close, liquidation} 的平仓成交；
- 入参 trades 假定来自 Repo.trades_for_review（已按区间/mode/可选策略与合约过滤，
  且 JOIN decisions 去重）——一 round_id 对应一条 decisions 行，join 不会重复计数；
- 胜率 = 盈利笔数（pnl>0）/ 样本笔数，样本为 0 时为 None；
- 盈亏比 = 总盈利 / |总亏损|，总亏损为 0 时为 None；
- 金额一律 Decimal，Python 侧合计（沿用 daily_stats 反浮点先例）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.memory.models import Trade

# 平仓成交来源枚举（样本口径；llm_open 与 '' 不计入）
CLOSE_SOURCES = frozenset({"llm_close", "tpsl_close", "user_close", "liquidation"})

# 比率类指标保留 4 位小数，均值保留 8 位（展示再另行取舍，存储值保持稳定）
_RATIO_Q = Decimal("0.0001")
_AVG_Q = Decimal("0.00000001")


@dataclass(frozen=True)
class ContractStat:
    """单个合约的平仓分布。"""

    count: int
    pnl: Decimal


@dataclass(frozen=True)
class ReviewStats:
    """一个复盘区间的统计结果。比率/均值为 None 表示分母为 0（数据不足）。"""

    close_count: int  # 平仓笔数（样本量）
    total_pnl: Decimal  # 总盈亏
    win_count: int  # 盈利笔数（pnl > 0）
    win_rate: Decimal | None  # 胜率（0-1）
    total_profit: Decimal  # 总盈利（盈利笔合计）
    total_loss: Decimal  # 总亏损（亏损笔合计，≤0）
    profit_factor: Decimal | None  # 盈亏比 = 总盈利 / |总亏损|
    avg_win: Decimal | None  # 平均盈利
    avg_loss: Decimal | None  # 平均亏损（≤0）
    max_loss: Decimal | None  # 最大单笔亏损（最负 pnl）
    per_contract: dict[str, ContractStat] = field(default_factory=dict)  # 各合约分布

    def to_dict(self) -> dict:
        """序列化为 stats_json 落库结构（Decimal 转字符串，None 保留）。"""
        return {
            "close_count": self.close_count,
            "total_pnl": str(self.total_pnl),
            "win_count": self.win_count,
            "win_rate": None if self.win_rate is None else str(self.win_rate),
            "total_profit": str(self.total_profit),
            "total_loss": str(self.total_loss),
            "profit_factor": None if self.profit_factor is None else str(self.profit_factor),
            "avg_win": None if self.avg_win is None else str(self.avg_win),
            "avg_loss": None if self.avg_loss is None else str(self.avg_loss),
            "max_loss": None if self.max_loss is None else str(self.max_loss),
            "per_contract": {
                c: {"count": s.count, "pnl": str(s.pnl)} for c, s in self.per_contract.items()
            },
        }


def _div(numerator: Decimal, denominator: Decimal, quantum: Decimal) -> Decimal | None:
    """Decimal 除法：分母为 0 返回 None，否则按 quantum 量化。"""
    if denominator == 0:
        return None
    return (numerator / denominator).quantize(quantum)


def compute_review_stats(trades: list[Trade]) -> ReviewStats:
    """按 spec §6 口径统计平仓成交。入参已过滤/去重（见模块docstring假设）。"""
    sample = [t for t in trades if t.source in CLOSE_SOURCES]
    wins = [t.pnl for t in sample if t.pnl > 0]
    losses = [t.pnl for t in sample if t.pnl < 0]
    total_profit = sum(wins, Decimal(0))
    total_loss = sum(losses, Decimal(0))
    per_contract: dict[str, ContractStat] = {}
    for t in sample:
        prev = per_contract.get(t.contract, ContractStat(count=0, pnl=Decimal(0)))
        per_contract[t.contract] = ContractStat(count=prev.count + 1, pnl=prev.pnl + t.pnl)
    count = len(sample)
    return ReviewStats(
        close_count=count,
        total_pnl=sum((t.pnl for t in sample), Decimal(0)),
        win_count=len(wins),
        win_rate=_div(Decimal(len(wins)), Decimal(count), _RATIO_Q),
        total_profit=total_profit,
        total_loss=total_loss,
        profit_factor=_div(total_profit, abs(total_loss), _RATIO_Q),
        avg_win=_div(total_profit, Decimal(len(wins)), _AVG_Q),
        avg_loss=_div(total_loss, Decimal(len(losses)), _AVG_Q),
        max_loss=min(losses) if losses else None,
        per_contract=per_contract,
    )


def _fmt(value: Decimal | None, quantum: Decimal = Decimal("0.01")) -> str:
    """展示用格式化：None 显示为「数据不足」，Decimal 按分位量化。"""
    return "数据不足" if value is None else str(value.quantize(quantum))


def format_stats_text(stats: ReviewStats) -> str:
    """中文纯文本统计结果（供 get_review_stats 工具返回与复盘简报预统计段）。"""
    lines = [
        f"平仓笔数：{stats.close_count}；总盈亏：{stats.total_pnl}",
        f"胜率：{_fmt(stats.win_rate, Decimal('0.0001'))}（{stats.win_count}/{stats.close_count}）",
        f"盈亏比：{_fmt(stats.profit_factor, Decimal('0.0001'))}"
        f"；总盈利：{stats.total_profit}；总亏损：{stats.total_loss}",
        f"平均盈利：{_fmt(stats.avg_win)}；平均亏损：{_fmt(stats.avg_loss)}"
        f"；最大单笔亏损：{_fmt(stats.max_loss)}",
    ]
    if stats.per_contract:
        dist = "；".join(
            f"{contract} {s.count} 笔（盈亏 {s.pnl}）"
            for contract, s in sorted(stats.per_contract.items())
        )
        lines.append(f"各合约分布：{dist}")
    else:
        lines.append("各合约分布：无平仓样本")
    return "\n".join(lines)
