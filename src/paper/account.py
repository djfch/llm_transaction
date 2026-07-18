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
    """一笔成交的账本记录，供审计与测试断言。"""

    order_id: str
    contract: str
    size: Decimal  # 成交张数，正买负卖
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal  # 本笔成交结算的已实现盈亏（开仓为 0）
    maker: bool


class PaperAccount:
    """虚拟账户账本。所有金额为结算币种（如 USDT），用 Decimal 计算。"""

    def __init__(self, initial_equity: Decimal) -> None:
        self.available = initial_equity  # 可用余额
        self.positions: dict[str, PaperPosition] = {}
        self.fills: list[FillRecord] = []
        self.total_fee = Decimal(0)  # 累计手续费
        self.total_funding = Decimal(0)  # 累计资金费（负=净支出）
        self.total_realized = Decimal(0)  # 累计已实现盈亏（不含费用）

    @staticmethod
    def notional(size: Decimal, price: Decimal, quanto: Decimal) -> Decimal:
        return abs(size) * price * quanto

    def position(self, contract: str) -> PaperPosition | None:
        pos = self.positions.get(contract)
        return pos if pos is not None and pos.size != 0 else None

    def ensure_position(self, contract: str, leverage: Decimal) -> PaperPosition:
        pos = self.positions.get(contract)
        if pos is None:
            pos = PaperPosition(contract=contract, leverage=leverage)
            self.positions[contract] = pos
        return pos

    def unrealised(self, contract: str, mark_price: Decimal, quanto: Decimal) -> Decimal:
        pos = self.position(contract)
        if pos is None:
            return Decimal(0)
        direction = Decimal(1) if pos.size > 0 else Decimal(-1)
        return (mark_price - pos.entry_price) * abs(pos.size) * quanto * direction

    def equity(self, marks: dict[str, Decimal], quantos: dict[str, Decimal]) -> Decimal:
        """总权益 = 可用 + Σ保证金 + Σ未实现盈亏。无行情的持仓按成本价估值。"""
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
        """
        fee = self.notional(size, price, quanto) * fee_rate
        realized = Decimal(0)
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
        """翻仓余额预检：模拟平仓返还后的可用余额须覆盖新开仓保证金+手续费。"""
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
        """开仓/加仓：同向加权均价，追加保证金；余额不足则拒绝。"""
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
        """平仓 closed 张：按比例释放保证金并返还余额；返回已实现盈亏（以释放保证金为下限）。"""
        direction = Decimal(1) if pos.size > 0 else Decimal(-1)
        released = pos.margin * closed / abs(pos.size)
        pnl = (price - pos.entry_price) * closed * quanto * direction
        self.available += max(released + pnl, Decimal(0))  # 逐仓亏损以保证金为限
        pos.margin -= released
        pos.size -= direction * closed
        if pos.size == 0:
            pos.entry_price = Decimal(0)
        return max(pnl, -released)  # 统计与余额同口径：亏损以释放保证金为限
