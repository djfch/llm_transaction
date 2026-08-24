"""风控数据模型：交易意图、判定结果、账户/持仓/当日统计快照。

当前风控模型约定：
- 金额一律用 Decimal，禁止使用 float
- side_size 为张数（非币数），正=开多/买入，负=开空/卖出
- 平仓/减仓（is_close=True）属降风险操作，只受价格偏离规则约束，其余规则一律豁免
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class TradeIntent(BaseModel):
    """交易意图（下单前的业务语义，由 agent 工具层构造）。"""

    contract: str
    side_size: Decimal  # 张数，正=多，负=空
    price: Decimal | None = Field(default=None, gt=0)  # None=市价单
    is_close: bool = False  # True=平仓/减仓（reduce_only/close 语义）
    leverage: int = Field(ge=1)  # 请求杠杆倍数
    mark_price: Decimal = Field(gt=0)  # 当前标记价（市价单估值与价格偏离判定用）
    quanto_multiplier: Decimal = Field(gt=0)  # 每张合约对应的币数
    planned_stop_risk: Decimal | None = Field(default=None, ge=0)  # 成交后整仓计划止损金额


class OpenOrderIntent(BaseModel):
    """未成交挂单快照（风控敞口计算用，issue #58）。

    由 agent 工具层从网关挂单列表组装；price/size_left 为挂单价与剩余张数。
    """

    contract: str
    price: Decimal = Field(ge=0)  # 挂单价；个别来源缺价时为 0，敞口计算侧跳过
    size_left: Decimal  # 无符号剩余张数（|剩余量|）
    quanto_multiplier: Decimal = Field(gt=0)


class Verdict(BaseModel):
    """风控判定结果：allowed=True 放行；False 拒绝并附全部命中理由。"""

    allowed: bool
    reasons: list[str] = []

    @classmethod
    def allow(cls) -> Verdict:
        """构造放行判定：允许该交易意图执行，不带任何拒绝理由。

        参数：无

        返回：
            Verdict：allowed=True、reasons 为空的判定结果
        """
        return cls(allowed=True)

    @classmethod
    def deny(cls, reasons: list[str]) -> Verdict:
        """构造拒绝判定：禁止该交易意图执行，并附全部命中的风控理由。

        参数：
            reasons: list[str]，各风控规则命中的拒绝理由列表

        返回：
            Verdict：allowed=False、附带全部拒绝理由的判定结果
        """
        return cls(allowed=False, reasons=reasons)


class PositionSnapshot(BaseModel):
    """持仓快照（风控视角，与 gateway 模型解耦，自带估值所需字段）。"""

    contract: str
    size: Decimal  # 张数，正多负空
    mark_price: Decimal = Field(gt=0)
    quanto_multiplier: Decimal = Field(gt=0)


class AccountSnapshot(BaseModel):
    """账户快照。equity 为账户权益（各项占比规则的估值基准）。"""

    equity: Decimal = Field(gt=0)
    unrealised_pnl: Decimal  # 未实现盈亏（账户级）


class DailyStats(BaseModel):
    """当日统计（自然日维度，由调用方按日重置）。"""

    realized_pnl: Decimal  # 当日已实现盈亏
    orders_today: int = Field(ge=0)  # 当日已下单数
