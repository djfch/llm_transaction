"""Gate.io 永续合约 REST 网关真实实现，封装 gate_api.FuturesApi。

分层（业务与基础设施解耦）：
- 纯业务函数：gen_client_order_id / build_order_payload / wrap_gate_exception（不触网，可单测）
- 映射函数：_to_*（SDK 对象 -> 共享 pydantic 模型）
- GateRestGateway：只做 SDK 调用、X-Gate-Exptime 注入、超时回查、异常包装
"""

from __future__ import annotations

import re
import time
import uuid
from decimal import Decimal

import gate_api
from gate_api.exceptions import GateApiException

from ..config import GateConfig
from .base import (
    Account,
    Candle,
    Contract,
    ContractNotFound,
    GatewayError,
    OrderNotFound,
    OrderRequest,
    OrderResult,
    OrderStateUnknown,
    Position,
    PositionNotFound,
    TpslOrder,
    Ticker,
)

_EXPTIME_AHEAD_MS = 30_000  # X-Gate-Exptime：当前毫秒 + 30 秒（计划附录）
_ORDER_TIMEOUT_S = 10  # 下单请求超时；超时后必须回查防重单
_TPSL_TIMEOUT_S = 10  # 保护单请求超时；状态未知时绝不继续撤旧单
_TEXT_MAX_BYTES = 28  # Gate 自定义订单 ID 总长上限（字节）
_TEXT_RE = re.compile(r"[0-9A-Za-z_-]+")  # Gate 自定义订单 ID 合法字符集

# Gate 私有错误 label -> 自定义异常（计划附录：异常按 label 分类）
_LABEL_EXCEPTIONS: dict[str, type[GatewayError]] = {
    "ORDER_NOT_FOUND": OrderNotFound,
    "POSITION_NOT_FOUND": PositionNotFound,
    "CONTRACT_NOT_FOUND": ContractNotFound,
}


def gen_client_order_id() -> str:
    """生成 text 自定义订单 ID：t- 前缀 + 26 位，总长 28 字节（计划附录）。"""
    return f"t-{uuid.uuid4().hex[:26]}"


def _fmt_decimal(value: Decimal) -> str:
    """Decimal -> 普通十进制字符串（避免科学计数法）。"""
    return format(value, "f")


def _validate_text(text: str) -> None:
    """校验调用方自带 text（Gate 自定义订单 ID 规则）：t- 前缀、≤28 字节、字符集 0-9A-Za-z_-。"""
    if not text.startswith("t-"):
        raise ValueError(f"text 必须以 't-' 开头（Gate 自定义订单 ID 规则）: {text!r}")
    if len(text.encode()) > _TEXT_MAX_BYTES:
        raise ValueError(f"text 总长不能超过 {_TEXT_MAX_BYTES} 字节: {text!r}")
    if not _TEXT_RE.fullmatch(text):
        raise ValueError(f"text 只能包含字符 0-9A-Za-z_-: {text!r}")


def build_order_payload(req: OrderRequest) -> dict:
    """把业务下单意图组装成 Gate 下单参数（纯函数，不触网）。

    已核实语义：市价单 price="0"+tif="ioc"；平仓 size="0"+close=true；
    text 为 None 时兜底生成，非 None 时按 Gate 规则校验（不合规抛 ValueError）。
    """
    if req.text is None:
        text = gen_client_order_id()
    else:
        _validate_text(req.text)
        text = req.text
    if req.close:
        return {
            "contract": req.contract,
            "size": "0",
            "price": "0",
            "tif": "ioc",
            "close": True,
            "text": text,
        }
    if req.price is None:  # 市价单
        price, tif = "0", "ioc"
    else:
        price = _fmt_decimal(req.price)
        tif = req.tif or "gtc"
    return {
        "contract": req.contract,
        "size": _fmt_decimal(req.size),
        "price": price,
        "tif": tif,
        "reduce_only": req.reduce_only,
        "tpsl_sl_trigger_price": (
            _fmt_decimal(req.stop_loss_price) if req.stop_loss_price is not None else None
        ),
        "tpsl_tp_trigger_price": (
            _fmt_decimal(req.take_profit_price) if req.take_profit_price is not None else None
        ),
        "text": text,
    }


def wrap_gate_exception(exc: GateApiException) -> GatewayError:
    """按 GateApiException.label 分类包装成自定义异常。"""
    label = getattr(exc, "label", "") or ""
    message = getattr(exc, "message", "") or str(exc)
    status = getattr(exc, "status", None)
    exc_type = _LABEL_EXCEPTIONS.get(label, GatewayError)
    return exc_type(f"[{label or 'UNKNOWN'}] {message}", label=label, status=status)


def _dec(value: str | None) -> Decimal:
    """SDK 字符串字段 -> Decimal；空串/None 归一为 0。"""
    if value in (None, ""):
        return Decimal(0)
    return Decimal(str(value))


