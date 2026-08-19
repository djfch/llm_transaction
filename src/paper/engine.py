"""PaperGateway：真实行情驱动的模拟撮合引擎，实现 Gateway 同一接口。

撮合规则（保守，宁差勿好）：
- 市价单：买按 mark×(1+slippage)、卖按 mark×(1-slippage) 成交，taker 费率
- 限价买单 price ≥ best_ask 才成交，成交价 = best_ask（不吃更优价）；限价卖单对称
- 立即成交的限价单按 taker 扣费；挂单后续被行情扫到按 maker 扣费
- 未注入盘口时 best_bid/best_ask 同时取 mark_price
- 资金费由外部调 settle_funding(contract, rate) 触发；强平在每次 on_price 后检查
- get_candlesticks/get_tickers 默认走注入的 provider（行情缓存代理），无 provider 时
  ticker 由行情快照合成、K 线返回空
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal

from ..config import PaperConfig
from ..gateway.base import (
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
from .account import FillRecord, PaperAccount
from .convert import PriceSnap, RestingOrder, synth_ticker, to_position
from .funding import settle_funding as _settle_funding
from .liquidation import LiquidationEvent, liquidate, should_liquidate

from .market_stats import PaperOpenInterestMixin


class PaperGateway(PaperOpenInterestMixin):
    """模拟撮合网关。paper 模式下替代真实网关，供 agent / 风控无差别调用。"""

    # 纯内存方法标记（统一卸载层 async_io 识别）：命中方法在事件循环线程内联执行，
    # 不进 executor——on_price/settle_funding/drain_fills 本就在事件循环线程直接改
    # 账户状态，账户类方法再进线程会把单线程状态机变成跨线程共享可变状态（PR #84
    # 评审 P1）。除网关方法外，还登记以本网关为首参、内部仅调纯内存方法的同步事务
    # 辅助（position_snapshots / _swap_tpsl_group），保证其事务段同样不被线程交错。
    # get_candlesticks/get_tickers/fetch_open_interest 等行情委托方法可能转发真实
    # REST provider，不得加入本集合。
    __gateway_io_inline__ = frozenset(
        {
            "get_contract",
            "get_account",
            "list_positions",
            "set_leverage",
            "place_order",
            "amend_order",
            "cancel_order",
            "list_orders",
            "list_tpsl_orders",
            "create_tpsl_order",
            "cancel_tpsl_order",
            "position_snapshots",
            "_swap_tpsl_group",
        }
    )

    def __init__(
        self,
        config: PaperConfig,
        contracts: dict[str, Contract] | None = None,
        maintenance_rate: Decimal = Decimal("0.005"),
        candle_provider: Callable[..., list[Candle]] | None = None,
        ticker_provider: Callable[[], list[Ticker]] | None = None,
        oi_provider: Callable[[str], Decimal | None] | None = None,
        oi_history_provider: Callable[[str, str, int], list[OpenInterestPoint]] | None = None,
    ) -> None:
        """初始化模拟撮合网关，建立账户与行情、订单等内部状态。

        参数：
            config: PaperConfig，模拟盘配置（初始权益、滑点等）
            contracts: dict[str, Contract] | None，初始合约表（键为合约名）；省略时为空表
            maintenance_rate: Decimal，默认维持保证金率（单档简化），省略时为 0.005
            candle_provider: Callable[..., list[Candle]] | None，K 线数据提供方；省略时 K 线返回空
            ticker_provider: Callable[[], list[Ticker]] | None，ticker 提供方；省略时由行情快照合成
            oi_provider: Callable[[str], Decimal | None] | None，持仓量提供方；省略时无真实 OI 源
            oi_history_provider: Callable[[str, str, int], list[OpenInterestPoint]] | None，
                持仓量历史提供方；省略时无历史数据

        返回：
            None，初始化内部状态（账户、行情快照、挂单与止盈止损表等）
        """
        self._cfg = config
        self._contracts = dict(contracts or {})
        self._default_maint = maintenance_rate  # 简化：单档维持保证金率
        self._maintenance: dict[str, Decimal] = {}  # 可按合约覆盖（risk_limit_tiers 注入点）
        self._candle_provider = candle_provider
        self._ticker_provider = ticker_provider
        self._oi_provider = oi_provider  # 公共行情网关委托（paper 非 mock 行情时注入真实 OI 源）
        self._oi_history_provider = oi_history_provider
        self.account = PaperAccount(Decimal(str(config.initial_equity)))
        self._snaps: dict[str, PriceSnap] = {}
        self._leverages: dict[str, Decimal] = {}
        self._open: dict[str, RestingOrder] = {}
        self._results: dict[str, OrderResult] = {}
        self._tpsl: dict[str, TpslOrder] = {}
        self.liquidations: list[LiquidationEvent] = []

    def upsert_contract(self, contract: Contract) -> None:
        """写入或更新合约信息（按合约名覆盖）。

        参数：
            contract: Contract，合约对象，以其 name 为键存入合约表

        返回：
            None，就地更新合约表
        """
        self._contracts[contract.name] = contract

    def set_maintenance_rate(self, contract: str, rate: Decimal) -> None:
        """按合约覆盖维持保证金率。

        参数：
            contract: str，合约名
            rate: Decimal，该合约的维持保证金率

        返回：
            None，就地更新维持保证金率表
        """
        self._maintenance[contract] = Decimal(str(rate))

    def on_price(
        self,
        contract: str,
        mark_price: Decimal,
        best_bid: Decimal | None = None,
        best_ask: Decimal | None = None,
    ) -> None:
        """行情注入入口：由外部行情源推送，驱动挂单撮合与强平检查。

        参数：
            contract: str，合约名称
            mark_price: Decimal，当前标记价格
            best_bid: Decimal | None，当前最优买价；缺失时使用标记价
            best_ask: Decimal | None，当前最优卖价；缺失时使用标记价

        返回：
            None：行情注入入口：由外部行情源推送，驱动挂单撮合与强平检查
        """
        mark = Decimal(str(mark_price))
        bid = Decimal(str(best_bid)) if best_bid is not None else mark
        ask = Decimal(str(best_ask)) if best_ask is not None else mark
        self._snaps[contract] = PriceSnap(mark=mark, bid=bid, ask=ask)
        if contract in self._contracts:
            self._contracts[contract] = self._contracts[contract].model_copy(
                update={"mark_price": mark}
            )
        self._match_resting(contract)
        self._trigger_tpsl(contract)
        self._check_liquidation(contract)

    def settle_funding(self, contract: str, rate: Decimal) -> Decimal:
        """资金费结算入口：由外部按 funding_interval 定时触发，返回余额变化。

        参数：
            contract: str，合约名称
            rate: Decimal，资金费率

        返回：
            Decimal：资金费结算入口：由外部按 funding_interval 定时触发，返回余额变化
        """
        c = self.get_contract(contract)
        if self.account.position(contract) is None:
            return Decimal(0)
        return _settle_funding(
            self.account,
            contract,
            Decimal(str(rate)),
            self._snap(contract).mark,
            c.quanto_multiplier,
        )

    def equity(self) -> Decimal:
        """计算账户总权益（可用余额 + 未实现盈亏）。

        参数：无

        返回：
            Decimal：按各合约最新标记价折算的账户总权益
        """
        marks = {name: s.mark for name, s in self._snaps.items()}
        quantos = {name: c.quanto_multiplier for name, c in self._contracts.items()}
        return self.account.equity(marks, quantos)

    def drain_fills(self) -> list[FillRecord]:
        """取走全部成交记录（含强平）并清空缓冲，按时间升序；drain 与落库须在锁内。

        参数：
            无

        返回：
            list[FillRecord]：取走全部成交记录（含强平）并清空缓冲，按时间升序；drain 与落库须在锁内
        """
        fills = self.account.fills
        self.account.fills = []
        return fills

    def reset_account(self, equity: Decimal) -> None:
        """重置模拟账户：清空全部持仓、未成交挂单、成交缓冲与强平记录。

        已完成订单历史、杠杆设置与行情快照保留。

        参数：
            equity: Decimal，重置后的模拟账户权益

        返回：
            None：重置模拟账户：清空全部持仓、未成交挂单、成交缓冲与强平记录
        """
        self.account = PaperAccount(equity)
        self._open.clear()
        self._results = {k: r for k, r in self._results.items() if r.status != "open"}
        self._tpsl.clear()
        self.liquidations.clear()

    def get_contract(self, contract: str) -> Contract:
        """按合约名读取合约信息。

        参数：
            contract: str，合约名

        返回：
            Contract：合约对象

        异常：
            ContractNotFound：合约不存在时抛出
        """
        if contract not in self._contracts:
            raise ContractNotFound(f"合约不存在: {contract}", label="CONTRACT_NOT_FOUND")
        return self._contracts[contract]

    def get_account(self) -> Account:
        """读取账户概览（可用余额与全部持仓未实现盈亏汇总）。

        参数：无

        返回：
            Account：可用余额与未实现盈亏之和
        """
        upnl = sum((p.unrealised_pnl for p in self.list_positions()), Decimal(0))
        return Account(available=self.account.available, unrealised_pnl=upnl)

    def list_positions(self) -> list[Position]:
        """列出全部非零持仓，并附带各持仓同方向的止损/止盈触发价。

        参数：无

        返回：
            list[Position]：持仓列表；止损/止盈价取自同方向止盈止损单，无对应单时为 None
        """
        positions = []
        for pos in self.account.positions.values():
            if pos.size == 0:
                continue
            item = self._position_of(pos)
            direction = 1 if item.size > 0 else -1
            mine = [o for o in self.list_tpsl_orders(item.contract) if o.direction == direction]
            positions.append(
                item.model_copy(
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
        return positions

    def set_leverage(self, contract: str, leverage: int, margin_mode: str = "isolated") -> Position:
        """设置合约杠杆；已有持仓时重算占用保证金并从可用余额划转差额。

        参数：
            contract: str，合约名
            leverage: int，目标杠杆倍数（必须 ≥ 1）
            margin_mode: str，保证金模式（isolated/cross），省略时为 isolated

        返回：
            Position：调杠杆后的持仓对象

        异常：
            ValueError：margin_mode 非法或 leverage 小于 1 时抛出
            GatewayError：持仓存在且调低杠杆需补保证金但可用余额不足时抛出
        """
        if margin_mode not in ("isolated", "cross"):
            raise ValueError(f"非法 margin_mode: {margin_mode}（可选 isolated/cross）")
        if leverage < 1:
            raise ValueError("leverage 必须 ≥ 1")
        c = self.get_contract(contract)
        pos = self.account.ensure_position(contract, Decimal(leverage))
        # 先校验后写入：余额不足抛错时不得留下杠杆已改而保证金未划转的不一致账目
        if pos.size != 0:  # 调杠杆重算占用保证金，差额从可用余额划转
            new_margin = (
                PaperAccount.notional(pos.size, pos.entry_price, c.quanto_multiplier) / leverage
            )
            delta = new_margin - pos.margin
            if delta > 0 and self.account.available < delta:
                raise GatewayError("可用余额不足，无法调低杠杆", label="INSUFFICIENT_BALANCE")
            self.account.available -= delta
            pos.margin = new_margin
        self._leverages[contract] = Decimal(leverage)
        pos.leverage = Decimal(leverage)
        pos.margin_mode = margin_mode  # 持久化模式，to_position 据此映射 Gate 口径的全仓字段
        return self._position_of(pos)

    def place_order(self, req: OrderRequest) -> OrderResult:
        """下单入口：按请求类型分流到一键平仓、市价单或限价单。

        参数：
            req: OrderRequest，下单请求（合约、数量、价格、close/reduce_only 标记等）

        返回：
            OrderResult：订单结果；市价单与立即成交限价单为 filled，未成交限价单为 open

        异常：
            GatewayError：size 为 0 且未用 close 平仓，或 reduce_only 单与持仓同向/无持仓时抛出
        """
        self.get_contract(req.contract)
        order_id = self._next_id()
        text = req.text or self._next_id()  # 客户端订单 ID 同样全局唯一
        if req.close:
            return self._close_all(req, order_id, text)
        if req.size == 0:
            raise GatewayError("size 不能为 0（平仓请用 close=True）", label="INVALID_PARAM")
        if req.reduce_only and not self._is_reducing(req.contract, req.size):
            raise GatewayError("reduce_only 单与当前持仓同向或无持仓", label="REDUCE_ONLY")
        if req.price is None:
            return self._market_order(req, order_id, text)
        return self._limit_order(req, order_id, text)

    def amend_order(
        self,
        contract: str,
        order_id: str,
        price: Decimal | None = None,
        size: Decimal | None = None,
    ) -> OrderResult:
        """修改未成交挂单的价格/数量；改单后将最新委托量和价格写回未成交订单快照。

        改单后立即穿透盘口的按 taker 成交，并登记原单附带的止盈止损。

        参数：
            contract: str，合约名
            order_id: str，订单 ID
            price: Decimal | None，新价格；省略时不改价格
            size: Decimal | None，新委托量（带方向）；省略时不改数量

        返回：
            OrderResult：立即穿透时为成交结果，否则为更新后的挂单快照
        """
        order = self._open_order(contract, order_id)
        if price is not None:
            order.price = price
        if size is not None:
            order.size = size
        if self._crossed(order):  # 改单后立即穿透，按 taker 成交
            snap = self._snaps[contract]
            fill = snap.ask if order.size > 0 else snap.bid
            result = self._execute(
                order_id, contract, order.size, fill, maker=False, text=order.text
            )
            self._apply_tpsl(contract, order.size, order.stop_loss_price, order.take_profit_price)
            return result
        self._results[order_id] = self._results[order_id].model_copy(
            update={"left": abs(order.size), "size": order.size, "price": order.price}
        )
        return self._results[order_id]

    def cancel_order(self, contract: str, order_id: str) -> OrderResult:
        """撤销未成交挂单。

        参数：
            contract: str，合约名
            order_id: str，订单 ID

        返回：
            OrderResult：状态置为 finished、finish_as 为 cancelled 的订单结果
        """
        self._open_order(contract, order_id)
        del self._open[order_id]
        cancelled = self._results[order_id].model_copy(
            update={"status": "finished", "finish_as": "cancelled"}
        )
        self._results[order_id] = cancelled
        return cancelled

    def list_orders(
        self,
        contract: str | None = None,
        status: str = "open",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[OrderResult]:
        """分页读取订单，支持全合约 open 订单的过滤读取。

        参数：
            contract: str | None，合约名过滤；省略时含全部合约
            status: str，订单状态过滤，默认 open
            limit: int | None，每页条数；省略时返回 offset 之后的全部
            offset: int，分页起始偏移

        返回：
            list[OrderResult]：符合条件的订单结果分页
        """
        orders = [
            r
            for r in self._results.values()
            if (contract is None or r.contract == contract) and r.status == status
        ]
        return orders[offset:] if limit is None else orders[offset : offset + limit]

    def list_tpsl_orders(self, contract: str) -> list[TpslOrder]:
        """列出某合约的全部止盈止损单。

        参数：
            contract: str，合约名

        返回：
            list[TpslOrder]：该合约的止盈止损单列表
        """
        return [order for order in self._tpsl.values() if order.contract == contract]

    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder:
        """登记一条止盈止损单并分配 ID。

        参数：
            order: TpslOrder，止盈止损单（传入的 id 会被忽略，重新生成 tpsl- 前缀 ID）

        返回：
            TpslOrder：带新 ID 的止盈止损单
        """
        created = order.model_copy(update={"id": f"tpsl-{self._next_id()}"})
        self._tpsl[created.id] = created
        return created

    def cancel_tpsl_order(self, order_id: str) -> None:
        """撤销止盈止损单。

        参数：
            order_id: str，止盈止损单 ID

        返回：
            None，从止盈止损单表中删除

        异常：
            OrderNotFound：止盈止损单不存在时抛出
        """
        if order_id not in self._tpsl:
            raise OrderNotFound(f"止盈止损单不存在: {order_id}", label="ORDER_NOT_FOUND")
        del self._tpsl[order_id]

    def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """读取 K 线数据，委托给注入的 candle_provider；未注入时返回空列表。

        参数：
            contract: str，合约名
            interval: str，K 线周期，默认 1m
            limit: int | None，返回条数；与 from/to 互斥
            from_ts: int | None，起始时间戳
            to_ts: int | None，结束时间戳

        返回：
            list[Candle]：K 线列表

        异常：
            ValueError：limit 与 from/to 同时传入时抛出
        """
        if limit is not None and (from_ts is not None or to_ts is not None):
            raise ValueError("limit 与 from/to 互斥，不能同时传")
        if self._candle_provider is None:
            return []
        return self._candle_provider(contract, interval, limit, from_ts, to_ts)

    def get_tickers(self) -> list[Ticker]:
        """读取全部 ticker；优先走注入的 ticker_provider，否则由行情快照合成。

        参数：无

        返回：
            list[Ticker]：ticker 列表
        """
        if self._ticker_provider is not None:
            return self._ticker_provider()
        return [synth_ticker(n, s, self._contracts.get(n)) for n, s in self._snaps.items()]

    def _market_order(self, req: OrderRequest, order_id: str, text: str) -> OrderResult:
        """撮合市价单：按标记价加滑点立即以 taker 成交，并登记请求附带的止盈止损。

        参数：
            req: OrderRequest，下单请求
            order_id: str，订单 ID
            text: str，客户端订单 ID

        返回：
            OrderResult：成交结果（finished/filled）
        """
        snap = self._snap(req.contract)
        slip = Decimal(str(self._cfg.slippage))
        price = snap.mark * (1 + slip) if req.size > 0 else snap.mark * (1 - slip)
        result = self._execute(order_id, req.contract, req.size, price, maker=False, text=text)
        self._apply_request_tpsl(req)
        return result

    def _limit_order(self, req: OrderRequest, order_id: str, text: str) -> OrderResult:
        """处理限价单：价格穿透立即按对手价以 taker 成交，否则登记为挂单。

        参数：
            req: OrderRequest，下单请求
            order_id: str，订单 ID
            text: str，客户端订单 ID

        返回：
            OrderResult：立即成交为 filled；未成交为 open 挂单，ioc 单未成交则为已撤销结果
        """
        order = RestingOrder(
            id=order_id,
            contract=req.contract,
            size=req.size,
            price=req.price,
            reduce_only=req.reduce_only,
            stop_loss_price=req.stop_loss_price,
            take_profit_price=req.take_profit_price,
            text=text,
        )
        if self._crossed(order):  # 立即成交按 taker
            snap = self._snaps[req.contract]
            fill = snap.ask if req.size > 0 else snap.bid
            result = self._execute(order_id, req.contract, req.size, fill, maker=False, text=text)
            self._apply_request_tpsl(req)
            return result
        # 挂单卡片依赖原始委托字段，不能只保留 left。
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
            text=text,
        )
        if req.tif == "ioc":  # 未立即成交的 ioc 直接撤销
            result = result.model_copy(update={"status": "finished", "finish_as": "cancelled"})
        else:
            self._open[order_id] = order
        self._results[order_id] = result
        return result

    def _match_resting(self, contract: str) -> None:
        """行情更新后扫描挂单：价格穿透即按对手价成交（maker），不做部分成交。

        触价余额不足的挂单直接撤销（不抛异常），避免阻塞后续强平检查。

        参数：
            contract: str，合约名称

        返回：
            None：行情更新后扫描挂单：价格穿透即按对手价成交（maker），不做部分成交

        异常：
            GatewayError：触价成交失败且原因不是余额不足时原样抛出
        """
        for order in list(self._open.values()):
            if order.contract != contract or not self._crossed(order):
                continue
            if order.reduce_only and not self._is_reducing(contract, order.size):
                self.cancel_order(contract, order.id)  # 仓位已不存在/同向，挂单失效
                continue
            snap = self._snaps[contract]
            fill = snap.ask if order.size > 0 else snap.bid
            try:
                self._execute(order.id, contract, order.size, fill, maker=True, text=order.text)
                self._apply_tpsl(
                    contract, order.size, order.stop_loss_price, order.take_profit_price
                )
            except GatewayError as exc:
                if exc.label != "INSUFFICIENT_BALANCE":
                    raise
                self._cancel_failed(order, exc.label)

    def _trigger_tpsl(self, contract: str) -> None:
        """价格穿越时按市价全平，并清理该方向全部保护单。

        参数：
            contract: str，合约名称

        返回：
            None：价格穿越时按市价全平，并清理该方向全部保护单
        """
        pos = self.account.position(contract)
        if pos is None:
            return
        direction = 1 if pos.size > 0 else -1
        mark = self._snap(contract).mark
        triggered = [
            order
            for order in self.list_tpsl_orders(contract)
            if order.direction == direction
            and (
                (
                    order.kind == "stop_loss"
                    and (
                        (direction > 0 and mark <= order.trigger_price)
                        or (direction < 0 and mark >= order.trigger_price)
                    )
                )
                or (
                    order.kind == "take_profit"
                    and (
                        (direction > 0 and mark >= order.trigger_price)
                        or (direction < 0 and mark <= order.trigger_price)
                    )
                )
            )
        ]
        if not triggered:
            return
        order_id = triggered[0].id
        self._execute(order_id, contract, -pos.size, mark, maker=False, text="tpsl")
        self._clear_tpsl(contract, direction)

    def _apply_request_tpsl(self, req: OrderRequest) -> None:
        """把下单请求附带的止盈止损价登记为保护单。

        参数：
            req: OrderRequest，下单请求（取合约、数量与止盈止损价）

        返回：
            None，转发给 _apply_tpsl 就地登记止盈止损单
        """
        self._apply_tpsl(req.contract, req.size, req.stop_loss_price, req.take_profit_price)

    def _apply_tpsl(
        self,
        contract: str,
        size: Decimal,
        stop_loss_price: Decimal | None,
        take_profit_price: Decimal | None,
    ) -> None:
        """成交后按新止损价重建该持仓方向的止盈止损单（先清旧单再登记）。

        参数：
            contract: str，合约名
            size: Decimal，本次成交数量（当前实现未使用，方向按现有持仓判定）
            stop_loss_price: Decimal | None，止损触发价；为 None 时不做任何处理
            take_profit_price: Decimal | None，止盈触发价；为 None 时不登记止盈单

        返回：
            None，就地更新止盈止损单表
        """
        if stop_loss_price is None:
            return
        pos = self.account.position(contract)
        if pos is None:
            return
        direction = 1 if pos.size > 0 else -1
        self._clear_tpsl(contract, direction)
        self.create_tpsl_order(
            TpslOrder(
                id="",
                contract=contract,
                direction=direction,
                kind="stop_loss",
                trigger_price=stop_loss_price,
            )
        )
        if take_profit_price is not None:
            self.create_tpsl_order(
                TpslOrder(
                    id="",
                    contract=contract,
                    direction=direction,
                    kind="take_profit",
                    trigger_price=take_profit_price,
                )
            )

    def _clear_tpsl(self, contract: str, direction: int | None = None) -> None:
        """清理某合约的止盈止损单，可按持仓方向过滤。

        参数：
            contract: str，合约名
            direction: int | None，持仓方向（1 多 / -1 空）；省略时清理该合约全部方向

        返回：
            None，就地从止盈止损单表中删除
        """
        for order in list(self._tpsl.values()):
            if order.contract == contract and (direction is None or order.direction == direction):
                del self._tpsl[order.id]

    def _clear_stale_tpsl(self, contract: str) -> None:
        """清理与当前持仓方向不一致的失效止盈止损单（无持仓时清理该合约全部保护单）。

        参数：
            contract: str，合约名

        返回：
            None，就地从止盈止损单表中删除失效单
        """
        pos = self.account.position(contract)
        direction = None if pos is None else (1 if pos.size > 0 else -1)
        for order in list(self._tpsl.values()):
            if order.contract == contract and (direction is None or order.direction != direction):
                del self._tpsl[order.id]

    def _cancel_failed(self, order: RestingOrder, reason: str) -> None:
        """撤销触价失败的挂单：从 open 移除，结果标记 cancelled 并记录原因。

        参数：
            order: RestingOrder，待处理的订单对象
            reason: str，操作原因或失败说明

        返回：
            None：撤销触价失败的挂单：从 open 移除，结果标记 cancelled 并记录原因
        """
        self._open.pop(order.id, None)
        self._results[order.id] = self._results[order.id].model_copy(
            update={"status": "finished", "finish_as": f"cancelled:{reason}"}
        )

    def _execute(
        self, order_id: str, contract: str, size: Decimal, price: Decimal, maker: bool, text: str
    ) -> OrderResult:
        """执行成交：记账（保证金/手续费/盈亏）、写订单结果并清理失效止盈止损单。

        参数：
            order_id: str，订单 ID
            contract: str，合约名
            size: Decimal，成交数量（带方向，正买负卖）
            price: Decimal，成交价
            maker: bool，是否按 maker 费率扣费（False 按 taker）
            text: str，客户端订单 ID

        返回：
            OrderResult：成交结果（finished/filled）
        """
        c = self.get_contract(contract)
        fee_rate = c.maker_fee_rate if maker else c.taker_fee_rate
        self.account.apply_fill(
            order_id,
            contract,
            size,
            price,
            c.quanto_multiplier,
            self._leverage(contract),
            fee_rate,
            maker,
        )
        result = OrderResult(
            id=order_id,
            contract=contract,
            status="finished",
            left=Decimal(0),
            fill_price=price,
            finish_as="filled",
            text=text,
        )
        self._results[order_id] = result
        self._open.pop(order_id, None)
        self._clear_stale_tpsl(contract)
        return result

    def _close_all(self, req: OrderRequest, order_id: str, text: str) -> OrderResult:
        """一键平仓：无持仓时直接返回 no_position 结果，有持仓时按市价全平并清理保护单。

        参数：
            req: OrderRequest，平仓请求（close=True）
            order_id: str，订单 ID
            text: str，客户端订单 ID

        返回：
            OrderResult：无持仓时 finish_as 为 no_position；有持仓时为市价成交结果
        """
        pos = self.account.position(req.contract)
        if pos is None:  # 无持仓：不伪装成交，也无需行情（先判持仓再取行情）
            result = OrderResult(
                id=order_id,
                contract=req.contract,
                status="finished",
                left=Decimal(0),
                fill_price=Decimal(0),
                finish_as="no_position",
                text=text,
            )
            self._results[order_id] = result
            return result
        close_req = req.model_copy(update={"size": -pos.size, "close": False})
        result = self._market_order(close_req, order_id, text)
        self._clear_tpsl(req.contract)
        return result

    def _check_liquidation(self, contract: str) -> None:
        """按最新标记价检查持仓是否触发强平，触发则执行强平并记录事件。

        参数：
            contract: str，合约名

        返回：
            None，触发强平时就地记账并向 liquidations 追加事件
        """
        pos = self.account.position(contract)
        if pos is None:
            return
        c = self.get_contract(contract)
        mark = self._snaps[contract].mark
        if should_liquidate(pos, mark, c.quanto_multiplier, self._maint(contract)):
            self.liquidations.append(liquidate(self.account, contract, mark, c.quanto_multiplier))

    def _crossed(self, order: RestingOrder) -> bool:
        """判断限价挂单是否穿透当前盘口（买单价 ≥ 卖一、卖单价 ≤ 买一）。

        参数：
            order: RestingOrder，未成交挂单

        返回：
            bool：已穿透返回 True；无行情快照或未穿透返回 False
        """
        snap = self._snaps.get(order.contract)
        if snap is None:
            return False
        if order.size > 0:
            return order.price >= snap.ask
        return order.price <= snap.bid

    def _mark(self, contract: str, entry_price: Decimal) -> Decimal:
        """取合约最新标记价，无行情快照时回退为给定价格。

        参数：
            contract: str，合约名
            entry_price: Decimal，无行情时的回退价（通常为开仓价）

        返回：
            Decimal：标记价或回退价
        """
        snap = self._snaps.get(contract)
        return snap.mark if snap is not None else entry_price

    def _position_of(self, pos) -> Position:
        """把内部持仓记录转换为对外 Position（按最新标记价估值）。

        参数：
            pos: PaperAccount 内部持仓记录

        返回：
            Position：对外持仓对象
        """
        c = self.get_contract(pos.contract)
        mark = self._mark(pos.contract, pos.entry_price)
        return to_position(pos, c, mark, self._maint(pos.contract), self.account)

    def _snap(self, contract: str) -> PriceSnap:
        """取合约行情快照。

        参数：
            contract: str，合约名

        返回：
            PriceSnap：行情快照（标记价/买一/卖一）

        异常：
            GatewayError：尚无该合约行情（未 on_price 注入）时抛出
        """
        snap = self._snaps.get(contract)
        if snap is None:
            raise GatewayError(
                f"尚无行情: {contract}（需先 on_price 注入）", label="NO_MARKET_DATA"
            )
        return snap

    def _leverage(self, contract: str) -> Decimal:
        """取合约当前杠杆：有持仓取持仓杠杆，否则取已设置的杠杆（默认 1 倍）。

        参数：
            contract: str，合约名

        返回：
            Decimal：杠杆倍数
        """
        pos = self.account.position(contract)
        if pos is not None:
            return pos.leverage
        return self._leverages.get(contract, Decimal(1))

    def _maint(self, contract: str) -> Decimal:
        """取合约维持保证金率：优先按合约覆盖值，否则用默认档。

        参数：
            contract: str，合约名

        返回：
            Decimal：维持保证金率
        """
        return self._maintenance.get(contract, self._default_maint)

    def _is_reducing(self, contract: str, size: Decimal) -> bool:
        """判断委托是否为减仓方向（与现有持仓方向相反）。

        参数：
            contract: str，合约名
            size: Decimal，委托数量（带方向）

        返回：
            bool：有持仓且方向相反返回 True，否则 False
        """
        pos = self.account.position(contract)
        return pos is not None and (pos.size > 0) != (size > 0)

    def _open_order(self, contract: str, order_id: str) -> RestingOrder:
        """读取指定合约的未成交挂单。

        参数：
            contract: str，合约名
            order_id: str，订单 ID

        返回：
            RestingOrder：未成交挂单

        异常：
            OrderNotFound：订单不存在、非 open 或合约不匹配时抛出
        """
        order = self._open.get(order_id)
        if order is None or order.contract != contract:
            raise OrderNotFound(f"订单不存在或非 open: {order_id}", label="ORDER_NOT_FOUND")
        return order

    def _next_id(self) -> str:
        """订单 ID 全局唯一：t- 前缀 + 26 位 uuid hex（与真实网关 gen_client_order_id 同风格）。

        参数：
            无

        返回：
            str：订单 ID 全局唯一：t- 前缀 + 26 位 uuid hex（与真实网关 gen_client_order_id 同风格）
        """
        return f"t-{uuid.uuid4().hex[:26]}"
