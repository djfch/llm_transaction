"""交易所网关抽象接口（Protocol）与共享数据模型。

Gate 永续业务接口约定（调用 SDK 时必须按其当前参数名完成适配）：
- size 为张数（非币数），正=开多/买入，负=开空/卖出；币数换算用 Contract.quanto_multiplier
- 市价单 = price="0" + tif="ioc"；单仓模式平仓 = size="0" + close=true
- enable_decimal=false 的合约 size 必须整数
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel


class GatewayError(Exception):
    """网关统一异常基类。label 为 Gate 私有错误标签（如有），status 为 HTTP 状态码。"""

    def __init__(self, message: str, label: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.label = label
        self.status = status


class OrderNotFound(GatewayError):
    """订单不存在（Gate label: ORDER_NOT_FOUND）。"""


class PositionNotFound(GatewayError):
    """持仓不存在（Gate label: POSITION_NOT_FOUND）。"""


class ContractNotFound(GatewayError):
    """合约不存在（Gate label: CONTRACT_NOT_FOUND）。"""


class OrderStateUnknown(GatewayError):
    """下单请求超时且回查失败：订单可能已创建，禁止盲目重试（防重单）。"""


class Contract(BaseModel):
    """合约元数据与实时标记信息。"""

    name: str
    quanto_multiplier: Decimal  # 每张合约对应的币数
    order_size_min: Decimal
    order_size_max: Decimal
    order_price_round: Decimal  # 下单价格步长
    enable_decimal: bool  # false 时 size 必须整数
    mark_price: Decimal
    funding_rate: Decimal  # 资金费率（比率，非百分比）
    funding_interval: int  # 资金费结算间隔（秒）
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    status: str  # 只交易 trading
    in_delisting: bool


class Account(BaseModel):
    """合约账户（position_margin 已废弃，不建模）。"""

    available: Decimal  # 可用余额
    unrealised_pnl: Decimal  # 未实现盈亏


class Position(BaseModel):
    """持仓。size 正=多，负=空；leverage=0 表示全仓。"""

    contract: str
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal
    liq_price: Decimal  # 仅估值，不作强平依据
    leverage: Decimal
    margin: Decimal
    unrealised_pnl: Decimal
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None


class OrderRequest(BaseModel):
    """下单意图（业务层语义，与交易所 SDK 解耦）。

    price 为 None 表示市价单；close=True 表示平掉该合约全部持仓。
    """

    contract: str
    size: Decimal = Decimal(0)  # 正多负空；close=True 时忽略
    price: Decimal | None = None
    tif: str | None = None  # gtc/ioc/poc/fok；市价单强制 ioc
    reduce_only: bool = False
    close: bool = False
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    text: str | None = None  # 自定义订单 ID；None 时由网关生成


class OrderResult(BaseModel):
    """订单结果。status: open/finished；finish_as: filled/cancelled 等。"""

    id: str
    contract: str
    status: str
    # 挂单快照展示字段：size 保留方向，price/tif/reduce_only 保留委托约束。
    size: Decimal = Decimal(0)
    price: Decimal | None = None
    tif: str = ""
    reduce_only: bool = False
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    left: Decimal  # 剩余未成交张数
    fill_price: Decimal  # 成交均价
    finish_as: str = ""
    text: str = ""


class TpslOrder(BaseModel):
    """整仓止盈止损保护单。direction 正=保护多仓，负=保护空仓。"""

    id: str
    contract: str
    direction: int
    kind: str  # stop_loss / take_profit
    trigger_price: Decimal


class ExchangeTrade(BaseModel):
    """交易所真实成交（my_trades REST / usertrades WS 推送归一化后的内部结构）。

    id/order_id 为 Gate 侧 id 的字符串形式；size 正多负空；text 为 Gate 侧标记
    （客户端订单 id 或系统来源文本）。仅后端对账用，不进前端响应。
    """

    id: str  # 交易所成交 id（去重键）
    order_id: str  # 交易所订单 id（归属/分类键）
    contract: str
    size: Decimal
    price: Decimal
    fee: Decimal
    role: str = ""  # taker/maker
    text: str = ""
    create_time: float


class PositionCloseRecord(BaseModel):
    """平仓盈亏历史条目（position_close）：平仓成交的 pnl 回填来源。"""

    time: float
    contract: str
    pnl: Decimal
    accum_size: Decimal
    text: str = ""


class Candle(BaseModel):
    """K 线。t 为秒级时间戳；v 为成交量（张数）。"""

    t: int
    o: Decimal
    h: Decimal
    # 字段名沿用 Gate K 线原始命名 o/h/l/c/v
    l: Decimal  # noqa: E741
    c: Decimal
    v: Decimal


class Ticker(BaseModel):
    """合约 ticker 摘要。"""

    contract: str
    last: Decimal
    mark_price: Decimal
    funding_rate: Decimal
    high_24h: Decimal
    low_24h: Decimal
    change_percentage: Decimal


class Gateway(Protocol):
    """交易所网关统一接口：真实实现、mock、paper 撮合引擎都实现它。"""

    def get_contract(self, contract: str) -> Contract: ...

    def get_account(self) -> Account: ...

    def list_positions(self) -> list[Position]: ...

    def place_order(self, req: OrderRequest) -> OrderResult: ...

    def amend_order(
        self,
        contract: str,
        order_id: str,
        price: Decimal | None = None,
        size: Decimal | None = None,
    ) -> OrderResult: ...

    def cancel_order(self, contract: str, order_id: str) -> OrderResult: ...

    # 分页读取订单快照；contract 为空时返回全部合约。
    def list_orders(
        self,
        contract: str | None = None,
        status: str = "open",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[OrderResult]: ...

    def list_tpsl_orders(self, contract: str) -> list[TpslOrder]: ...

    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder: ...

    def cancel_tpsl_order(self, order_id: str) -> None: ...

    def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]: ...

    def get_tickers(self) -> list[Ticker]: ...

    def set_leverage(
        self, contract: str, leverage: int, margin_mode: str = "isolated"
    ) -> Position: ...
