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
import urllib3
from gate_api.exceptions import ApiException, GateApiException
from urllib3.exceptions import HTTPError as _Urllib3HTTPError

from ..config import GateConfig
from .base import (
    Account,
    Candle,
    Contract,
    GatewayError,
    GatewayTransportError,
    OrderNotFound,
    ExchangeTrade,
    OrderRequest,
    OrderResult,
    OrderStateUnknown,
    Position,
    PositionCloseRecord,
    TpslOrder,
    Ticker,
)
from .errors import wrap_gate_exception, wrap_transport_exception
from .gate_market_stats import GateOpenInterestMixin

_EXPTIME_AHEAD_MS = 30_000  # X-Gate-Exptime：当前毫秒 + 30 秒
_ORDER_TIMEOUT_S = 10  # 下单请求超时；超时后必须回查防重单
_TPSL_TIMEOUT_S = 10  # 保护单请求超时；状态未知时绝不继续撤旧单
_FILLS_TIMEOUT_S = 10  # 成交对账读请求超时；悬挂比失败更糟（会卡死启动/泄漏回填任务）
_CONNECT_TIMEOUT_S = 5  # 默认连接超时：Gate 正常建连亚秒级，5s 已宽裕
_READ_TIMEOUT_S = 15  # 默认读取超时：正常 REST 响应秒级，15s 覆盖极端慢响应
_TOTAL_TIMEOUT_S = 30  # 整次请求 wall-clock 上限（含全部重试共享同一预算）
# 未显式指定超时的调用统一使用（连接, 读取）超时：SDK 缺省 None=不限时，
# 网关线程一旦被悬挂请求占住，所有网关 I/O 会整体停摆（issue #72 建议 2）
_DEFAULT_REQUEST_TIMEOUT = (_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S)
# SDK 对 tuple 只构造 Timeout(connect, read)、int 只构造 Timeout(total)；且
# urllib3 的 total 只覆盖单次尝试——每次重试都会 clone 新 Timeout 重新起表，
# Retry(total=2) 下整次调用最长可达 3×30s（PR #84 评审 P2）。因此禁用 urllib3
# 自动重试，由 _call_with_shared_deadline 以单调时钟统一控制：全部尝试共享
# 同一 wall-clock 预算，每次尝试的连接/读取超时按剩余预算收紧。
_RETRY_ATTEMPTS = 3  # 首试 + 2 次重试（与原 Retry(total=2) 等价）
_RETRY_BACKOFF_S = 0.2  # 退避基数：第 n 次重试前等待 0.2×2^n 秒（受剩余预算截断）
_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 仅明确只读方法可重试。HTTP 幂等 ≠ 交易安全：PUT 改单/DELETE 撤单在传输结果
# 未知时重试，会把首次尝试的未知结果"洗成"第二次尝试的明确业务错误（如订单
# 已成交后重试返回 ORDER_NOT_FOUND），绕过 OrderStateUnknown fail-closed 契约
# （PR #84 评审 P1）。所有交易写（POST/PUT/DELETE）传输异常后只执行一次，
# 原样上抛由 call_api 归一化为 GatewayTransportError，再由业务层转状态未知；
# 个别确可安全重试的写接口须由业务方法在对账后显式决定。
_RETRYABLE_TRANSPORT_ERRORS = (
    urllib3.exceptions.ConnectTimeoutError,
    urllib3.exceptions.ReadTimeoutError,
    urllib3.exceptions.NewConnectionError,
    urllib3.exceptions.ProtocolError,
)
_TEXT_MAX_BYTES = 28  # Gate 自定义订单 ID 总长上限（字节）
_TEXT_RE = re.compile(r"[0-9A-Za-z_-]+")  # Gate 自定义订单 ID 合法字符集


