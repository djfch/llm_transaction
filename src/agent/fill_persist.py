"""统一成交写入入口（FillPersister）：轮末 drain / 手动平仓 / 行情即时 drain 三方共用。

- 归属继承：按 fill.order_id 查 orders 表继承 round_id（LLM 单继承下单轮、
  历史挂单触发继承原下单轮）；查不到（liquidation 强平、tpsl-* 止盈止损等
  无订单行的成交）→ round_id=''（前端标记照常绘制但不可点击）
- 双计防护：drain（同步取空网关缓冲）与落库处于同一把 asyncio.Lock 临界区，
  三方互斥——同一批成交只被 drain 走一次、只落库一次；手动平仓持锁覆盖
  「下单→drain→落库」全程，行情即时 drain 抢不走其成交（user_close 标注不丢失）
- 失败语义：单笔失败记日志继续，不重试（成交已在网关账本，重试可能双计）；
  批次成功 ≥1 笔发一次 trades_updated 失效信号（契约见 src/server/ws.py）
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from src.audit.logger import get_logger
from src.memory.repo import Repo
from src.paper.account import FillRecord

logger = get_logger(__name__)


def trade_source_of(fill: FillRecord) -> str:
    """落库时的 source 推导：强平 > 止盈止损 > LLM 平仓 > LLM 开仓（user_close 由调用方覆盖）。"""
    if fill.order_id == "liquidation":
        return "liquidation"
    if fill.order_id.startswith("tpsl-"):
        return "tpsl_close"
    return "llm_close" if fill.is_close else "llm_open"


class FillPersister:
    """成交统一落库器：构造期注入 repo/mode/notify_event，三方临界区共用内置锁。"""

    def __init__(
        self,
        repo: Repo,
        mode: str,
        notify_event: Callable[[dict], None] | None = None,
    ) -> None:
        self._repo = repo
        self._mode = mode
        self._notify_event = notify_event
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        """手动平仓等「多步操作须同一临界区」的场景持锁用；持锁期间只能调 persist_locked。"""
        return self._lock

    async def drain_persist(self, drain_fills: Callable[[], list[FillRecord]]) -> int:
        """锁内 drain（取空网关缓冲）+ 落库：轮末 drain 与行情即时 drain 用。"""
        async with self._lock:
            return await self.persist_locked(drain_fills())

    async def persist_locked(self, fills: list[FillRecord], *, source_override: str = "") -> int:
        """逐笔继承归属并落 trades 表；调用方须已持 self.lock（或经 drain_persist 进入）。

        单笔失败不中断：剩余成交继续落，失败记日志（成交已在网关账本，重试可能双计）。
        返回失败笔数；批次成功 ≥1 笔发一次 trades_updated（contracts 去重，count=成功笔数）。
        """
        failures = 0
        saved_contracts: set[str] = set()
        for fill in fills:
            try:
                round_id = await self._repo.order_round_id(fill.order_id) or ""
            except Exception:
                # 归属查询失败不丢成交：降级为无归属落库（可见不可点），保记录优先
                round_id = ""
                logger.exception("成交归属查询失败，按无归属落库 order=%s", fill.order_id)
            try:
                await self._repo.save_trade(
                    round_id=round_id,
                    mode=self._mode,
                    contract=fill.contract,
                    size=fill.size,
                    price=fill.price,
                    fee=fill.fee,
                    pnl=fill.realized_pnl,
                    source=source_override or trade_source_of(fill),
                )
                saved_contracts.add(fill.contract)
            except Exception:
                failures += 1
                logger.exception("成交落库失败 round=%s order=%s", round_id[:8], fill.order_id)
        if saved_contracts and self._notify_event is not None:
            self._notify_event(
                {
                    "type": "trades_updated",
                    "data": {"contracts": sorted(saved_contracts), "count": len(fills) - failures},
                }
            )
        return failures


async def _drain_safely(
    persister: FillPersister, drain_fills: Callable[[], list[FillRecord]], contract: str
) -> None:
    """即时 drain 的任务体：异常仅记日志（护住行情任务）。

    drain 已取空缓冲，失败笔不可重试——落库/归属失败已在 persist_locked 内逐笔记日志，
    成交仍在网关账本，可事后对账。
    """
    try:
        await persister.drain_persist(drain_fills)
    except Exception:
        logger.exception("即时成交落库异常（%s）", contract)


_pending_drains: set[asyncio.Task] = set()  # 事件循环对任务只持弱引用，须强引用至完成


def schedule_drain(
    persister: FillPersister, drain_fills: Callable[[], list[FillRecord]], contract: str
) -> None:
    """从同步回调（行情线程）调度一次即时 drain：create_task 后立即返回，不占关键路径。

    无运行中事件循环（同步单测直接调 on_ticker）时记 warning 跳过——轮末兜底 drain
    仍会落库，不丢成交。
    """
    try:
        task = asyncio.get_running_loop().create_task(
            _drain_safely(persister, drain_fills, contract)
        )
    except RuntimeError:
        logger.warning("无运行中事件循环，跳过即时成交落库（%s）", contract)
        return
    _pending_drains.add(task)
    task.add_done_callback(_pending_drains.discard)