def _to_contract(c: gate_api.Contract) -> Contract:
    return Contract(
        name=c.name,
        quanto_multiplier=_dec(c.quanto_multiplier),
        order_size_min=_dec(c.order_size_min),
        order_size_max=_dec(c.order_size_max),
        order_price_round=_dec(c.order_price_round),
        enable_decimal=bool(c.enable_decimal),
        mark_price=_dec(c.mark_price),
        funding_rate=_dec(c.funding_rate),
        funding_interval=int(c.funding_interval or 0),
        maker_fee_rate=_dec(c.maker_fee_rate),
        taker_fee_rate=_dec(c.taker_fee_rate),
        status=c.status or "",
        in_delisting=bool(c.in_delisting),
    )


def _to_position(p: gate_api.Position) -> Position:
    return Position(
        contract=p.contract,
        size=_dec(p.size),
        entry_price=_dec(p.entry_price),
        mark_price=_dec(p.mark_price),
        liq_price=_dec(p.liq_price),
        leverage=_dec(p.leverage),
        margin=_dec(p.margin),
        unrealised_pnl=_dec(p.unrealised_pnl),
    )


def _to_order(o: gate_api.FuturesOrder) -> OrderResult:
    return OrderResult(
        id=str(o.id),
        contract=o.contract or "",
        status=o.status or "",
        left=_dec(o.left),
        fill_price=_dec(o.fill_price),
        finish_as=o.finish_as or "",
        text=o.text or "",
    )


def _to_candle(k: gate_api.FuturesCandlestick) -> Candle:
    return Candle(t=int(k.t), o=_dec(k.o), h=_dec(k.h), l=_dec(k.l), c=_dec(k.c), v=_dec(k.v))


def _to_ticker(t: gate_api.FuturesTicker) -> Ticker:
    return Ticker(
        contract=t.contract,
        last=_dec(t.last),
        mark_price=_dec(t.mark_price),
        funding_rate=_dec(t.funding_rate),
        high_24h=_dec(t.high_24h),
        low_24h=_dec(t.low_24h),
        change_percentage=_dec(t.change_percentage),
    )


def _to_tpsl(order: gate_api.FuturesPriceTriggeredOrder) -> TpslOrder | None:
    """Gate 价格触发单转换为本系统整仓保护单；非整仓平仓单不参与接管。"""
    direction = {"close-long-position": 1, "close-short-position": -1}.get(order.order_type)
    if direction is None or order.trigger is None:
        return None
    rule = int(order.trigger.rule)
    kind = "take_profit" if (direction > 0) == (rule == 1) else "stop_loss"
    return TpslOrder(
        id=str(order.id_string or order.id),
        contract=order.initial.contract,
        direction=direction,
        kind=kind,
        trigger_price=_dec(order.trigger.price),
    )