def _ensure_total_deadline(timeout: object) -> urllib3.Timeout:
    """给 SDK 转换后的 timeout 补整次 wall-clock 上限（total），无 total 时补默认值。

    SDK 只产三种形态：None（调用方未给超时）、Timeout(connect, read)（tuple 路径）、
    Timeout(total)（int 路径）。已带 total 的原样返回（显式给定的整次上限优先）。

    参数：
        timeout: object，SDK 传入 PoolManager.request 的 timeout 实参

    返回：
        urllib3.Timeout：保证携带 total 的超时对象；None 时构造
        （connect=5, read=15, total=30）默认组合
    """
    if timeout is None:
        return urllib3.Timeout(
            connect=_CONNECT_TIMEOUT_S, read=_READ_TIMEOUT_S, total=_TOTAL_TIMEOUT_S
        )
    if isinstance(timeout, urllib3.Timeout):
        if timeout.total is not None:
            return timeout
        timeout = timeout.clone()
        timeout.total = _TOTAL_TIMEOUT_S
        return timeout
    return urllib3.Timeout(total=float(timeout))  # 兼容 int/float 直传


def _call_with_shared_deadline(request_fn, method: str, url: str, request_kwargs: dict):
    """以单调时钟统一控制重试：全部尝试共享同一 wall-clock 预算后转发真实请求。

    urllib3 的 Timeout.total 只覆盖单次尝试（每次重试 clone 新 Timeout 重新起表），
    Retry(total=2) 下整次调用最长 3×预算；本层改为：禁用 urllib3 自动重试
    （retries=False），用 time.monotonic() 建立跨尝试共享的 deadline，每次尝试的
    连接/读取超时按剩余预算收紧，预算耗尽不再发起新尝试。仅明确只读方法
    （GET/HEAD/OPTIONS）可重试，交易写（POST/PUT/DELETE）绝不重试（传输结果
    未知时重试会掩盖首次尝试的未知结果，见 _RETRYABLE_METHODS），可重试异常
    仅限传输层（超时/连接失败/连接中断）。
    进行中的响应无法硬中断——read 超时只约束相邻字节间隔，服务端持续慢吐字节
    可拖延单次响应远超预算；故响应返回后再校验 deadline，超出预算的迟到成功
    一律按本次尝试超时处理（可重试），保证整次调用不在预算耗尽后返回成功。

    参数：
        request_fn: Callable，真实执行请求的函数（PoolManager.request 原实现）
        method: str，HTTP 方法
        url: str，请求 URL
        request_kwargs: dict，转发给 request_fn 的关键字参数；timeout/retries
            会被本层改写

    返回：
        与 PoolManager.request 相同：原始 HTTP 响应（在共享预算内到达）

    异常：
        urllib3.exceptions.HTTPError：末次尝试的传输层异常原样上抛；响应在预算
            耗尽后到达时抛 ReadTimeoutError；预算在两次尝试之间耗尽而无末次异常
            时抛 ConnectTimeoutError（均由 call_api 归一化为 GatewayTransportError）
    """
    base = _ensure_total_deadline(request_kwargs.get("timeout"))
    budget = base.total if isinstance(base.total, (int, float)) else float(_TOTAL_TIMEOUT_S)
    # 读构造期原始字段 _connect/_read 而非 connect_timeout/read_timeout 属性：
    # read_timeout 在 read 缺省且 total 有值时会按"total-已用连接时长"动态计算，
    # 计时器未启动直接抛 TimeoutStateError（urllib3 Timeout 实现细节）
    per_connect = base._connect if isinstance(base._connect, (int, float)) else budget
    per_read = base._read if isinstance(base._read, (int, float)) else budget
    deadline = time.monotonic() + budget
    request_kwargs["retries"] = False  # 重试由本层按共享 deadline 控制
    attempts = _RETRY_ATTEMPTS if method.upper() in _RETRYABLE_METHODS else 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break  # 预算耗尽：不再发起新尝试
        request_kwargs["timeout"] = urllib3.Timeout(
            connect=min(per_connect, remaining), read=min(per_read, remaining)
        )
        try:
            response = request_fn(method, url, **request_kwargs)
        except _RETRYABLE_TRANSPORT_ERRORS as exc:
            last_exc = exc
        else:
            if time.monotonic() <= deadline:
                return response
            # 进行中的响应无法硬中断（read 超时只约束相邻字节间隔，服务端持续
            # 慢吐字节可拖延单次响应远超预算）：响应返回后补验 deadline，超出
            # 预算的迟到成功按本次尝试超时处理，整次调用不得在预算耗尽后返回
            # 成功（PR #84 评审 P2）；POST 写路径由此落入"状态未知"契约
            last_exc = urllib3.exceptions.ReadTimeoutError(
                None, url, f"响应超过 {budget:.0f}s wall-clock 预算后到达，按超时处理"
            )
        backoff = _RETRY_BACKOFF_S * (2**attempt)
        if deadline - time.monotonic() > backoff:
            time.sleep(backoff)
    if last_exc is not None:
        raise last_exc
    raise urllib3.exceptions.ConnectTimeoutError(
        None, f"整次请求超过 {budget:.0f}s wall-clock 上限，未再发起新尝试"
    )


