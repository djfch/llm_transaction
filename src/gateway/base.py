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
from .market_stats import OpenInterestPoint

# LLM 参数数值安全上限（tool_handlers._to_decimal 与 gate_rest._fmt_decimal 同口径）：
# 防极端指数（如 1e-1000000000）在交易所参数格式化时展开为等长字符串耗尽内存
# （issue #80）；有效数字 18 位远超任何合约的价格/张数精度，指数 ±30 覆盖从
# 1e-8 级最小跳动到 1e30 级名义价值的全部合法范围
MAX_DECIMAL_DIGITS = 18
MAX_DECIMAL_EXPONENT = 30


class GatewayError(Exception):
    """网关统一异常基类。label 为 Gate 私有错误标签（如有），status 为 HTTP 状态码。"""

    def __init__(self, message: str, label: str = "", status: int | None = None) -> None:
        """初始化网关异常，记录错误消息与交易所侧的错误标签、HTTP 状态码。

        参数：
            message: str，错误描述消息
            label: str，Gate 私有错误标签（如 ORDER_NOT_FOUND），无则省略
            status: int | None，HTTP 状态码，无则省略

        返回：
            None，初始化异常实例（就地写入 label/status 属性）
        """
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


class GatewayTransportError(GatewayError):
    """传输层异常（连接/读取超时、重试耗尽等）：请求未获交易所确认。

    与 GateApiException 的服务端明确拒绝区分：读操作结果未知、写操作状态未知，
    统一 label=TRANSPORT_UNKNOWN，调用方按"不可当作明确失败"处理。
    """


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
    market_order_size_max: Decimal = Decimal(0)  # 0 表示市价单沿用普通订单上限


class Account(BaseModel):
    """合约账户（position_margin 已废弃，不建模）。"""

    available: Decimal  # 可用余额
    unrealised_pnl: Decimal  # 未实现盈亏


