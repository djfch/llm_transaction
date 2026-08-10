"""paper 模式虚拟账户：权益、保证金与持仓账本。

记账模型（逐仓简化）：
- 权益 equity = available(可用余额) + Σ持仓 margin(占用保证金) + Σ未实现盈亏
- 开仓：available 扣除 保证金+手续费；保证金 = 名义价值 / 杠杆
- 平仓：按平仓比例释放保证金并结算已实现盈亏；逐仓亏损以保证金为限
- size 正=多、负=空；名义价值 = |size| × price × quanto_multiplier
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from ..gateway.base import GatewayError


class PaperPosition(BaseModel):
    """虚拟持仓。margin 为该仓占用的逐仓保证金。"""

    contract: str
    size: Decimal = Decimal(0)  # 正=多，负=空
    entry_price: Decimal = Decimal(0)
    leverage: Decimal = Decimal(1)
    margin: Decimal = Decimal(0)


class FillRecord(BaseModel):
    """一笔成交的账本记录，供审计与测试断言。

    is_close：平仓/减仓成交为 True（翻仓含平仓部分亦记 True），开仓为 False，
    强平成交恒 True；供落库 trades.source（llm_open/llm_close/liquidation）判定。
    """

    order_id: str
    contract: str
    size: Decimal  # 成交张数，正买负卖
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal  # 本笔成交结算的已实现盈亏（开仓为 0）
    maker: bool
    is_close: bool


class PaperAccount:
    """虚拟账户账本。所有金额为结算币种（如 USDT），用 Decimal 计算。"""

    def __init__(self, initial_equity: Decimal) -> None:
        """创建虚拟账户，以初始权益作为可用余额，并清空持仓、成交记录与各项累计统计。

        参数：
            initial_equity: Decimal，初始权益（同时作为初始可用余额）

        返回：
            None，就地初始化账户内部状态
        """
        self.available = initial_equity  # 可用余额
        self.positions: dict[str, PaperPosition] = {}
        self.fills: list[FillRecord] = []
        self.total_fee = Decimal(0)  # 累计手续费
        self.total_funding = Decimal(0)  # 累计资金费（负=净支出）
        self.total_realized = Decimal(0)  # 累计已实现盈亏（不含费用）

    @staticmethod
    def notional(size: Decimal, price: Decimal, quanto: Decimal) -> Decimal:
        """计算一笔持仓或成交的名义价值，即 |张数| × 价格 × 合约乘数。

        参数：
            size: Decimal，张数（正多负空，正负号不影响名义价值）
            price: Decimal，价格
            quanto: Decimal，合约乘数（quanto_multiplier，每张合约对应的基础资产数量）

        返回：
            Decimal：名义价值（结算币种金额）
        """
        return abs(size) * price * quanto

    def position(self, contract: str) -> PaperPosition | None:
        """读取某合约当前的有效持仓。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            PaperPosition | None：持仓对象；从未持仓或张数已归零时返回 None
        """
        pos = self.positions.get(contract)
        return pos if pos is not None and pos.size != 0 else None

    def ensure_position(self, contract: str, leverage: Decimal) -> PaperPosition:
        """获取某合约的持仓对象，不存在则按给定杠杆新建一个空持仓并登记入账。

        参数：
            contract: str，合约名（如 BTC_USDT）
            leverage: Decimal，新建持仓时使用的杠杆倍数；持仓已存在时不生效

        返回：
            PaperPosition：已有的或新建的持仓对象
        """
        pos = self.positions.get(contract)
        if pos is None:
            pos = PaperPosition(contract=contract, leverage=leverage)
            self.positions[contract] = pos
        return pos

    def unrealised(self, contract: str, mark_price: Decimal, quanto: Decimal) -> Decimal:
        """按标记价格计算某合约持仓的未实现盈亏。

        参数：
            contract: str，合约名（如 BTC_USDT）
            mark_price: Decimal，标记价格（最新行情价）
            quanto: Decimal，合约乘数（quanto_multiplier，每张合约对应的基础资产数量）

        返回：
            Decimal：未实现盈亏（正为浮盈、负为浮亏）；无有效持仓时返回 0
        """
        pos = self.position(contract)
        if pos is None:
            return Decimal(0)
        direction = Decimal(1) if pos.size > 0 else Decimal(-1)
        return (mark_price - pos.entry_price) * abs(pos.size) * quanto * direction

    def equity(self, marks: dict[str, Decimal], quantos: dict[str, Decimal]) -> Decimal:
        """总权益 = 可用 + Σ保证金 + Σ未实现盈亏。无行情的持仓按成本价估值。

        参数：
            marks: dict[str, Decimal]，各合约标记价格
            quantos: dict[str, Decimal]，各合约乘数
        返回：
            Decimal，总权益 = 可用 + Σ保证金 + Σ未实现盈亏。无行情的持仓按成本价估值
        """
        total = self.available
        for contract, pos in self.positions.items():
            if pos.size == 0:
                continue
            mark = marks.get(contract, pos.entry_price)
            quanto = quantos.get(contract, Decimal(1))
            total += pos.margin + self.unrealised(contract, mark, quanto)
        return total

    def apply_fill(
        self,
        order_id: str,
        contract: str,
        size: Decimal,
        price: Decimal,
        quanto: Decimal,
        leverage: Decimal,
        fee_rate: Decimal,
        maker: bool,
    ) -> FillRecord:
        """按成交记账：反向先平仓结算盈亏，剩余部分开仓占用保证金，最后扣手续费。

        翻仓（平仓后仍有剩余需开仓）先做余额预检，不足则整单拒绝、分文不动。

        参数：
            order_id: str，交易所订单标识
            contract: str，合约标识
            size: Decimal，订单张数
            price: Decimal，委托价格；None 表示市价
            quanto: Decimal，合约乘数
            leverage: Decimal，请求杠杆倍数
            fee_rate: Decimal，成交手续费率
            maker: bool，是否按挂单费率计费
        返回：
            FillRecord，按成交记账：反向先平仓结算盈亏，剩余部分开仓占用保证金，最后扣手续费
        """
        fee = self.notional(size, price, quanto) * fee_rate
        realized = Decimal(0)
        closed = Decimal(0)  # 本笔成交中平仓/减仓的张数（0 = 纯开仓）
        pos = self.ensure_position(contract, leverage)
        remaining = size
        if pos.size != 0 and (pos.size > 0) != (size > 0):
            closed = min(abs(size), abs(pos.size))
            remaining = size - closed if size > 0 else size + closed
            if remaining != 0:
                self._preflight_flip(pos, closed, remaining, price, quanto, fee)
            realized = self._reduce(pos, closed, price, quanto)
        if remaining != 0:
            self._open(pos, remaining, price, quanto, fee)
        self.available -= fee
        self.total_fee += fee
        self.total_realized += realized
        record = FillRecord(
            order_id=order_id,
            contract=contract,
            size=size,
            price=price,
            fee=fee,
            realized_pnl=realized,
            maker=maker,
            is_close=closed > 0,  # 含平仓/减仓部分即记 True（翻仓亦然）
        )
        self.fills.append(record)
        return record

    def _preflight_flip(
        self,
        pos: PaperPosition,
        closed: Decimal,
        remaining: Decimal,
        price: Decimal,
        quanto: Decimal,
        fee: Decimal,
    ) -> None:
        """翻仓余额预检：模拟平仓返还后的可用余额须覆盖新开仓保证金+手续费。

        参数：
            pos: PaperPosition，当前合约持仓；无持仓时为 None
            closed: Decimal，本次实际平仓张数
            remaining: Decimal，平仓后剩余待开仓张数
            price: Decimal，委托价格；None 表示市价
            quanto: Decimal，合约乘数
            fee: Decimal，本次成交手续费
        返回：
            None，翻仓余额预检：模拟平仓返还后的可用余额须覆盖新开仓保证金+手续费
        异常：
            GatewayError，平仓返还余额后仍不足以覆盖新仓保证金和手续费时抛出
        """
        direction = Decimal(1) if pos.size > 0 else Decimal(-1)
        released = pos.margin * closed / abs(pos.size)
        pnl = (price - pos.entry_price) * closed * quanto * direction
        projected = self.available + max(released + pnl, Decimal(0))
        need = self.notional(remaining, price, quanto) / pos.leverage
        if projected < need + fee:
            raise GatewayError(
                f"可用余额不足：翻仓需 {need + fee}，平仓返还后可用 {projected}",
                label="INSUFFICIENT_BALANCE",
            )

    def _open(
        self, pos: PaperPosition, size: Decimal, price: Decimal, quanto: Decimal, fee: Decimal
    ) -> None:
        """开仓/加仓：同向加权均价，追加保证金；余额不足则拒绝。

        参数：
            pos: PaperPosition，当前合约持仓；无持仓时为 None
            size: Decimal，订单张数
            price: Decimal，委托价格；None 表示市价
            quanto: Decimal，合约乘数
            fee: Decimal，本次成交手续费
        返回：
            None，开仓/加仓：同向加权均价，追加保证金；余额不足则拒绝
        异常：
            GatewayError，可用余额不足以覆盖新增保证金和手续费时抛出
        """
        need = self.notional(size, price, quanto) / pos.leverage
        if self.available < need + fee:
            raise GatewayError(
                f"可用余额不足：需 {need + fee}，可用 {self.available}",
                label="INSUFFICIENT_BALANCE",
            )
        if pos.size != 0:
            total = abs(pos.size) + abs(size)
            pos.entry_price = (pos.entry_price * abs(pos.size) + price * abs(size)) / total
        else:
            pos.entry_price = price
        pos.size += size
        pos.margin += need
        self.available -= need

    def _reduce(
        self, pos: PaperPosition, closed: Decimal, price: Decimal, quanto: Decimal
    ) -> Decimal:
        """平仓 closed 张：按比例释放保证金并返还余额；返回已实现盈亏（以释放保证金为下限）。

        参数：
            pos: PaperPosition，当前合约持仓；无持仓时为 None
            closed: Decimal，本次实际平仓张数
            price: Decimal，委托价格；None 表示市价
            quanto: Decimal，合约乘数
        返回：
            Decimal，平仓 closed 张：按比例释放保证金并返还余额；返回已实现盈亏（以释放保证金为下限）
        """
        direction = Decimal(1) if pos.size > 0 else Decimal(-1)
        released = pos.margin * closed / abs(pos.size)
        pnl = (price - pos.entry_price) * closed * quanto * direction
        self.available += max(released + pnl, Decimal(0))  # 逐仓亏损以保证金为限
        pos.margin -= released
        pos.size -= direction * closed
        if pos.size == 0:
            pos.entry_price = Decimal(0)
        return max(pnl, -released)  # 统计与余额同口径：亏损以释放保证金为限