class _TimeoutApiClient(gate_api.ApiClient):
    """为未显式指定 _request_timeout 的调用注入默认（连接, 读取）超时的 ApiClient。

    gate_api 生成代码对每次调用都显式传 _request_timeout（缺省 None=不限时），
    故这里对 None 统一替换为默认超时；调用方显式给定的值（如下单 10s）原样优先。
    构造时包装 PoolManager.request 入口：每次请求经 _call_with_shared_deadline
    以单调时钟建立跨重试共享的 wall-clock 上限（SDK 的 tuple/int 转换到不了
    total，且 urllib3 的 total 每次重试会重新起表，见 _ensure_total_deadline）。
    """

    def __init__(self, configuration: gate_api.Configuration, *args, **kwargs) -> None:
        """初始化客户端：包装 PoolManager.request 为共享 deadline 版本后交父类装配。

        参数：
            configuration: gate_api.Configuration，SDK 客户端配置（原样透传）
            args: tuple，透传父类的位置参数
            kwargs: dict，透传父类的关键字参数

        返回：
            None，初始化实例（副作用：包装 rest_client.pool_manager.request）
        """
        super().__init__(configuration, *args, **kwargs)
        pool_manager = self.rest_client.pool_manager
        original_request = pool_manager.request

        def _request_with_total_deadline(method: str, url: str, **request_kwargs):
            """经共享 deadline 重试层转发 PoolManager.request 原实现。

            参数：
                method: str，HTTP 方法
                url: str，请求 URL
                request_kwargs: dict，原样转发的关键字参数；timeout/retries 由
                    _call_with_shared_deadline 按共享预算改写

            返回：
                与 PoolManager.request 相同：原始 HTTP 响应
            """
            return _call_with_shared_deadline(original_request, method, url, request_kwargs)

        pool_manager.request = _request_with_total_deadline

    def call_api(self, *args, **kwargs):
        """注入默认 _request_timeout 后转发父类实现，并归一化传输层异常。

        urllib3 的 ReadTimeoutError/ConnectTimeoutError/MaxRetryError 等传输层
        异常统一包装为 GatewayTransportError（label=TRANSPORT_UNKNOWN）：读路径
        由此获得稳定的 502/错误契约，写路径可据类型区分"明确拒绝"与"结果未知"
        （PR #84 评审 P2）。GateApiException 不继承 urllib3.HTTPError，不受影响。

        参数：
            args: tuple，原样转发的位置参数
            kwargs: dict，原样转发的关键字参数；_request_timeout 为 None 或缺失时
                注入默认（连接, 读取）超时

        返回：
            与父类 call_api 相同：反序列化后的响应对象

        异常：
            GatewayTransportError：传输层/网关层失败（超时、连接失败、无 label 的
                502/504、SSL 失败等）时抛出，请求可能已到达交易所，按"结果未知"处理
        """
        if kwargs.get("_request_timeout") is None:
            kwargs["_request_timeout"] = _DEFAULT_REQUEST_TIMEOUT
        try:
            return super().call_api(*args, **kwargs)
        except GateApiException:
            raise  # 带 label 的服务端明确拒绝：保持原样，由各方法 wrap_gate_exception 分类
        except (ApiException, _Urllib3HTTPError, AttributeError) as exc:
            # 原始 ApiException（无 label 的 502/504 代理响应、SSL status=0）、urllib3
            # 传输异常、SDK 对 body=None 解码产生的 AttributeError：请求可能已到达
            # 交易所，统一按"结果未知"归一化（PR #84 评审 P1）
            raise wrap_transport_exception(exc) from exc


def gen_client_order_id() -> str:
    """生成 text 自定义订单 ID：t- 前缀 + 26 位，总长 28 字节。

    参数：
        无

    返回：
        str：生成 text 自定义订单 ID：t- 前缀 + 26 位，总长 28 字节
    """
    return f"t-{uuid.uuid4().hex[:26]}"


def _fmt_decimal(value: Decimal) -> str:
    """Decimal -> 普通十进制字符串（避免科学计数法）。

    参数：
        value: Decimal，待转换或校验的值

    返回：
        str：Decimal -> 普通十进制字符串（避免科学计数法）
    """
    return format(value, "f")


def _validate_text(text: str) -> None:
    """校验调用方自带 text（Gate 自定义订单 ID 规则）：t- 前缀、≤28 字节、字符集 0-9A-Za-z_-。

    参数：
        text: str，待处理的文本

    返回：
        None：校验调用方自带 text（Gate 自定义订单 ID 规则）：t- 前缀、≤28 字节、字符集 0-9A-Za-z_-

    异常：
        ValueError：f"text 必须以 't-' 开头（Gate 自定义订单 ID 规则）: {text!r}" 所描述的条件发生时
        ValueError：f'text 总长不能超过 {_TEXT_MAX_BYTES} 字节: {text!r}' 所描述的条件发生时
        ValueError：f'text 只能包含字符 0-9A-Za-z_-: {text!r}' 所描述的条件发生时
    """
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

    参数：
        req: OrderRequest，业务下单请求

    返回：
        dict：把业务下单意图组装成 Gate 下单参数（纯函数，不触网）
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


def _dec(value: str | None) -> Decimal:
    """SDK 字符串字段 -> Decimal；空串/None 归一为 0。

    参数：
        value: str | None，待转换或校验的值

    返回：
        Decimal：SDK 字符串字段 -> Decimal；空串/None 归一为 0
    """
    if value in (None, ""):
        return Decimal(0)
    return Decimal(str(value))


def _dec_opt(value: str | None) -> Decimal | None:
    """SDK 字符串字段 -> Decimal；空串/None 保留为 None（区分"字段缺失"与数值 0）。

    参数：
        value: str | None，待转换的 SDK 原始字段

    返回：
        Decimal | None：字段存在时返回数值，缺失（None/空串）时返回 None
    """
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _optional_price(value: str | None) -> Decimal | None:
    """Gate 附带保护价的空串与零值都表示未配置。

    参数：
        value: str | None，待转换或校验的值

    返回：
        Decimal | None：Gate 附带保护价的空串与零值都表示未配置
    """
    price = _dec(value)
    return None if price == 0 else price


def _to_contract(c: gate_api.Contract) -> Contract:
    """把 Gate SDK 合约对象转换为系统内共用的合约模型。

    参数：
        c: gate_api.Contract，SDK 返回的合约原始对象

    返回：
        Contract：共用合约模型（下单步长、标记价、资金费率等元数据与实时信息）
    """
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
    """把 Gate SDK 持仓对象转换为系统内共用的持仓模型。

    保证金模式优先取 pos_margin_mode，缺失时按 leverage==0 推断全仓；
    全仓实际杠杆优先取 cross_leverage_limit（用户配置值，回滚锚点必须用它），
    缺失或为 0 时回退 lever（当前有效杠杆）。逐仓杠杆同样优先取 lever——
    Gate 新协议中 lever 是 isolated/cross 通用的当前杠杆字段（逐步替代旧
    leverage），旧字段在逐仓也可能为 0，只信旧字段会把真实杠杆快照成 1x。

    参数：
        p: gate_api.Position，SDK 返回的持仓原始对象

    返回：
        Position：共用持仓模型（止盈止损价不在此填充，保持默认 None）
    """
    lev_raw = _dec(p.leverage)
    # getattr 防御：旧版 SDK 或精简响应可能缺少 pos_margin_mode/lever 字段
    mode = (getattr(p, "pos_margin_mode", None) or "").strip().lower()
    if mode not in ("isolated", "cross"):
        mode = "cross" if lev_raw == 0 else "isolated"
    lever = _dec_opt(getattr(p, "lever", None))
    cross_limit = None
    if mode == "cross":
        # 回滚锚点须用配置值：cross_leverage_limit 是用户设定的全仓杠杆，
        # lever 为当前有效杠杆（可能非整数），仅作缺失时的回退
        cross_limit = _dec_opt(getattr(p, "cross_leverage_limit", None)) or lever
    elif lever is not None and lever > 0:
        lev_raw = lever
    return Position(
        contract=p.contract,
        size=_dec(p.size),
        entry_price=_dec(p.entry_price),
        mark_price=_dec(p.mark_price),
        liq_price=_dec(p.liq_price),
        leverage=lev_raw,
        margin=_dec(p.margin),
        unrealised_pnl=_dec(p.unrealised_pnl),
        margin_mode=mode,
        cross_leverage_limit=cross_limit,
    )