class Position(BaseModel):
    """持仓。size 正=多，负=空；leverage=0 表示全仓。

    margin_mode 为保证金模式（isolated/cross）；cross_leverage_limit 为全仓实际
    杠杆（逐仓时为 None）。全仓持仓的有效杠杆以 cross_leverage_limit 为准，
    leverage 字段保持交易所原始语义（0=全仓）。
    """

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
    margin_mode: str = "isolated"  # isolated/cross
    cross_leverage_limit: Decimal | None = None  # 全仓实际杠杆（逐仓为 None）


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

    def get_contract(self, contract: str) -> Contract:
        """读取单个合约的元数据与实时标记信息（标记价、资金费率、手续费率等）。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            Contract：合约元数据与实时标记信息

        异常：
            ContractNotFound：合约不存在时抛出
            GatewayError：交易所请求失败时抛出
        """
        ...

    def get_cached_contract(self, contract: str) -> Contract:
        """读取最近一次成功获取的内存合约规格，不访问外部接口。

        参数：
            contract: str，合约名

        返回：
            Contract：进程内最近一次成功获取的合约规格

        异常：
            ContractNotFound：内存中尚无该合约规格时抛出
        """
        ...

    def get_account(self) -> Account:
        """读取合约账户的可用余额与未实现盈亏。

        参数：无

        返回：
            Account：合约账户快照（可用余额、未实现盈亏）

        异常：
            GatewayError：交易所请求失败时抛出
        """
        ...

    def list_positions(self) -> list[Position]:
        """读取当前全部持仓，并带上各持仓的止盈止损触发价（如有）。

        参数：无

        返回：
            list[Position]：持仓列表；无持仓时返回空列表

        异常：
            GatewayError：交易所请求失败时抛出
        """
        ...

    def place_order(self, req: OrderRequest) -> OrderResult:
        """按下单意图向交易所提交订单（开仓/平仓、限价/市价）。

        参数：
            req: OrderRequest，下单意图；price 为 None 表示市价单，close=True 表示整仓平仓

        返回：
            OrderResult：交易所确认后的订单结果

        异常：
            GatewayError：交易所明确拒绝或请求失败时抛出
            OrderStateUnknown：下单超时且回查失败、订单状态未知时抛出（禁止盲目重试）
        """
        ...

    def amend_order(
        self,
        contract: str,
        order_id: str,
        price: Decimal | None = None,
        size: Decimal | None = None,
    ) -> OrderResult:
        """修改未成交挂单的价格和/或张数，未传的字段保持原值。

        参数：
            contract: str，合约名（如 BTC_USDT）
            order_id: str，交易所订单 id
            price: Decimal | None，新的委托价格；省略时不修改价格
            size: Decimal | None，新的委托张数（正多负空）；省略时不修改张数

        返回：
            OrderResult：修改后的订单快照

        异常：
            OrderNotFound：订单不存在时抛出
            GatewayError：交易所请求失败时抛出
        """
        ...

    def cancel_order(self, contract: str, order_id: str) -> OrderResult:
        """撤销指定未成交挂单。

        参数：
            contract: str，合约名（如 BTC_USDT）
            order_id: str，交易所订单 id

        返回：
            OrderResult：撤单后的订单快照

        异常：
            OrderNotFound：订单不存在时抛出
            GatewayError：交易所请求失败时抛出
        """
        ...

    def list_orders(
        self,
        contract: str | None = None,
        status: str = "open",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[OrderResult]:
        """分页读取订单快照；contract 为空时返回全部合约的订单。

        参数：
            contract: str | None，合约名（如 BTC_USDT）；省略时查询全部合约
            status: str，订单状态过滤（open 未成交 / finished 已完结），省略时默认 open
            limit: int | None，单页最大条数；省略时由交易所决定
            offset: int，分页偏移量，省略时默认 0

        返回：
            list[OrderResult]：订单快照列表；无匹配订单时返回空列表

        异常：
            GatewayError：交易所请求失败时抛出
        """
        ...

    def list_tpsl_orders(self, contract: str) -> list[TpslOrder]:
        """读取指定合约当前生效的整仓止盈止损保护单。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            list[TpslOrder]：止盈止损保护单列表；无保护单时返回空列表

        异常：
            GatewayError：交易所请求失败时抛出
        """
        ...

    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder:
        """创建整仓止盈止损保护单，触发后平掉对应方向的全部持仓。

        参数：
            order: TpslOrder，保护单参数（方向、类型、触发价）；id 由交易所生成

        返回：
            TpslOrder：创建成功的保护单（id 已回填为交易所生成的 id）

        异常：
            GatewayError：交易所明确拒绝或请求失败时抛出
            OrderStateUnknown：请求超时或网络失败、保护单状态未知时抛出（禁止盲目重试）
        """
        ...

    def cancel_tpsl_order(self, order_id: str) -> None:
        """撤销指定止盈止损保护单。

        参数：
            order_id: str，交易所保护单 id

        返回：
            None，撤销请求已被交易所受理

        异常：
            GatewayError：交易所明确拒绝或请求失败时抛出
            OrderStateUnknown：请求超时或网络失败、保护单状态未知时抛出（需人工核对）
        """
        ...

    def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """读取合约 K 线；limit 与 from/to 两种查询方式互斥。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，K 线周期（如 1m、5m、1h），省略时默认 1m
            limit: int | None，最近 N 根（1~2000）；与 from_ts/to_ts 不能同时传
            from_ts: int | None，起始秒级时间戳；省略表示不限制起点
            to_ts: int | None，结束秒级时间戳；省略表示不限制终点

        返回：
            list[Candle]：K 线列表；无数据时返回空列表

        异常：
            ValueError：limit 与 from/to 同时传入，或 limit 超出 1~2000 时抛出
            GatewayError：交易所请求失败时抛出
        """
        ...

    def get_tickers(self) -> list[Ticker]:
        """读取全部合约的 ticker 摘要（最新价、标记价、资金费率、24h 高低等）。

        参数：无

        返回：
            list[Ticker]：全部合约的 ticker 摘要列表

        异常：
            GatewayError：交易所请求失败时抛出
        """
        ...

    def fetch_open_interest(self, contract: str) -> Decimal | None:
        """读取合约最新持仓量（张数）。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            Decimal | None：持仓量张数；无该数据的实现（如 paper）返回 None

        异常：
            GatewayError：交易所查询失败时抛出
        """
        ...

    def fetch_open_interest_history(
        self, contract: str, interval: str, limit: int = 3
    ) -> list[OpenInterestPoint]:
        """按统计周期读取合约持仓量历史，按时间升序返回。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，统计周期（与 K 线周期同格式，如 4h、1d）
            limit: int，返回的最大数据点数，省略时默认 3

        返回：
            list[OpenInterestPoint]：持仓量历史点列表（按时间升序）；无数据时返回空列表

        异常：
            GatewayError：交易所查询失败时抛出
        """
        ...

    def set_leverage(self, contract: str, leverage: int, margin_mode: str = "isolated") -> Position:
        """设置合约的杠杆倍数与保证金模式（逐仓/全仓）。

        参数：
            contract: str，合约名（如 BTC_USDT）
            leverage: int，杠杆倍数
            margin_mode: str，保证金模式（isolated 逐仓 / cross 全仓），省略时默认 isolated

        返回：
            Position：设置后的持仓快照

        异常：
            ValueError：margin_mode 不是 isolated/cross 时抛出
            GatewayError：交易所请求失败时抛出
        """
        ...
