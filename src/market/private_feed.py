"""Gate 永续私有频道 WS 封装（成交回报对账用），testnet/live 专用。

gatews 行为约定与 feed.py 相同（协程回调、ACK 只认 "update"、指数退避守护、
断线自动重放订阅）；私有频道额外约定：
- 需在 Configuration 注入 api_key/api_secret（HMAC 签名由 gatews 完成，见 client.py）
- 订阅 ACK 的 error 非空表示权限/参数问题：回调 on_error（启动告警钩子，由调用方
  决定告警策略），本层不重试
- run() 异常退出并退避后回调 on_reconnected（补漏钩子）：重启期间错过的成交由
  调用方经 REST 补齐；gatews 重启 run() 时会自动重放订阅
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from gate_ws import Configuration, Connection, WebSocketResponse
from gate_ws.futures import (
    FuturesAutoOrdersChannel,
    FuturesLiquidatesChannel,
    FuturesUserTradesChannel,
)

from src.market.feed import maybe_await

logger = logging.getLogger(__name__)

RawHandler = Callable[[dict], Awaitable[None] | None]  # 原始推送条目（字段以 testnet 实测为准）
EventHook = Callable[[], Awaitable[None] | None]
ErrorHook = Callable[[str], Awaitable[None] | None]

_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_ALL = ["!all"]  # Gate 私有频道全合约通配


class PrivateTradeFeed:
    """futures.usertrades/autoorders/liquidates 三频道订阅。start/stop 为 asyncio 接口。"""

    def __init__(
        self,
        settle: str = "usdt",
        *,
        testnet: bool,
        api_key: str,
        api_secret: str,
        ws_host: str = "",
    ) -> None:
        """初始化私有频道订阅配置与内部状态（此时尚未建立连接，连接在 start 时创建）。

        参数：
            settle: str，结算币种（如 "usdt"）
            testnet: bool，是否连接 testnet 环境
            api_key: str，交易所 API Key（私有频道 HMAC 鉴权用）
            api_secret: str，交易所 API Secret（私有频道 HMAC 鉴权用）
            ws_host: str，testnet 专用 WS 地址覆盖；留空时使用 SDK 内置地址

        返回：
            None，仅写入实例字段，无网络副作用
        """
        self._settle = settle
        self._testnet = testnet
        self._api_key = api_key
        self._api_secret = api_secret
        self._ws_host = ws_host  # testnet 专用覆盖（SDK 内置 testnet 地址已失效）
        self._on_user_trade: RawHandler | None = None
        self._on_auto_order: RawHandler | None = None
        self._on_liquidation: RawHandler | None = None
        self._on_reconnected: EventHook | None = None
        self._on_error: ErrorHook | None = None
        self._conn: Connection | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    def set_handlers(
        self,
        on_user_trade: RawHandler | None = None,
        on_auto_order: RawHandler | None = None,
        on_liquidation: RawHandler | None = None,
        on_reconnected: EventHook | None = None,
        on_error: ErrorHook | None = None,
    ) -> None:
        """注册处理函数与钩子（须在 start 前调用）。

        参数：
            on_user_trade: RawHandler | None，用户成交事件处理回调
            on_auto_order: RawHandler | None，自动订单事件处理回调
            on_liquidation: RawHandler | None，强平事件处理回调
            on_reconnected: EventHook | None，重连后的补漏回调
            on_error: ErrorHook | None，私有订阅错误回调
        返回：
            None，注册处理函数与钩子（须在 start 前调用）
        """
        if on_user_trade is not None:
            self._on_user_trade = on_user_trade
        if on_auto_order is not None:
            self._on_auto_order = on_auto_order
        if on_liquidation is not None:
            self._on_liquidation = on_liquidation
        if on_reconnected is not None:
            self._on_reconnected = on_reconnected
        if on_error is not None:
            self._on_error = on_error

    async def start(self) -> None:
        """启动私有频道订阅：创建 WS 连接、订阅三频道并拉起守护协程；已在运行中时为空操作。

        参数：无

        返回：
            None，后台守护任务在事件循环中持续运行，直至 stop 被调用
        """
        if self._running:
            return
        self._running = True
        # Connection 须在运行中的事件循环内创建（gatews 取当前 loop）；
        # host 仅 testnet 生效：SDK 内置 testnet 地址已 502 失效，与 feed.py 同款覆盖
        self._conn = Connection(
            Configuration(
                app="futures",
                settle=self._settle,
                test_net=self._testnet,
                api_key=self._api_key,
                api_secret=self._api_secret,
                host=self._ws_host if self._testnet else "",
                max_retry=1,
            )
        )
        trades_ch = FuturesUserTradesChannel(self._conn, self._handle_user_trade)
        auto_ch = FuturesAutoOrdersChannel(self._conn, self._handle_auto_order)
        liq_ch = FuturesLiquidatesChannel(self._conn, self._handle_liquidation)
        trades_ch.subscribe(_ALL)
        auto_ch.subscribe(_ALL)
        liq_ch.subscribe(_ALL)
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        """停止订阅：标记退出、关闭 WS 连接并取消守护协程，等待其退出后释放资源。

        参数：无

        返回：
            None，连接与后台任务就地清理，取消过程中的 CancelledError 已被吞掉
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
        """守护 conn.run()：异常退出后指数退避重启；重启前回调 on_reconnected（补漏）。

        参数：无
        返回：
            None，守护 conn.run()：异常退出后指数退避重启；重启前回调 on_reconnected（补漏）
        异常：
            asyncio.CancelledError，守护任务被取消时原样抛出
        """
        backoff = _INITIAL_BACKOFF
        while self._running:
            try:
                assert self._conn is not None
                await self._conn.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("私有 WS 连接异常退出")
            if not self._running:
                break
            logger.warning("私有 WS %.0f 秒后重连（指数退避）", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)
            if self._on_reconnected is not None:
                try:
                    await maybe_await(self._on_reconnected())
                except Exception:
                    logger.exception("私有 WS 重连补漏回调异常")

    async def _dispatch(
        self, response: WebSocketResponse, handler: RawHandler | None, label: str
    ) -> None:
        """分发单条频道推送：错误 ACK 走 on_error 钩子，update 事件逐条交给业务回调。

        参数：
            response: WebSocketResponse，gatews 回调收到的推送响应
            handler: RawHandler | None，该频道的业务处理函数；为 None 时丢弃推送
            label: str，频道名标签，仅用于日志与错误提示文案

        返回：
            None，回调与钩子内的异常均已捕获并记日志，不向 gatews 传播
        """
        if response.error:
            logger.warning("%s 频道异常 ACK/错误：%s", label, response.error)
            if self._on_error is not None:
                try:
                    await maybe_await(self._on_error(f"{label}: {response.error}"))
                except Exception:
                    logger.exception("私有 WS 错误回调异常")
            return
        if response.event != "update":
            return
        for raw in response.result or []:
            if handler is None:
                continue
            try:
                await maybe_await(handler(raw))
            except Exception:
                logger.exception("%s 回调异常，已跳过：%s", label, raw)

    async def _handle_user_trade(self, conn: Connection, response: WebSocketResponse) -> None:
        """usertrades 频道入口：把 gatews 成交回报回调转发给统一分发逻辑。

        参数：
            conn: Connection，gatews 连接对象（回调签名要求，本方法未使用）
            response: WebSocketResponse，成交回报推送响应

        返回：
            None，成交条目交给已注册的 on_user_trade 处理
        """
        await self._dispatch(response, self._on_user_trade, "usertrades")

    async def _handle_auto_order(self, conn: Connection, response: WebSocketResponse) -> None:
        """autoorders 频道入口：把 gatews 条件单回调转发给统一分发逻辑。

        参数：
            conn: Connection，gatews 连接对象（回调签名要求，本方法未使用）
            response: WebSocketResponse，条件单推送响应

        返回：
            None，条件单条目交给已注册的 on_auto_order 处理
        """
        await self._dispatch(response, self._on_auto_order, "autoorders")

    async def _handle_liquidation(self, conn: Connection, response: WebSocketResponse) -> None:
        """liquidates 频道入口：把 gatews 强平回调转发给统一分发逻辑。

        参数：
            conn: Connection，gatews 连接对象（回调签名要求，本方法未使用）
            response: WebSocketResponse，强平推送响应

        返回：
            None，强平条目交给已注册的 on_liquidation 处理
        """
        await self._dispatch(response, self._on_liquidation, "liquidates")
