"""内存 mock 网关：实现 Gateway 同一接口，供单元测试与 agent loop 联调。

撮合语义刻意保持简单（真实撮合在 paper/ 实现）：
- 市价单（price 为 None）立即按合约 mark_price 成交并更新持仓
- 限价单保持 open，可改单/撤单
- 持仓聚合：同向加仓按加权均价，反向先平仓，翻仓部分以成交价开仓
- reduce_only 单校验：无持仓或方向与持仓同向（会加仓）时抛 GatewayError（对齐真实 Gate）
"""

from __future__ import annotations

from decimal import Decimal

from .base import (
    Account,
    Candle,
    Contract,
    ContractNotFound,
    GatewayError,
    OrderNotFound,
    OrderRequest,
    OrderResult,
    Position,
    Ticker,
    TpslOrder,
)


def _default_position(contract: str, mark_price: Decimal) -> Position:
    return Position(
        contract=contract,
        size=Decimal(0),
        entry_price=Decimal(0),
        mark_price=mark_price,
        liq_price=Decimal(0),
        leverage=Decimal(1),
        margin=Decimal(0),
        unrealised_pnl=Decimal(0),
    )


class MockGateway:
    """内存版 Gateway。placed 记录全部下单请求，便于断言。"""

    def __init__(
        self,
        contracts: dict[str, Contract] | None = None,
        account: Account | None = None,
        positions: dict[str, Position] | None = None,
    ) -> None:
        self.contracts = contracts or {}
        self.account = account or Account(available=Decimal("10000"), unrealised_pnl=Decimal(0))
        self.positions = positions or {}
        self.tickers: list[Ticker] = []
        self.candles: list[Candle] = []
        self.orders: dict[str, OrderResult] = {}
        self.placed: list[OrderRequest] = []
        self.tpsl_orders: dict[str, TpslOrder] = {}
        self._order_seq = 0

    def get_contract(self, contract: str) -> Contract:
        if contract not in self.contracts:
            raise ContractNotFound(f"合约不存在: {contract}", label="CONTRACT_NOT_FOUND")
        return self.contracts[contract]

    def get_account(self) -> Account:
        return self.account

    def list_positions(self) -> list[Position]:
        result = []
        for pos in self.positions.values():
            if pos.size == 0:
                continue
            direction = 1 if pos.size > 0 else -1
            mine = [o for o in self.list_tpsl_orders(pos.contract) if o.direction == direction]
            result.append(
                pos.model_copy(
                    update={
                        "stop_loss_price": next(
                            (o.trigger_price for o in mine if o.kind == "stop_loss"), None
                        ),
                        "take_profit_price": next(
                            (o.trigger_price for o in mine if o.kind == "take_profit"), None
                        ),
                    }
                )
            )
        return result

    def place_order(self, req: OrderRequest) -> OrderResult:
        self.placed.append(req)
        self._order_seq += 1
        order_id = str(self._order_seq)
        if req.close:
            return self._close_position(req, order_id)
        if req.reduce_only:
            self._check_reduce_only(req)
        if req.price is None:  # 市价单：立即成交
            fill_price = self._mark_price(req.contract)
            size = -abs(req.size) if req.reduce_only and self._is_long(req.contract) else req.size
            self._apply_fill(req.contract, size, fill_price)
            result = OrderResult(
                id=order_id,
                contract=req.contract,
                status="finished",
                left=Decimal(0),
                fill_price=fill_price,
                finish_as="filled",
                text=req.text or "",
            )
        else:  # 限价单：挂单等待
            result = OrderResult(
                id=order_id,
                contract=req.contract,
                status="open",
                left=abs(req.size),
                fill_price=Decimal(0),
                text=req.text or "",
            )
        self.orders[order_id] = result
        if result.status == "finished":
            self._apply_request_tpsl(req)
        return result

    def _check_reduce_only(self, req: OrderRequest) -> None:
        """reduce_only 校验：无持仓或下单方向与持仓同向（会加仓）时拒绝，对齐真实 Gate。"""
        pos = self.positions.get(req.contract)
        reducing = pos is not None and pos.size != 0 and (pos.size > 0) != (req.size > 0)
        if not reducing:
            raise GatewayError("reduce_only 单与当前持仓同向或无持仓", label="REDUCE_ONLY")

    def _close_position(self, req: OrderRequest, order_id: str) -> OrderResult:
        """平仓：平掉该合约全部持仓（对应真实网关 size=0+close=true）。"""
        pos = self.positions.get(req.contract)
        size = pos.size if pos else Decimal(0)
        fill_price = self._mark_price(req.contract)
        if size != 0:
            self._apply_fill(req.contract, -size, fill_price)
        result = OrderResult(
            id=order_id,
            contract=req.contract,
            status="finished",
            left=Decimal(0),
            fill_price=fill_price,
            finish_as="filled",
            text=req.text or "",
        )
        self.orders[order_id] = result
        self._clear_tpsl(req.contract)
        return result

    def _apply_fill(self, contract: str, size: Decimal, price: Decimal) -> None:
        """按成交更新持仓：同向加仓加权均价，反向减仓，翻仓以新价开仓。"""
        pos = self.positions.get(contract) or _default_position(contract, price)
        new_size = pos.size + size
        if pos.size == 0 or (pos.size > 0) == (size > 0):
            total = abs(pos.size) + abs(size)
            entry = (pos.entry_price * abs(pos.size) + price * abs(size)) / total
        elif new_size == 0 or (new_size > 0) == (pos.size > 0):
            entry = pos.entry_price if new_size != 0 else Decimal(0)
        else:  # 翻仓：剩余部分以成交价开仓
            entry = price
        self.positions[contract] = pos.model_copy(
            update={"size": new_size, "entry_price": entry, "mark_price": price}
        )
        self._clear_stale_tpsl(contract)

    def _mark_price(self, contract: str) -> Decimal:
        c = self.contracts.get(contract)
        if c is not None:
            return c.mark_price
        pos = self.positions.get(contract)
        return pos.mark_price if pos else Decimal(0)

    def _is_long(self, contract: str) -> bool:
        pos = self.positions.get(contract)
        return bool(pos and pos.size > 0)

    def amend_order(
        self,
        contract: str,
        order_id: str,
        price: Decimal | None = None,
        size: Decimal | None = None,
    ) -> OrderResult:
        order = self._open_order(order_id)
        # mock 未成交，改 size 即改 left；价格仅记录（不影响结果模型字段）
        new_left = abs(size) if size is not None else order.left
        amended = order.model_copy(update={"left": new_left})
        self.orders[order_id] = amended
        return amended

    def cancel_order(self, contract: str, order_id: str) -> OrderResult:
        order = self._open_order(order_id)
        cancelled = order.model_copy(update={"status": "finished", "finish_as": "cancelled"})
        self.orders[order_id] = cancelled
        return cancelled

    def _open_order(self, order_id: str) -> OrderResult:
        order = self.orders.get(order_id)
        if order is None or order.status != "open":
            raise OrderNotFound(f"订单不存在或非 open: {order_id}", label="ORDER_NOT_FOUND")
        return order

    def list_orders(self, contract: str, status: str = "open") -> list[OrderResult]:
        return [o for o in self.orders.values() if o.contract == contract and o.status == status]

    def list_tpsl_orders(self, contract: str) -> list[TpslOrder]:
        return [order for order in self.tpsl_orders.values() if order.contract == contract]

    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder:
        self._order_seq += 1
        created = order.model_copy(update={"id": f"tpsl-{self._order_seq}"})
        self.tpsl_orders[created.id] = created
        return created

    def cancel_tpsl_order(self, order_id: str) -> None:
        if order_id not in self.tpsl_orders:
            raise OrderNotFound(f"止盈止损单不存在: {order_id}", label="ORDER_NOT_FOUND")
        del self.tpsl_orders[order_id]

    def _apply_request_tpsl(self, req: OrderRequest) -> None:
        if req.stop_loss_price is None:
            return
        pos = self.positions.get(req.contract)
        if pos is None or pos.size == 0:
            return
        direction = 1 if pos.size > 0 else -1
        self._clear_tpsl(req.contract, direction)
        self.create_tpsl_order(
            TpslOrder(
                id="",
                contract=req.contract,
                direction=direction,
                kind="stop_loss",
                trigger_price=req.stop_loss_price,
            )
        )
        if req.take_profit_price is not None:
            self.create_tpsl_order(
                TpslOrder(
                    id="",
                    contract=req.contract,
                    direction=direction,
                    kind="take_profit",
                    trigger_price=req.take_profit_price,
                )
            )

    def _clear_tpsl(self, contract: str, direction: int | None = None) -> None:
        for order in list(self.tpsl_orders.values()):
            if order.contract == contract and (direction is None or order.direction == direction):
                del self.tpsl_orders[order.id]

    def _clear_stale_tpsl(self, contract: str) -> None:
        pos = self.positions.get(contract)
        direction = None if pos is None or pos.size == 0 else (1 if pos.size > 0 else -1)
        for order in list(self.tpsl_orders.values()):
            if order.contract == contract and (direction is None or order.direction != direction):
                del self.tpsl_orders[order.id]

    def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        if limit is not None and (from_ts is not None or to_ts is not None):
            raise ValueError("limit 与 from/to 互斥，不能同时传")
        candles = self.candles
        if from_ts is not None:
            candles = [c for c in candles if c.t >= from_ts]
        if to_ts is not None:
            candles = [c for c in candles if c.t <= to_ts]
        return candles[:limit] if limit is not None else candles

    def get_tickers(self) -> list[Ticker]:
        return list(self.tickers)

    def set_leverage(self, contract: str, leverage: int, margin_mode: str = "isolated") -> Position:
        if margin_mode not in ("isolated", "cross"):
            raise ValueError(f"非法 margin_mode: {margin_mode}（可选 isolated/cross）")
        pos = self.positions.get(contract) or _default_position(
            contract, self._mark_price(contract)
        )
        updated = pos.model_copy(update={"leverage": Decimal(leverage)})
        self.positions[contract] = updated
        return updated
