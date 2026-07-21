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
    Position,
    Ticker,
    TpslOrder,
)
from .account import FillRecord, PaperAccount
from .convert import PriceSnap, RestingOrder, synth_ticker, to_position
from .funding import settle_funding as _settle_funding
from .liquidation import LiquidationEvent, liquidate, should_liquidate


class PaperGateway:
    """模拟撮合网关。paper 模式下替代真实网关，供 agent / 风控无差别调用。"""

    def __init__(
        self,
        config: PaperConfig,
        contracts: dict[str, Contract] | None = None,
        maintenance_rate: Decimal = Decimal("0.005"),
        candle_provider: Callable[..., list[Candle]] | None = None,
        ticker_provider: Callable[[], list[Ticker]] | None = None,
    ) -> None:
        self._cfg = config
        self._contracts = dict(contracts or {})
        self._default_maint = maintenance_rate  # 简化：单档维持保证金率
        self._maintenance: dict[str, Decimal] = {}  # 可按合约覆盖（risk_limit_tiers 注入点）
        self._candle_provider = candle_provider
        self._ticker_provider = ticker_provider
        self.account = PaperAccount(Decimal(str(config.initial_equity)))
        self._snaps: dict[str, PriceSnap] = {}
        self._leverages: dict[str, Decimal] = {}
        self._open: dict[str, RestingOrder] = {}
        self._results: dict[str, OrderResult] = {}
        self._tpsl: dict[str, TpslOrder] = {}
        self.liquidations: list[LiquidationEvent] = []

    def upsert_contract(self, contract: Contract) -> None:
        self._contracts[contract.name] = contract

    def set_maintenance_rate(self, contract: str, rate: Decimal) -> None:
        self._maintenance[contract] = Decimal(str(rate))

    def on_price(
        self,
        contract: str,
        mark_price: Decimal,
        best_bid: Decimal | None = None,
        best_ask: Decimal | None = None,
    ) -> None:
        """行情注入入口：由外部行情源推送，驱动挂单撮合与强平检查。"""
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
        """资金费结算入口：由外部按 funding_interval 定时触发，返回余额变化。"""
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
        marks = {name: s.mark for name, s in self._snaps.items()}
        quantos = {name: c.quanto_multiplier for name, c in self._contracts.items()}
        return self.account.equity(marks, quantos)

    def drain_fills(self) -> list[FillRecord]:
        """取走自上次调用以来的全部成交记录（含强平记录）并清空缓冲。

        供决策循环每轮结束后落库 trades 表；返回 list[FillRecord]，按成交时间升序。
        """
        fills = self.account.fills
        self.account.fills = []
        return fills

    def reset_account(self, equity: Decimal) -> None:
        """重置模拟账户：按新权益新建账本（重置即清空模拟仓位与挂单）。

        清空：全部持仓、未成交挂单（_open 及其在 _results 中的 open 记录）、
        成交缓冲 fills、强平事件记录；已完成订单历史、杠杆设置与行情快照保留。
        """
        self.account = PaperAccount(equity)
        self._open.clear()
        self._results = {k: r for k, r in self._results.items() if r.status != "open"}
        self._tpsl.clear()
        self.liquidations.clear()

    def get_contract(self, contract: str) -> Contract:
        if contract not in self._contracts:
            raise ContractNotFound(f"合约不存在: {contract}", label="CONTRACT_NOT_FOUND")
        return self._contracts[contract]

    def get_account(self) -> Account:
        upnl = sum((p.unrealised_pnl for p in self.list_positions()), Decimal(0))
        return Account(available=self.account.available, unrealised_pnl=upnl)

    def list_positions(self) -> list[Position]:
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
        if margin_mode not in ("isolated", "cross"):
            raise ValueError(f"非法 margin_mode: {margin_mode}（可选 isolated/cross）")
        if leverage < 1:
            raise ValueError("leverage 必须 ≥ 1")
        c = self.get_contract(contract)
        self._leverages[contract] = Decimal(leverage)
        pos = self.account.ensure_position(contract, Decimal(leverage))
        pos.leverage = Decimal(leverage)
        if pos.size != 0:  # 调杠杆重算占用保证金，差额从可用余额划转
            new_margin = (
                PaperAccount.notional(pos.size, pos.entry_price, c.quanto_multiplier) / leverage
            )
            delta = new_margin - pos.margin
            if delta > 0 and self.account.available < delta:
                raise GatewayError("可用余额不足，无法调低杠杆", label="INSUFFICIENT_BALANCE")
            self.account.available -= delta
            pos.margin = new_margin
        return self._position_of(pos)

    def place_order(self, req: OrderRequest) -> OrderResult:
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
            update={"left": abs(order.size)}
        )
        return self._results[order_id]

    def cancel_order(self, contract: str, order_id: str) -> OrderResult:
        self._open_order(contract, order_id)
        del self._open[order_id]
        cancelled = self._results[order_id].model_copy(
            update={"status": "finished", "finish_as": "cancelled"}
        )
        self._results[order_id] = cancelled
        return cancelled

    def list_orders(self, contract: str, status: str = "open") -> list[OrderResult]:
        return [r for r in self._results.values() if r.contract == contract and r.status == status]

    def list_tpsl_orders(self, contract: str) -> list[TpslOrder]:
        return [order for order in self._tpsl.values() if order.contract == contract]

    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder:
        created = order.model_copy(update={"id": f"tpsl-{self._next_id()}"})
        self._tpsl[created.id] = created
        return created

    def cancel_tpsl_order(self, order_id: str) -> None:
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
        if limit is not None and (from_ts is not None or to_ts is not None):
            raise ValueError("limit 与 from/to 互斥，不能同时传")
        if self._candle_provider is None:
            return []
        return self._candle_provider(contract, interval, limit, from_ts, to_ts)

    def get_tickers(self) -> list[Ticker]:
        if self._ticker_provider is not None:
            return self._ticker_provider()
        return [
            synth_ticker(name, snap, self._contracts.get(name))
            for name, snap in self._snaps.items()
        ]

    def _market_order(self, req: OrderRequest, order_id: str, text: str) -> OrderResult:
        snap = self._snap(req.contract)
        slip = Decimal(str(self._cfg.slippage))
        price = snap.mark * (1 + slip) if req.size > 0 else snap.mark * (1 - slip)
        result = self._execute(order_id, req.contract, req.size, price, maker=False, text=text)
        self._apply_request_tpsl(req)
        return result

    def _limit_order(self, req: OrderRequest, order_id: str, text: str) -> OrderResult:
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
        result = OrderResult(
            id=order_id,
            contract=req.contract,
            status="open",
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

        触价时余额不足的挂单直接撤销（不向外抛异常），避免 on_price 每 tick
        重复报错并跳过后续强平检查。
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
        """价格穿越时按市价全平，并清理该方向全部保护单。"""
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
        self._apply_tpsl(req.contract, req.size, req.stop_loss_price, req.take_profit_price)

    def _apply_tpsl(
        self,
        contract: str,
        size: Decimal,
        stop_loss_price: Decimal | None,
        take_profit_price: Decimal | None,
    ) -> None:
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
        for order in list(self._tpsl.values()):
            if order.contract == contract and (direction is None or order.direction == direction):
                del self._tpsl[order.id]

    def _clear_stale_tpsl(self, contract: str) -> None:
        pos = self.account.position(contract)
        direction = None if pos is None else (1 if pos.size > 0 else -1)
        for order in list(self._tpsl.values()):
            if order.contract == contract and (direction is None or order.direction != direction):
                del self._tpsl[order.id]

    def _cancel_failed(self, order: RestingOrder, reason: str) -> None:
        """撤销触价失败的挂单：从 open 移除，结果标记 cancelled 并记录原因。"""
        self._open.pop(order.id, None)
        self._results[order.id] = self._results[order.id].model_copy(
            update={"status": "finished", "finish_as": f"cancelled:{reason}"}
        )

    def _execute(
        self, order_id: str, contract: str, size: Decimal, price: Decimal, maker: bool, text: str
    ) -> OrderResult:
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
        pos = self.account.position(contract)
        if pos is None:
            return
        c = self.get_contract(contract)
        mark = self._snaps[contract].mark
        if should_liquidate(pos, mark, c.quanto_multiplier, self._maint(contract)):
            self.liquidations.append(liquidate(self.account, contract, mark, c.quanto_multiplier))

    def _crossed(self, order: RestingOrder) -> bool:
        snap = self._snaps.get(order.contract)
        if snap is None:
            return False
        if order.size > 0:
            return order.price >= snap.ask
        return order.price <= snap.bid

    def _mark(self, contract: str, entry_price: Decimal) -> Decimal:
        snap = self._snaps.get(contract)
        return snap.mark if snap is not None else entry_price

    def _position_of(self, pos) -> Position:
        c = self.get_contract(pos.contract)
        mark = self._mark(pos.contract, pos.entry_price)
        return to_position(pos, c, mark, self._maint(pos.contract), self.account)

    def _snap(self, contract: str) -> PriceSnap:
        snap = self._snaps.get(contract)
        if snap is None:
            raise GatewayError(
                f"尚无行情: {contract}（需先 on_price 注入）", label="NO_MARKET_DATA"
            )
        return snap

    def _leverage(self, contract: str) -> Decimal:
        pos = self.account.position(contract)
        if pos is not None:
            return pos.leverage
        return self._leverages.get(contract, Decimal(1))

    def _maint(self, contract: str) -> Decimal:
        return self._maintenance.get(contract, self._default_maint)

    def _is_reducing(self, contract: str, size: Decimal) -> bool:
        pos = self.account.position(contract)
        return pos is not None and (pos.size > 0) != (size > 0)

    def _open_order(self, contract: str, order_id: str) -> RestingOrder:
        order = self._open.get(order_id)
        if order is None or order.contract != contract:
            raise OrderNotFound(f"订单不存在或非 open: {order_id}", label="ORDER_NOT_FOUND")
        return order

    def _next_id(self) -> str:
        """订单 ID 全局唯一：t- 前缀 + 26 位 uuid hex（与真实网关 gen_client_order_id 同风格）。"""
        return f"t-{uuid.uuid4().hex[:26]}"