def _to_order(o: gate_api.FuturesOrder) -> OrderResult:
    """将 Gate futures order 转换为前后端共用的订单快照。

    参数：
        o: gate_api.FuturesOrder，SDK 返回的订单原始对象

    返回：
        OrderResult：共用订单模型（id 归一为字符串；附带保护价的空串/零值归一为 None）
    """
    return OrderResult(
        id=str(o.id),
        contract=o.contract or "",
        status=o.status or "",
        size=_dec(getattr(o, "size", 0)),
        price=_dec(getattr(o, "price", 0)),
        tif=getattr(o, "tif", "") or "",
        reduce_only=bool(getattr(o, "is_reduce_only", False)),
        stop_loss_price=_optional_price(getattr(o, "tpsl_sl_trigger_price", None)),
        take_profit_price=_optional_price(getattr(o, "tpsl_tp_trigger_price", None)),
        left=_dec(o.left),
        fill_price=_dec(o.fill_price),
        finish_as=o.finish_as or "",
        text=o.text or "",
    )


def _to_candle(k: gate_api.FuturesCandlestick) -> Candle:
    """把 Gate SDK K 线对象转换为系统内共用的 K 线模型。

    参数：
        k: gate_api.FuturesCandlestick，SDK 返回的 K 线原始对象

    返回：
        Candle：共用 K 线模型（t 为秒级时间戳，v 为成交量张数）
    """
    return Candle(t=int(k.t), o=_dec(k.o), h=_dec(k.h), l=_dec(k.l), c=_dec(k.c), v=_dec(k.v))


def _to_ticker(t: gate_api.FuturesTicker) -> Ticker:
    """把 Gate SDK ticker 对象转换为系统内共用的 ticker 模型。

    参数：
        t: gate_api.FuturesTicker，SDK 返回的 ticker 原始对象

    返回：
        Ticker：共用 ticker 模型（最新价、标记价、资金费率与 24h 高低点）
    """
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
    """Gate 价格触发单转换为本系统整仓保护单；非整仓平仓单不参与接管。

    参数：
        order: gate_api.FuturesPriceTriggeredOrder，待处理的订单对象

    返回：
        TpslOrder | None：Gate 价格触发单转换为本系统整仓保护单；非整仓平仓单不参与接管
    """
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


def _to_exchange_trade(t: gate_api.MyFuturesTrade) -> ExchangeTrade:
    """SDK 个人成交 -> 内部结构；id/order_id 归一为字符串（推送侧同为字符串键）。

    参数：
        t: gate_api.MyFuturesTrade，Gate SDK 个人成交对象

    返回：
        ExchangeTrade：SDK 个人成交 -> 内部结构；id/order_id 归一为字符串（推送侧同为字符串键）
    """
    return ExchangeTrade(
        id=str(t.id),
        order_id=str(t.order_id or ""),
        contract=t.contract,
        size=_dec(t.size),
        price=_dec(t.price),
        fee=_dec(t.fee),
        role=t.role or "",
        text=t.text or "",
        create_time=float(t.create_time),
    )