class GateRestGateway:
    """真实网关：只做 SDK 调用与异常/超时处理，下单语义由 build_order_payload 组装。"""

    def __init__(
        self, gate_config: GateConfig, api_key: str = "", api_secret: str = "", testnet: bool = True
    ) -> None:
        host = gate_config.testnet_host if testnet else gate_config.live_host
        config = gate_api.Configuration(host=host, key=api_key, secret=api_secret)
        self._api = gate_api.FuturesApi(gate_api.ApiClient(config))
        self._settle = gate_config.settle

    @staticmethod
    def _exptime() -> int:
        """X-Gate-Exptime 头（毫秒）：当前时间 + 30 秒，防延迟重放。"""
        return int(time.time() * 1000) + _EXPTIME_AHEAD_MS

    def get_contract(self, contract: str) -> Contract:
        try:
            return _to_contract(self._api.get_futures_contract(self._settle, contract))
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def get_account(self) -> Account:
        try:
            acc = self._api.list_futures_accounts(self._settle)
            return Account(available=_dec(acc.available), unrealised_pnl=_dec(acc.unrealised_pnl))
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def list_positions(self) -> list[Position]:
        try:
            positions = [_to_position(p) for p in self._api.list_positions(self._settle)]
            for pos in positions:
                tpsl = self.list_tpsl_orders(pos.contract)
                mine = [o for o in tpsl if o.direction == (1 if pos.size > 0 else -1)]
                stop = next((o.trigger_price for o in mine if o.kind == "stop_loss"), None)
                take = next((o.trigger_price for o in mine if o.kind == "take_profit"), None)
                pos.stop_loss_price = stop
                pos.take_profit_price = take
            return positions
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def place_order(self, req: OrderRequest) -> OrderResult:
        payload = build_order_payload(req)
        try:
            order = self._api.create_futures_order(  # 成功返回 201
                self._settle,
                gate_api.FuturesOrder(**payload),
                x_gate_exptime=self._exptime(),
                _request_timeout=_ORDER_TIMEOUT_S,
            )
            return _to_order(order)
        except GateApiException as exc:  # 服务端明确拒绝：不会重单，直接包装抛出
            raise wrap_gate_exception(exc) from exc
        except Exception as exc:  # 超时/网络异常：订单状态未知，必须回查防重单
            return self._recheck_after_timeout(payload["text"], exc)

    def _recheck_after_timeout(self, text: str, original: Exception) -> OrderResult:
        """下单超时后按 text 回查：已创建则返回结果；确认未创建可安全重试。"""
        try:
            order = self._api.get_futures_order(self._settle, text)  # order_id 支持 text
            return _to_order(order)
        except GateApiException as exc:
            wrapped = wrap_gate_exception(exc)
            if isinstance(wrapped, OrderNotFound):
                raise GatewayError(
                    "下单请求超时，回查确认订单未创建，可安全重试",
                    label="ORDER_TIMEOUT_NOT_CREATED",
                ) from original
            raise OrderStateUnknown(
                f"下单超时且回查失败（{wrapped}），订单状态未知，禁止盲目重试",
                label="ORDER_STATE_UNKNOWN",
            ) from original
        except Exception as exc:  # 网络层异常（非 GateApiException）：状态未知，禁止盲目重试
            raise OrderStateUnknown(
                f"下单超时且回查请求失败（{exc}），订单状态未知，禁止盲目重试",
                label="ORDER_STATE_UNKNOWN",
            ) from original

    def amend_order(
        self,
        contract: str,
        order_id: str,
        price: Decimal | None = None,
        size: Decimal | None = None,
    ) -> OrderResult:
        amendment = gate_api.FuturesOrderAmendment(
            price=_fmt_decimal(price) if price is not None else None,
            size=_fmt_decimal(size) if size is not None else None,
        )
        try:
            order = self._api.amend_futures_order(
                self._settle, order_id, amendment, x_gate_exptime=self._exptime()
            )
            return _to_order(order)
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def cancel_order(self, contract: str, order_id: str) -> OrderResult:
        try:
            order = self._api.cancel_futures_order(
                self._settle, order_id, x_gate_exptime=self._exptime()
            )
            return _to_order(order)
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def list_orders(self, contract: str, status: str = "open") -> list[OrderResult]:
        try:
            orders = self._api.list_futures_orders(self._settle, status, contract=contract)
            return [_to_order(o) for o in orders]
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def list_tpsl_orders(self, contract: str) -> list[TpslOrder]:
        try:
            orders = self._api.list_price_triggered_orders(self._settle, "open", contract=contract)
            return [mapped for raw in orders if (mapped := _to_tpsl(raw)) is not None]
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder:
        rule = 1 if (order.direction > 0) == (order.kind == "take_profit") else 2
        payload = gate_api.FuturesPriceTriggeredOrder(
            initial=gate_api.FuturesInitialOrder(
                contract=order.contract, size=0, price="0", close=True, tif="ioc"
            ),
            trigger=gate_api.FuturesPriceTrigger(
                strategy_type=0, price_type=1, price=_fmt_decimal(order.trigger_price), rule=rule
            ),
            order_type="close-long-position" if order.direction > 0 else "close-short-position",
        )
        try:
            result = self._api.create_price_triggered_order(
                self._settle, payload, _request_timeout=_TPSL_TIMEOUT_S
            )
            return order.model_copy(update={"id": str(result.id_string or result.id)})
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc
        except Exception as exc:
            raise OrderStateUnknown(
                "创建止盈止损超时或网络失败，状态未知；旧保护单未撤销，禁止盲目重试",
                label="TPSL_STATE_UNKNOWN",
            ) from exc

    def cancel_tpsl_order(self, order_id: str) -> None:
        try:
            self._api.cancel_price_triggered_order(
                self._settle, order_id, _request_timeout=_TPSL_TIMEOUT_S
            )
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc
        except Exception as exc:
            raise OrderStateUnknown(
                f"撤销止盈止损单 {order_id} 超时或网络失败，状态未知；请人工核对",
                label="TPSL_STATE_UNKNOWN",
            ) from exc

    def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """K 线查询。计划附录：limit 与 from/to 互斥，单次 ≤2000 点。"""
        if limit is not None and (from_ts is not None or to_ts is not None):
            raise ValueError("limit 与 from/to 互斥，不能同时传")
        if limit is not None and not 1 <= limit <= 2000:
            raise ValueError("limit 必须在 1~2000 之间")
        kwargs: dict = {"interval": interval}
        if limit is not None:
            kwargs["limit"] = limit
        if from_ts is not None:
            kwargs["from_ts"] = from_ts
        if to_ts is not None:
            kwargs["to_ts"] = to_ts
        try:
            candles = self._api.list_futures_candlesticks(self._settle, contract, **kwargs)
            return [_to_candle(k) for k in candles]
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def get_tickers(self) -> list[Ticker]:
        try:
            return [_to_ticker(t) for t in self._api.list_futures_tickers(self._settle)]
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def set_leverage(self, contract: str, leverage: int, margin_mode: str = "isolated") -> Position:
        """新接口调杠杆：margin_mode（isolated/cross）必填；禁用旧接口（计划附录）。"""
        if margin_mode not in ("isolated", "cross"):
            raise ValueError(f"非法 margin_mode: {margin_mode}（可选 isolated/cross）")
        try:
            # 注意：该 SDK 方法不支持 x_gate_exptime 关键字（testnet 实测），不能传
            position = self._api.update_contract_position_leverage(
                self._settle, contract, str(leverage), margin_mode
            )
            return _to_position(position)
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc
