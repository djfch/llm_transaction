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
    OpenInterestPoint,
    Position,
    Ticker,
    TpslOrder,
)


def _default_position(contract: str, mark_price: Decimal) -> Position:
    """构造指定合约的零持仓默认对象（张数、开仓均价、保证金均为 0）。

    参数：
        contract: str，合约名（如 BTC_USDT）
        mark_price: Decimal，标记价格，写入持仓的 mark_price 字段

    返回：
        Position：该合约的空仓对象（张数为 0、杠杆 1）
    """
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
        open_interest: Decimal = Decimal("123456"),
        open_interest_history: dict[str, list[OpenInterestPoint]] | None = None,
    ) -> None:
        """初始化内存网关，注入测试所需的合约、账户、持仓与持仓量数据。

        参数：
            contracts: dict[str, Contract] | None，合约名到合约元数据的映射；None 时为空
            account: Account | None，账户快照；None 时默认可用余额 10000
            positions: dict[str, Position] | None，合约名到持仓的映射；None 时为无持仓
            open_interest: Decimal，固定返回的持仓量张数，默认 123456
            open_interest_history: dict[str, list[OpenInterestPoint]] | None，
                各合约历史持仓量序列；None 时无历史数据

        返回：
            None，初始化实例属性（orders/placed/tpsl_orders 等置空）
        """
        self.contracts = contracts or {}
        self.account = account or Account(available=Decimal("10000"), unrealised_pnl=Decimal(0))
        self.positions = positions or {}
        self.open_interest = open_interest
        self.open_interest_history = open_interest_history or {}
        self.tickers: list[Ticker] = []
        self.candles: list[Candle] = []
        self.orders: dict[str, OrderResult] = {}
        self.placed: list[OrderRequest] = []
        self.tpsl_orders: dict[str, TpslOrder] = {}
        self._order_seq = 0

    def get_contract(self, contract: str) -> Contract:
        """读取单个合约的元数据与标记价格。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            Contract：该合约的元数据对象

        异常：
            ContractNotFound：合约不在注入的合约表中时抛出
        """
        if contract not in self.contracts:
            raise ContractNotFound(f"合约不存在: {contract}", label="CONTRACT_NOT_FOUND")
        return self.contracts[contract]

    def get_cached_contract(self, contract: str) -> Contract:
        """从 mock 内存合约表读取规格，不调用可被故障桩替换的实时查询。

        参数：
            contract: str，合约名

        返回：
            Contract：内存中的测试合约规格

        异常：
            ContractNotFound：合约不在内存表时抛出
        """
        if contract not in self.contracts:
            raise ContractNotFound(f"合约不存在: {contract}", label="CONTRACT_NOT_FOUND")
        return self.contracts[contract]

    def get_account(self) -> Account:
        """读取合约账户快照。

        参数：无

        返回：
            Account：构造时注入的账户对象（可用余额与未实现盈亏）
        """
        return self.account

    def list_positions(self) -> list[Position]:
        """列出全部非零持仓（单次裸读，不回填止盈止损触发价）。

        与 GateRestGateway 裸读语义保持一致：止盈止损触发价由展示路径经
        read_positions_with_tpsl 逐合约补全，安全路径（人工平仓/风控）只依赖
        本裸读，不经任何保护单查询（PR #84 评审 P1）。

        参数：无

        返回：
            list[Position]：非零持仓列表；stop_loss_price/take_profit_price
            保持持仓对象原值（通常为 None），不做保护单回填
        """
        return [pos.model_copy() for pos in self.positions.values() if pos.size != 0]

    def place_order(self, req: OrderRequest) -> OrderResult:
        """下单：市价单立即按标记价格成交，限价单挂为 open 等待改单/撤单。

        参数：
            req: OrderRequest，下单意图；close=True 时平掉该合约全部持仓，
                reduce_only=True 时先校验只减仓方向，price 为 None 表示市价单

        返回：
            OrderResult：市价/平仓单为 finished 且已按标记价格更新持仓；
            限价单为 open 并记录委托快照
        """
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
                size=req.size,
                price=req.price,
                tif=req.tif or "gtc",
                reduce_only=req.reduce_only,
                stop_loss_price=req.stop_loss_price,
                take_profit_price=req.take_profit_price,
                left=abs(req.size),
                fill_price=Decimal(0),
                text=req.text or "",
            )
        self.orders[order_id] = result
        if result.status == "finished":
            self._apply_request_tpsl(req)
        return result

    def _check_reduce_only(self, req: OrderRequest) -> None:
        """reduce_only 校验：无持仓或下单方向与持仓同向（会加仓）时拒绝，对齐真实 Gate。

        参数：
            req: OrderRequest，订单请求

        返回：
            None，reduce_only 校验：无持仓或下单方向与持仓同向（会加仓）时拒绝，对齐真实 Gate

        异常：
            GatewayError，无持仓或 reduce_only 下单方向会增加持仓时抛出
        """
        pos = self.positions.get(req.contract)
        reducing = pos is not None and pos.size != 0 and (pos.size > 0) != (req.size > 0)
        if not reducing:
            raise GatewayError("reduce_only 单与当前持仓同向或无持仓", label="REDUCE_ONLY")

    def _close_position(self, req: OrderRequest, order_id: str) -> OrderResult:
        """平仓：平掉该合约全部持仓（对应真实网关 size=0+close=true）。

        参数：
            req: OrderRequest，订单请求
            order_id: str，交易所订单编号

        返回：
            OrderResult，平仓：平掉该合约全部持仓（对应真实网关 size=0+close=true）
        """
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
        """按成交更新持仓：同向加仓加权均价，反向减仓，翻仓以新价开仓。

        参数：
            contract: str，合约标识
            size: Decimal，带方向的成交张数
            price: Decimal，订单价格，None 表示市价

        返回：
            None，按成交更新持仓：同向加仓加权均价，反向减仓，翻仓以新价开仓
        """
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
        """取合约当前标记价格：优先用合约元数据，否则回退到持仓上的标记价格。

        参数：
            contract: str，合约名

        返回：
            Decimal：标记价格；合约与持仓都不存在时返回 0
        """
        c = self.contracts.get(contract)
        if c is not None:
            return c.mark_price
        pos = self.positions.get(contract)
        return pos.mark_price if pos else Decimal(0)

    def _is_long(self, contract: str) -> bool:
        """判断合约当前是否持有多仓。

        参数：
            contract: str，合约名

        返回：
            bool：持仓张数大于 0 时为 True；无持仓或空仓/零仓时为 False
        """
        pos = self.positions.get(contract)
        return bool(pos and pos.size > 0)

    def amend_order(
        self,
        contract: str,
        order_id: str,
        price: Decimal | None = None,
        size: Decimal | None = None,
    ) -> OrderResult:
        """修改未成交限价单，同步更新展示所需的委托量和委托价快照。

        参数：
            contract: str，合约名（仅对齐接口签名，mock 未按合约索引订单）
            order_id: str，待改订单 id
            price: Decimal | None，新委托价；None 时保持原价
            size: Decimal | None，新委托量（张数）；None 时保持原量

        返回：
            OrderResult：改单后的订单快照（left 同步为新委托量的绝对值）
        """
        order = self._open_order(order_id)
        # mock 未成交，改 size 即改 left；价格仅记录（不影响结果模型字段）
        new_left = abs(size) if size is not None else order.left
        updates: dict[str, Decimal | None] = {"left": new_left}
        if size is not None:
            updates["size"] = size
        if price is not None:
            updates["price"] = price
        amended = order.model_copy(update=updates)
        self.orders[order_id] = amended
        return amended

    def cancel_order(self, contract: str, order_id: str) -> OrderResult:
        """撤销未成交订单，状态置为 finished、finish_as 置为 cancelled。

        参数：
            contract: str，合约名（仅对齐接口签名，mock 未按合约索引订单）
            order_id: str，待撤订单 id

        返回：
            OrderResult：撤单后的订单快照
        """
        order = self._open_order(order_id)
        cancelled = order.model_copy(update={"status": "finished", "finish_as": "cancelled"})
        self.orders[order_id] = cancelled
        return cancelled

    def _open_order(self, order_id: str) -> OrderResult:
        """按 id 取出仍处于 open 状态的订单。

        参数：
            order_id: str，订单 id

        返回：
            OrderResult：该订单的当前快照

        异常：
            OrderNotFound：订单不存在或已不是 open 状态时抛出
        """
        order = self.orders.get(order_id)
        if order is None or order.status != "open":
            raise OrderNotFound(f"订单不存在或非 open: {order_id}", label="ORDER_NOT_FOUND")
        return order

    def list_orders(
        self,
        contract: str | None = None,
        status: str = "open",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[OrderResult]:
        """按合约、状态和分页规则返回与真实网关一致的订单快照。

        参数：
            contract: str | None，合约名；None 时返回全部合约的订单
            status: str，订单状态过滤（如 open/finished），默认 open
            limit: int | None，最多返回条数；None 时不限
            offset: int，分页起始下标，默认 0

        返回：
            list[OrderResult]：满足条件的订单快照列表
        """
        orders = [
            o
            for o in self.orders.values()
            if (contract is None or o.contract == contract) and o.status == status
        ]
        return orders[offset:] if limit is None else orders[offset : offset + limit]

    def list_tpsl_orders(self, contract: str) -> list[TpslOrder]:
        """列出合约当前全部止盈止损保护单。

        参数：
            contract: str，合约名

        返回：
            list[TpslOrder]：该合约的止盈止损单列表；无单时为空列表
        """
        return [order for order in self.tpsl_orders.values() if order.contract == contract]

    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder:
        """登记一条止盈止损保护单并分配自增 id。

        参数：
            order: TpslOrder，止盈止损单内容（传入的 id 会被覆盖）

        返回：
            TpslOrder：登记后的止盈止损单（id 形如 tpsl-N）
        """
        self._order_seq += 1
        created = order.model_copy(update={"id": f"tpsl-{self._order_seq}"})
        self.tpsl_orders[created.id] = created
        return created

    def cancel_tpsl_order(self, order_id: str) -> None:
        """撤销指定止盈止损保护单。

        参数：
            order_id: str，止盈止损单 id

        返回：
            None，从内存表中删除该单

        异常：
            OrderNotFound：止盈止损单不存在时抛出
        """
        if order_id not in self.tpsl_orders:
            raise OrderNotFound(f"止盈止损单不存在: {order_id}", label="ORDER_NOT_FOUND")
        del self.tpsl_orders[order_id]

    def _apply_request_tpsl(self, req: OrderRequest) -> None:
        """市价/平仓单成交后，按下单请求附带的止损/止盈价重建整仓保护单。

        参数：
            req: OrderRequest，下单请求；stop_loss_price 为 None 时直接不处理

        返回：
            None，先清掉同方向旧保护单，再按需创建新止损/止盈单
        """
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
        """删除合约的止盈止损保护单，可按方向过滤。

        参数：
            contract: str，合约名
            direction: int | None，保护方向（1 保护多仓，-1 保护空仓）；
                None 时删除该合约全部保护单

        返回：
            None，就地从内存表中删除止盈止损单
        """
        for order in list(self.tpsl_orders.values()):
            if order.contract == contract and (direction is None or order.direction == direction):
                del self.tpsl_orders[order.id]

    def _clear_stale_tpsl(self, contract: str) -> None:
        """成交后清理失效的止盈止损保护单。

        参数：
            contract: str，合约名

        返回：
            None，无持仓时清空该合约全部保护单，否则删除与持仓方向不一致的保护单
        """
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
        """返回预注入的 K 线序列，支持按时间范围过滤与条数截断。

        参数：
            contract: str，合约名（仅对齐接口签名，mock 只有一份 K 线数据）
            interval: str，K 线周期（仅对齐接口签名，不参与过滤），默认 1m
            limit: int | None，最多返回条数；None 时返回全部
            from_ts: int | None，起始时间（秒级时间戳，含边界）；None 时不限
            to_ts: int | None，结束时间（秒级时间戳，含边界）；None 时不限

        返回：
            list[Candle]：满足条件的 K 线列表

        异常：
            ValueError：limit 与 from_ts/to_ts 同时传入时抛出
        """
        if limit is not None and (from_ts is not None or to_ts is not None):
            raise ValueError("limit 与 from/to 互斥，不能同时传")
        candles = self.candles
        if from_ts is not None:
            candles = [c for c in candles if c.t >= from_ts]
        if to_ts is not None:
            candles = [c for c in candles if c.t <= to_ts]
        return candles[:limit] if limit is not None else candles

    def get_tickers(self) -> list[Ticker]:
        """返回预注入的 ticker 摘要列表。

        参数：无

        返回：
            list[Ticker]：注入的 ticker 列表副本；未注入时为空列表
        """
        return list(self.tickers)

    def fetch_open_interest(self, contract: str) -> Decimal | None:
        """mock 固定持仓量：构造时可注入，默认 123456。

        参数：
            contract: str，合约标识

        返回：
            Decimal | None，mock 固定持仓量：构造时可注入，默认 123456
        """
        return self.open_interest

    def fetch_open_interest_history(
        self, contract: str, interval: str, limit: int = 3
    ) -> list[OpenInterestPoint]:
        """返回构造时注入的历史持仓量，默认无历史数据。

        参数：
            contract: str，合约标识
            interval: str，K 线周期
            limit: int，返回记录数量上限

        返回：
            list[OpenInterestPoint]，返回构造时注入的历史持仓量，默认无历史数据
        """
        return self.open_interest_history.get(contract, [])[-limit:]

    def set_leverage(self, contract: str, leverage: int, margin_mode: str = "isolated") -> Position:
        """设置合约杠杆倍数与保证金模式（逐仓改 leverage，全仓改 cross_leverage_limit）。

        参数：
            contract: str，合约名
            leverage: int，目标杠杆倍数
            margin_mode: str，保证金模式（isolated/cross），默认 isolated

        返回：
            Position：更新杠杆后的持仓对象；无持仓时先按零持仓创建

        异常：
            ValueError：margin_mode 不是 isolated/cross 时抛出
        """
        if margin_mode not in ("isolated", "cross"):
            raise ValueError(f"非法 margin_mode: {margin_mode}（可选 isolated/cross）")
        pos = self.positions.get(contract) or _default_position(
            contract, self._mark_price(contract)
        )
        if margin_mode == "cross":
            updated = pos.model_copy(
                update={
                    "leverage": Decimal(0),
                    "margin_mode": "cross",
                    "cross_leverage_limit": Decimal(leverage),
                }
            )
        else:
            updated = pos.model_copy(
                update={
                    "leverage": Decimal(leverage),
                    "margin_mode": "isolated",
                    "cross_leverage_limit": None,
                }
            )
        self.positions[contract] = updated
        return updated
