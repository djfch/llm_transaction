"""ticker 总闸工厂：paper 撮合 / 触发器检查 / 行情广播 / 成交即时落库的扇出回调。

各分支独立捕获异常记日志，不外抛（护住 WS 任务）。自 bootstrap.py 拆出（文件体量门禁）。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from src.agent.fill_persist import FillPersister, schedule_drain
from src.audit.logger import get_logger
from src.gateway.base import Gateway, Ticker
from src.market.triggers import TriggerManager
from src.paper.engine import PaperGateway

logger = get_logger(__name__)


def make_on_ticker(
    gateway: Gateway,
    triggers: TriggerManager,
    broadcast: Callable[[dict], None] | None = None,
    *,
    broadcast_interval: float = 1.0,
    fill_persister: FillPersister | None = None,
) -> Callable[[Ticker], None]:
    """ticker 总闸：paper 撮合、触发器检查、WS 行情广播各自捕获异常记日志，不外抛（护住 WS 任务）。

    broadcast：每合约按 broadcast_interval 秒节流后推 {"type":"ticker",...}（前端实时价）；
    last 转 float（Decimal 无法被 ws send_json 序列化）。
    fill_persister 非空且撮合后成交缓冲有货时调度即时落库（create_task 后立即返回，
    落库不在行情回调关键路径；drain 与落库在 FillPersister 锁内完成，与轮末兜底
    drain、手动平仓互斥，不会双计）。

    参数：
        gateway: Gateway，交易所网关
        triggers: TriggerManager，价格预警线管理器
        broadcast: Callable[[dict], None] | None，WebSocket 行情广播回调
        broadcast_interval: float，同合约行情广播最小间隔秒数
        fill_persister: FillPersister | None，可选的即时成交持久化器
    返回：
        Callable[[Ticker], None]，ticker 总闸：paper 撮合、触发器检查、WS 行情广播各自捕获异常记日志，不外抛（护住 WS 任务）
    """
    last_sent: dict[str, float] = {}

    def on_ticker(ticker: Ticker) -> None:
        """处理单笔 ticker 行情：驱动 paper 撮合、成交即时落库调度、触发器检查与节流广播。

        参数：
            ticker: Ticker，最新行情快照（含合约名、标记价与最新成交价）

        返回：
            None，只产生副作用（撮合、落库调度、触发器触发、WS 行情广播）；
            各分支异常仅记日志不外抛，护住 WS 行情任务
        """
        if isinstance(gateway, PaperGateway):
            try:
                gateway.on_price(ticker.contract, ticker.mark_price, ticker.last, ticker.last)
            except Exception:
                logger.exception("paper 撮合异常（%s）", ticker.contract)
            if fill_persister is not None and gateway.account.fills:
                schedule_drain(fill_persister, gateway.drain_fills, ticker.contract)
        try:
            triggers.check(ticker.contract, ticker.last)
        except Exception:
            logger.exception("触发器检查异常（%s）", ticker.contract)
        if broadcast is not None:
            now = time.monotonic()
            if now - last_sent.get(ticker.contract, float("-inf")) >= broadcast_interval:
                last_sent[ticker.contract] = now
                try:
                    broadcast(
                        {
                            "type": "ticker",
                            "data": {"contract": ticker.contract, "last": float(ticker.last)},
                        }
                    )
                except Exception:
                    logger.exception("ticker 广播异常（%s）", ticker.contract)

    return on_ticker
