"""Gate 永续 WS 行情订阅封装（gatews 官方包），实盘 PriceSource 实现。

实测确认的 gatews 行为（scripts/check_feed.py 可复验，禁止猜测）：
- 回调签名 callback(conn, response)；协程回调由 gatews create_task 调度，
  同步回调会被扔进线程池（run_in_executor），故本层只用协程回调
- 订阅 ACK 与行情推送走同一频道名：event=="subscribe" 为 ACK，只处理 "update"
- ticker 订阅 payload 为合约名列表；K 线 payload 为 [interval, contract]
- K 线结果字段：t/o/h/l/c/v + n="{interval}_{contract}" + w（True=收盘）
- gatews 内部断线自动重连并回放订阅（sending_history），但退避是线性的，
  且放弃重连后 run() 直接返回；本层用指数退避重启 run()，保证长期在线
- 解析或业务回调异常由本层逐条捕获记日志，不外抛进 gatews 任务
  （协程回调由 gatews create_task 调度，异常外抛即 exception never retrieved）
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from decimal import Decimal
from typing import Awaitable, Callable

from gate_ws import Configuration, Connection, WebSocketResponse
from gate_ws.futures import FuturesCandlesticksChannel, FuturesTickerChannel

from ..gateway.base import Candle, Ticker

logger = logging.getLogger(__name__)

TickerHandler = Callable[[Ticker], Awaitable[None] | None]
# 参数：contract, interval, candle, closed（w=true 收盘）
CandleHandler = Callable[[str, str, Candle, bool], Awaitable[None] | None]

_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0


async def maybe_await(result: Awaitable[None] | None) -> None:
    """处理函数允许同步或协程，统一在此消化。

    参数：
        result: Awaitable[None] | None，待序列化或返回的执行结果

    返回：
        None：处理函数允许同步或协程，统一在此消化
    """
    if inspect.isawaitable(result):
        await result


def parse_ticker(raw: dict) -> Ticker:
    """解析 futures.tickers 推送条目（字段已实测）。

    参数：
        raw: dict，待解析或保留的原始数据

    返回：
        Ticker：解析 futures.tickers 推送条目（字段已实测）
    """
    return Ticker(
        contract=raw["contract"],
        last=Decimal(str(raw["last"])),
        mark_price=Decimal(str(raw["mark_price"])),
        funding_rate=Decimal(str(raw["funding_rate"])),
        high_24h=Decimal(str(raw["high_24h"])),
        low_24h=Decimal(str(raw["low_24h"])),
        change_percentage=Decimal(str(raw["change_percentage"])),
    )


def parse_ws_candle(raw: dict) -> tuple[str, str, Candle, bool]:
    """解析 futures.candlesticks 推送条目，返回 (contract, interval, candle, closed)。

    参数：
        raw: dict，待解析或保留的原始数据

    返回：
        tuple[str, str, Candle, bool]：解析 futures.candlesticks 推送条目，返回 (contract, interval, candle, closed)
    """
    interval, contract = raw["n"].split("_", 1)
    candle = Candle(
        t=int(raw["t"]),
        o=Decimal(str(raw["o"])),
        h=Decimal(str(raw["h"])),
        l=Decimal(str(raw["l"])),  # noqa: E741
        c=Decimal(str(raw["c"])),
        v=Decimal(str(raw["v"])),
    )
    return contract, interval, candle, bool(raw.get("w"))


class MarketFeed:
    """白名单合约的 ticker + K 线 WS 订阅。start/stop 为 asyncio 接口。"""

    def __init__(
        self,
        contracts: list[str],
        intervals: list[str],
        settle: str = "usdt",
        testnet: bool = False,
        ws_host: str = "",  # testnet 专用 WS 地址覆盖（SDK 内置 testnet 地址已失效）
        on_ticker: TickerHandler | None = None,
        on_candle: CandleHandler | None = None,
    ) -> None:
        """保存订阅配置与行情处理函数，初始化运行状态（不建立连接）。

        参数：
            contracts: list[str]，要订阅的合约白名单（如 BTC_USDT）
            intervals: list[str]，要订阅的 K 线周期列表（如 1m、5m）
            settle: str，结算币种，省略时为 usdt
            testnet: bool，是否连接 Gate 测试网，省略时连接实盘
            ws_host: str，testnet 专用 WS 地址覆盖（SDK 内置 testnet 地址已失效），
                仅 testnet 生效，省略时走 SDK 默认地址
            on_ticker: TickerHandler | None，ticker 行情处理函数，省略时收到行情只解析不回调
            on_candle: CandleHandler | None，K 线行情处理函数，省略时收到行情只解析不回调

        返回：
            None，仅就地初始化内部字段，WS 连接推迟到 start 时建立
        """
        self._contracts = list(contracts)
        self._intervals = list(intervals)
        self._settle = settle
        self._testnet = testnet
        self._ws_host = ws_host
        self._on_ticker = on_ticker
        self._on_candle = on_candle
        self._conn: Connection | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    def set_handlers(
        self,
        on_ticker: TickerHandler | None = None,
        on_candle: CandleHandler | None = None,
    ) -> None:
        """注册/替换处理函数（PriceSource 接口，须在 start 前调用）。

        参数：
            on_ticker: TickerHandler | None，行情处理回调
            on_candle: CandleHandler | None，K 线处理回调

        返回：
            None：注册/替换处理函数（PriceSource 接口，须在 start 前调用）
        """
        if on_ticker is not None:
            self._on_ticker = on_ticker
        if on_candle is not None:
            self._on_candle = on_candle

    async def start(self) -> None:
        """建立 WS 连接、订阅 ticker 与 K 线频道，并启动断线守护协程（重复调用幂等）。

        参数：无

        返回：
            None，后台创建守护任务；已在运行中时不做任何事直接返回
        """
        if self._running:
            return
        self._running = True
        # Connection 须在运行中的事件循环内创建（gatews 取当前 loop）；
        # host 仅 testnet 生效：SDK 内置 testnet 地址（fx-ws-testnet.gateio.ws）已 502 失效，
        # 由 gate.testnet_ws_host 配置提供；live 留空走 SDK 默认
        self._conn = Connection(
            Configuration(
                app="futures",
                settle=self._settle,
                test_net=self._testnet,
                host=self._ws_host if self._testnet else "",
                max_retry=1,
            )
        )
        ticker_ch = FuturesTickerChannel(self._conn, self._handle_ticker)
        candle_ch = FuturesCandlesticksChannel(self._conn, self._handle_candle)
        ticker_ch.subscribe(self._contracts)
        for interval in self._intervals:
            for contract in self._contracts:
                candle_ch.subscribe([interval, contract])
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        """停止守护协程并关闭 WS 连接。

        参数：无

        返回：
            None，取消并等待守护任务退出后释放连接与任务引用
        """
        self._running = False
        if self._conn is not None:
            self._conn.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _supervise(self) -> None:
        """守护 conn.run()：gatews 放弃重连后（run 返回/抛错）指数退避重启。

        参数：
            无

        返回：
            None：守护 conn.run()：gatews 放弃重连后（run 返回/抛错）指数退避重启

        异常：
            asyncio.CancelledError：守护任务被取消时保持协程取消语义并原样抛出
        """
        backoff = _INITIAL_BACKOFF
        while self._running:
            try:
                await self._conn.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("WS 连接异常退出")
            if not self._running:
                break
            logger.warning("WS %.0f 秒后重连（指数退避）", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _handle_ticker(self, conn: Connection, response: WebSocketResponse) -> None:
        """ticker 频道协程回调：忽略订阅 ACK，逐条解析行情推送并分发给已注册的处理函数。

        参数：
            conn: Connection，产生推送的 WS 连接（gatews 回调签名约定，本函数不使用）
            response: WebSocketResponse，ticker 频道响应（event 为 subscribe 的 ACK 或
                update 的行情推送）

        返回：
            None，单条解析或回调失败仅记日志并跳过，异常不外抛进 gatews 任务
        """
        if response.error or response.event != "update":
            if response.error:
                logger.warning("ticker 频道异常 ACK（订阅可能被拒）：%s", response.error)
            return
        for raw in response.result or []:
            try:
                ticker = parse_ticker(raw)
            except Exception:
                logger.exception("ticker 解析失败，已跳过：%s", raw)
                continue
            if self._on_ticker is not None:
                try:
                    await maybe_await(self._on_ticker(ticker))
                except Exception:
                    logger.exception("ticker 回调异常（%s）", ticker.contract)

    async def _handle_candle(self, conn: Connection, response: WebSocketResponse) -> None:
        """K 线频道协程回调：忽略订阅 ACK，逐条解析 K 线推送并分发给已注册的处理函数。

        参数：
            conn: Connection，产生推送的 WS 连接（gatews 回调签名约定，本函数不使用）
            response: WebSocketResponse，K 线频道响应（event 为 subscribe 的 ACK 或
                update 的 K 线推送）

        返回：
            None，单条解析或回调失败仅记日志并跳过，异常不外抛进 gatews 任务
        """
        if response.error or response.event != "update":
            if response.error:
                logger.warning("K 线频道异常 ACK（订阅可能被拒）：%s", response.error)
            return
        for raw in response.result or []:
            try:
                contract, interval, candle, closed = parse_ws_candle(raw)
            except Exception:
                logger.exception("K 线解析失败，已跳过：%s", raw)
                continue
            if self._on_candle is not None:
                try:
                    await maybe_await(self._on_candle(contract, interval, candle, closed))
                except Exception:
                    logger.exception("K 线回调异常（%s %s）", contract, interval)
