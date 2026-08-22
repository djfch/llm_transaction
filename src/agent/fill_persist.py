"""统一成交写入入口（FillPersister）：轮末 drain / 手动平仓 / 行情即时 drain 三方共用。

- 归属继承：按 fill.order_id 查 orders 表继承 round_id（LLM 单继承下单轮、
  历史挂单触发继承原下单轮）；查不到（liquidation 强平、tpsl-* 止盈止损等
  无订单行的成交）→ round_id=''（前端标记照常绘制但不可点击）
- 双计防护：drain（同步取空网关缓冲）与落库处于同一把 asyncio.Lock 临界区，
  三方互斥——同一批成交只被 drain 走一次、只落库一次；手动平仓持锁覆盖
  「下单→drain→落库」全程，行情即时 drain 抢不走其成交（user_close 标注不丢失）
- 失败语义：单笔失败记日志并进入待重试队列，下轮 drain 先重试（幂等键
  trade_id 配合 trades 唯一索引防双计，issue #67）；批次成功 ≥1 笔发一次
  trades_updated 失效信号（契约见 src/server/ws.py）
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from src.audit.logger import get_logger
from src.memory.repo import Repo
from src.paper.account import FillRecord

logger = get_logger(__name__)


def trade_source_of(fill: FillRecord) -> str:
    """落库时的 source 推导：强平 > 止盈止损 > LLM 平仓 > LLM 开仓（user_close 由调用方覆盖）。

    参数：
        fill: FillRecord，单笔模拟成交
    返回：
        str，落库时的 source 推导：强平 > 止盈止损 > LLM 平仓 > LLM 开仓（user_close 由调用方覆盖）
    """
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
        """注入持久化依赖并初始化三方共用的互斥锁。

        参数：
            repo: Repo，SQLite 仓储，用于查订单归属（round_id）与写 trades 表
            mode: str，运行模式（paper/testnet/live），随成交一并落库
            notify_event: Callable[[dict], None] | None，批次落库成功后的
                trades_updated 失效信号回调；省略则不通知前端

        返回：
            None，初始化实例字段（副作用：创建 asyncio.Lock 供三方临界区共用）
        """
        self._repo = repo
        self._mode = mode
        self._notify_event = notify_event
        self._lock = asyncio.Lock()
        self._pending: list[FillRecord] = []  # 落库失败笔待重试队列（issue #67）

    @property
    def lock(self) -> asyncio.Lock:
        """手动平仓等「多步操作须同一临界区」的场景持锁用；持锁期间只能调 persist_locked。

        参数：无
        返回：
            asyncio.Lock，手动平仓等「多步操作须同一临界区」的场景持锁用；持锁期间只能调 persist_locked
        """
        return self._lock

    async def drain_persist(self, drain_fills: Callable[[], list[FillRecord]]) -> int:
        """锁内 drain（取空网关缓冲）+ 落库：轮末 drain 与行情即时 drain 用。

        参数：
            drain_fills: Callable[[], list[FillRecord]]，取空成交缓冲区的回调
        返回：
            int，本批仍失败的笔数（0 表示全部落库成功）
        """
        async with self._lock:
            return await self.persist_locked(drain_fills())

    async def persist_locked(self, fills: list[FillRecord], *, source_override: str = "") -> int:
        """逐笔继承归属并落 trades 表；调用方须已持 self.lock（或经 drain_persist 进入）。

        先重试上轮失败笔再处理新成交；单笔失败不中断，失败笔进入 _pending
        待重试队列（幂等键配合唯一索引防双计，issue #67）。重试笔不套用本次
        调用的 source_override——其来源归属与本次调用无关。
        返回失败笔数（含重试仍失败）；成功 ≥1 笔发一次 trades_updated
        （contracts 去重，count=成功笔数）。

        参数：
            fills: list[FillRecord]，待持久化的成交批次
            source_override: str，调用方指定的成交来源覆盖值（仅作用于本批新成交）
        返回：
            int，仍失败（含重试仍失败）的笔数；0 表示全部落库成功
        """
        pending = self._pending
        self._pending = []
        saved_contracts: set[str] = set()
        saved_count = 0
        failures = 0
        # 重试笔沿用各自推导的 source，不套用本次调用的覆盖值（评审 N1）
        for fill, override in [(f, "") for f in pending] + [(f, source_override) for f in fills]:
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
                    source=override or trade_source_of(fill),
                    exchange_trade_id=fill.trade_id or None,
                )
                saved_contracts.add(fill.contract)
                saved_count += 1
            except Exception:
                failures += 1
                self._pending.append(fill)  # 留缓冲下轮重试（幂等键防双计，issue #67）
                logger.exception("成交落库失败 round=%s order=%s", round_id[:8], fill.order_id)
        if saved_contracts and self._notify_event is not None:
            self._notify_event(
                {
                    "type": "trades_updated",
                    "data": {"contracts": sorted(saved_contracts), "count": saved_count},
                }
            )
        return failures


async def _drain_safely(
    persister: FillPersister, drain_fills: Callable[[], list[FillRecord]], contract: str
) -> None:
    """即时 drain 的任务体：异常仅记日志（护住行情任务）。

    drain 已取空缓冲，失败笔进入 FillPersister 待重试队列下轮重试
    （幂等键防双计，issue #67）——落库/归属失败已在 persist_locked 内逐笔记日志。

    参数：
        persister: FillPersister，成交持久化协调器
        drain_fills: Callable[[], list[FillRecord]]，取空成交缓冲区的回调
        contract: str，合约标识
    返回：
        None，即时 drain 的任务体：异常仅记日志（护住行情任务）
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

    参数：
        persister: FillPersister，成交持久化协调器
        drain_fills: Callable[[], list[FillRecord]]，取空成交缓冲区的回调
        contract: str，合约标识
    返回：
        None，从同步回调（行情线程）调度一次即时 drain：create_task 后立即返回，不占关键路径
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