def _to_position_close_record(r: gate_api.PositionClose) -> PositionCloseRecord:
    """SDK 平仓盈亏历史 -> 内部结构（pnl 回填来源）。

    参数：
        r: gate_api.PositionClose，Gate SDK 平仓记录对象

    返回：
        PositionCloseRecord：SDK 平仓盈亏历史 -> 内部结构（pnl 回填来源）
    """
    return PositionCloseRecord(
        time=float(r.time),
        contract=r.contract,
        pnl=_dec(r.pnl),
        accum_size=_dec(r.accum_size),
        text=r.text or "",
    )


class GateRestGateway(GateOpenInterestMixin):
    """真实网关：只做 SDK 调用与异常/超时处理，下单语义由 build_order_payload 组装。"""

    def __init__(
        self, gate_config: GateConfig, api_key: str = "", api_secret: str = "", testnet: bool = True
    ) -> None:
        """按 Gate 配置初始化 SDK 客户端与结算币种。

        参数：
            gate_config: GateConfig，网关配置（结算币种、testnet/live 主机地址）
            api_key: str，交易所 API Key；省略时为空串（仅能访问公开接口）
            api_secret: str，交易所 API Secret；省略时为空串
            testnet: bool，是否连接测试网；省略时默认 True（测试网）

        返回：
            None，就地初始化实例的 SDK 客户端（self._api）与结算币种（self._settle）
        """
        host = gate_config.testnet_host if testnet else gate_config.live_host
        config = gate_api.Configuration(host=host, key=api_key, secret=api_secret)
        self._api = gate_api.FuturesApi(_TimeoutApiClient(config))
        self._settle = gate_config.settle

    @staticmethod
    def _exptime() -> int:
        """X-Gate-Exptime 头（毫秒）：当前时间 + 30 秒，防延迟重放。

        参数：
            无

        返回：
            int：X-Gate-Exptime 头（毫秒）：当前时间 + 30 秒，防延迟重放
        """
        return int(time.time() * 1000) + _EXPTIME_AHEAD_MS

    def get_contract(self, contract: str) -> Contract:
        """读取单个合约的元数据与实时标记信息。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            Contract：共用合约模型（下单步长、标记价、资金费率等）

        异常：
            GatewayError：交易所请求失败时抛出（合约不存在时为 ContractNotFound）
        """
        try:
            return _to_contract(self._api.get_futures_contract(self._settle, contract))
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def get_account(self) -> Account:
        """读取合约账户的可用余额与未实现盈亏。

        参数：无

        返回：
            Account：共用账户模型（可用余额、未实现盈亏）

        异常：
            GatewayError：交易所请求失败时抛出
        """
        try:
            acc = self._api.list_futures_accounts(self._settle)
            return Account(available=_dec(acc.available), unrealised_pnl=_dec(acc.unrealised_pnl))
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def list_positions(self) -> list[Position]:
        """读取全部持仓（单次 REST 裸读，止损/止盈字段不回填）。

        保持单次请求：持仓 + 逐合约保护单查询的 N+1 复合读取会作为一个 executor
        任务长期占住唯一网关线程，HIGH 人工平仓无法在子请求间插队，且任一保护单
        查询超时会把安全路径整体拖死（PR #84 评审 P1）。止损/止盈展示补全由
        async_io.read_positions_with_tpsl 逐合约独立调度、单合约失败降级。

        参数：无

        返回：
            list[Position]：共用持仓模型列表；stop_loss_price/take_profit_price 恒为 None

        异常：
            GatewayError：交易所请求失败时抛出
        """
        try:
            return [_to_position(p) for p in self._api.list_positions(self._settle)]
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def place_order(self, req: OrderRequest) -> OrderResult:
        """按业务下单意图向 Gate 下单；超时/网络异常时按 text 回查防重单。

        参数：
            req: OrderRequest，下单意图（合约、方向张数、价格、保护价、自定义订单 ID 等）

        返回：
            OrderResult：订单快照（正常创建或超时回查确认已创建）

        异常：
            GatewayError：交易所明确拒绝时抛出；超时回查确认订单未创建时抛出
                （label=ORDER_TIMEOUT_NOT_CREATED，可安全重试）
            OrderStateUnknown：下单超时且回查失败，订单状态未知（禁止盲目重试）
        """
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
        """下单超时后按 text 回查：已创建则返回结果；确认未创建可安全重试。

        参数：
            text: str，待处理的文本
            original: Exception，下单超时时捕获的原始异常

        返回：
            OrderResult：下单超时后按 text 回查：已创建则返回结果；确认未创建可安全重试

        异常：
            GatewayError：'下单请求超时，回查确认订单未创建，可安全重试' 所描述的条件发生时
            OrderStateUnknown：f'下单超时且回查失败（{wrapped}），订单状态未知，禁止盲目重试' 所描述的条件发生时
            OrderStateUnknown：f'下单超时且回查请求失败（{exc}），订单状态未知，禁止盲目重试' 所描述的条件发生时
        """
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
        """修改挂单的价格或张数；不需要修改的字段传 None。

        参数：
            contract: str，合约名（如 BTC_USDT）
            order_id: str，交易所订单 id
            price: Decimal | None，新委托价；省略时（None）不修改价格
            size: Decimal | None，新委托张数；省略时（None）不修改张数

        返回：
            OrderResult：修改后的订单快照

        异常：
            GatewayError：交易所请求失败时抛出
            OrderStateUnknown：改单超时或网络失败、订单状态未知时抛出（禁止盲目重试）
        """
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
        except GatewayTransportError as exc:  # 超时/网络失败：交易所可能已执行，状态未知
            raise OrderStateUnknown(
                f"改单超时或网络失败，订单 {order_id} 状态未知，禁止盲目重试，请人工核对",
                label="ORDER_STATE_UNKNOWN",
            ) from exc

    def cancel_order(self, contract: str, order_id: str) -> OrderResult:
        """撤销指定挂单。

        参数：
            contract: str，合约名（如 BTC_USDT）
            order_id: str，交易所订单 id

        返回：
            OrderResult：撤单后的订单快照

        异常：
            GatewayError：交易所请求失败时抛出（订单不存在时为 OrderNotFound）
            OrderStateUnknown：撤单超时或网络失败、订单状态未知时抛出（禁止盲目重试）
        """
        try:
            order = self._api.cancel_futures_order(
                self._settle, order_id, x_gate_exptime=self._exptime()
            )
            return _to_order(order)
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc
        except GatewayTransportError as exc:  # 超时/网络失败：交易所可能已执行，状态未知
            raise OrderStateUnknown(
                f"撤单超时或网络失败，订单 {order_id} 状态未知，禁止盲目重试，请人工核对",
                label="ORDER_STATE_UNKNOWN",
            ) from exc

    def list_orders(
        self,
        contract: str | None = None,
        status: str = "open",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[OrderResult]:
        """分页读取订单快照；支持省略 contract 的全合约查询，并透传分页参数给 Gate。

        参数：
            contract: str | None，合约名；省略时（None）查询全部合约
            status: str，订单状态（open/finished）；省略时默认 "open"
            limit: int | None，每页条数；省略时（None）由交易所默认
            offset: int，分页偏移量；省略时默认 0

        返回：
            list[OrderResult]：订单快照列表

        异常：
            GatewayError：交易所请求失败时抛出
        """
        try:
            kwargs: dict[str, object] = {"offset": offset}
            if contract is not None:
                kwargs["contract"] = contract
            if limit is not None:
                kwargs["limit"] = limit
            orders = self._api.list_futures_orders(self._settle, status, **kwargs)
            return [_to_order(o) for o in orders]
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def list_tpsl_orders(self, contract: str) -> list[TpslOrder]:
        """读取指定合约当前生效（open）的整仓止盈止损保护单。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            list[TpslOrder]：整仓保护单列表（非整仓平仓的触发单已被过滤）

        异常：
            GatewayError：交易所请求失败时抛出
        """
        try:
            orders = self._api.list_price_triggered_orders(self._settle, "open", contract=contract)
            return [mapped for raw in orders if (mapped := _to_tpsl(raw)) is not None]
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def list_my_trades(self, contract: str | None = None, limit: int = 100) -> list[ExchangeTrade]:
        """个人成交历史（补漏用）：按时间倒序拉最近 limit 条，调用方按水线自行过滤。

        参数：
            contract: str | None，合约名称
            limit: int，最多读取或返回的记录数量

        返回：
            list[ExchangeTrade]：个人成交历史（补漏用）：按时间倒序拉最近 limit 条，调用方按水线自行过滤

        异常：
        GatewayError：Gate SDK 成交历史请求失败并被统一包装时
        """
        try:
            kwargs: dict[str, object] = {"limit": limit, "_request_timeout": _FILLS_TIMEOUT_S}
            if contract is not None:
                kwargs["contract"] = contract
            return [_to_exchange_trade(t) for t in self._api.get_my_trades(self._settle, **kwargs)]
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def list_position_close(
        self, contract: str, from_ts: float, to_ts: float
    ) -> list[PositionCloseRecord]:
        """平仓盈亏历史（pnl 回填用）：[from_ts, to_ts] 时间窗内按合约过滤。

        参数：
            contract: str，合约名称
            from_ts: float，查询窗口起始时间戳
            to_ts: float，查询窗口结束时间戳

        返回：
            list[PositionCloseRecord]：平仓盈亏历史（pnl 回填用）：[from_ts, to_ts] 时间窗内按合约过滤

        异常：
        GatewayError：Gate SDK 平仓历史请求失败并被统一包装时
        """
        try:
            rows = self._api.list_position_close(
                self._settle,
                contract=contract,
                _from=int(from_ts),
                to=int(to_ts),
                _request_timeout=_FILLS_TIMEOUT_S,
            )
            return [_to_position_close_record(r) for r in rows]
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder:
        """创建整仓止盈止损保护单（触发后市价全平持仓）。

        参数：
            order: TpslOrder，保护单意图（合约、方向、类型、触发价）；id 无需填写，创建后回填

        返回：
            TpslOrder：已创建的保护单（id 已回填为交易所侧 id）

        异常：
            GatewayError：交易所明确拒绝时抛出
            OrderStateUnknown：请求超时或网络失败，保护单状态未知（禁止盲目重试）
        """
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
        """撤销指定的整仓止盈止损保护单。

        参数：
            order_id: str，交易所侧保护单 id

        返回：
            None，撤单请求已被交易所受理

        异常：
            GatewayError：交易所明确拒绝时抛出
            OrderStateUnknown：请求超时或网络失败，保护单状态未知（需人工核对）
        """
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
        """K 线查询：limit 路径可用，单次最多 2000 点。

        公开参数 from_ts/to_ts 对应 SDK 的 _from/to；当前区间关键字尚未正确映射，
        传入历史区间会被 gate-api 拒绝，修复前不要依赖该路径。

        参数：
            contract: str，合约名称
            interval: str，行情或统计周期
            limit: int | None，最多读取或返回的记录数量
            from_ts: int | None，查询窗口起始时间戳
            to_ts: int | None，查询窗口结束时间戳

        返回：
            list[Candle]：K 线查询：limit 路径可用，单次最多 2000 点

        异常：
            ValueError：'limit 与 from/to 互斥，不能同时传' 所描述的条件发生时
            ValueError：'limit 必须在 1~2000 之间' 所描述的条件发生时
        GatewayError：Gate SDK K 线请求失败并被统一包装时
        """
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
        """读取全部合约的 ticker 摘要。

        参数：无

        返回：
            list[Ticker]：共用 ticker 模型列表（最新价、标记价、资金费率、24h 高低等）

        异常：
            GatewayError：交易所请求失败时抛出
        """
        try:
            return [_to_ticker(t) for t in self._api.list_futures_tickers(self._settle)]
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc

    def set_leverage(self, contract: str, leverage: int, margin_mode: str = "isolated") -> Position:
        """通过当前持仓杠杆接口设置 isolated/cross 模式与杠杆倍数。

        参数：
            contract: str，合约名称
            leverage: int，目标杠杆倍数
            margin_mode: str，逐仓或全仓保证金模式

        返回：
            Position：通过当前持仓杠杆接口设置 isolated/cross 模式与杠杆倍数

        异常：
            ValueError：f'非法 margin_mode: {margin_mode}（可选 isolated/cross）' 所描述的条件发生时
        GatewayError：Gate SDK 杠杆设置请求失败并被统一包装时
        """
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
