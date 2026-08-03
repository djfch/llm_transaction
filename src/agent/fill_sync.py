"""交易所成交同步器（testnet/live）：私有推送 -> trades 表 -> trades_updated。

职责链：
1. handle_user_trade：解析 → exchange_trade_id 幂等落库 → 分类（本地订单 > 强平集合 >
   自动订单集合 > ''）→ 平仓类成交调度 pnl 回填 → 发 trades_updated
2. handle_auto_order / handle_liquidation：提取触发订单 id 入集合；对应成交已落库（乱序）
   → update_attribution 补正（本地订单来源不覆盖）+ 再发 trades_updated + 补做 pnl 回填
3. catch_up：启动/断线重连后以 latest_exchange_ts 为水线（首次以服务器时间 -600s）
   拉 my_trades，重叠 60s 窗口逐条走同一落库路径（ON CONFLICT 兜底，天然无双计）
4. run_safety_net：5 分钟一次低频幂等安全网。gatews 对会话中断连做内部静默重连
   （on_reconnected 不触发、私有推送不重放），秒级断线窗口内的成交靠本兜底找回
5. pnl 回填：延迟 ~1.5s 查 position_close，未中 ~5s 再查一次；命中 update_pnl + 再发
   trades_updated；再不中记 0 不再查（回填不阻塞落库）

Gate 字段校准：parse/extract 中的字段名与取值以 testnet 实测为准
（scripts/verify_private_feed.py 输出），未经实测不得扩展猜测字段。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from typing import Protocol

from src.audit.logger import get_logger
from src.gateway.base import ExchangeTrade, PositionCloseRecord
from src.memory.fills_repo import ExchangeFillsRepo

logger = get_logger(__name__)

_CLOSE_SOURCES = {"user_close", "llm_close", "tpsl_close", "liquidation"}
_FIRST_PNL_DELAY_S = 1.5  # 平仓后 position_close 首次查询延迟（实测定）
_RETRY_PNL_DELAY_S = 5.0  # 第二次查询间隔（再不中记 0）
_ORDER_SET_CAP = 2000  # 触发订单集合上限（防内存无界增长）
_SAFETY_NET_INTERVAL_S = 300  # 低频安全网间隔（秒）：幂等补漏，每次仅 1 个 REST 请求


class ExchangeRestSource(Protocol):
    """同步器依赖的 REST 能力（结构化接口，GateRestGateway 天然满足）。"""

    def list_my_trades(
        self, contract: str | None = None, limit: int = 100
    ) -> list[ExchangeTrade]: ...

    def list_position_close(
        self, contract: str, from_ts: float, to_ts: float
    ) -> list[PositionCloseRecord]: ...


def parse_user_trade(raw: dict) -> ExchangeTrade:
    """usertrades 推送条目解析（字段名与 REST MyFuturesTrade 一致，实测校准）。"""
    create_time_ms = raw.get("create_time_ms")
    create_time = float(create_time_ms) / 1000 if create_time_ms else float(raw["create_time"])
    return ExchangeTrade(
        id=str(raw["id"]),
        order_id=str(raw.get("order_id") or ""),
        contract=str(raw["contract"]),
        size=Decimal(str(raw["size"])),
        price=Decimal(str(raw["price"])),
        fee=Decimal(str(raw.get("fee") or 0)),
        role=str(raw.get("role") or ""),
        text=str(raw.get("text") or ""),
        create_time=create_time,
    )


def extract_triggered_order_id(raw: dict) -> str:
    """autoorders 推送提取已触发产生的成交订单 id；未触发或无 id 返回 ''。

    实测确认（testnet 2026-08）：open/finished(auto_cancelled, trade_id=0) 事件正确
    返回空，不会把被自动撤销的止盈止损误判为已触发；真正触发的事件本轮未观测到，
    trade_id 为触发后生成的订单 id 依据 Gate 官方文档。
    """
    status = str(raw.get("status") or "")
    if status and status not in ("finished", "triggered", "succeeded"):
        return ""  # 创建/撤销等非触发事件不入集合
    for key in ("order_id", "fired_order_id", "trade_id", "order"):
        value = raw.get(key)
        if value not in (None, "", 0):
            return str(value)
    return ""


def extract_liquidation_order_id(raw: dict) -> str:
    """liquidates 推送提取强平订单 id（与 usertrades 的 order_id 对应）。

    本轮 testnet 实测未触发强平，字段名按 Gate 官方文档假设（order_id 优先、id 兜底）；
    待真实强平事件观测后校准。
    """
    for key in ("order_id", "id"):
        value = raw.get(key)
        if value not in (None, "", 0):
            return str(value)
    return ""


class ExchangeFillSync:
    """成交同步器：依赖构造期注入，事件驱动，无自有线程/定时器。"""

    def __init__(
        self,
        fills: ExchangeFillsRepo,
        rest: ExchangeRestSource,
        mode: str,
        notify_event: Callable[[dict], None] | None = None,
    ) -> None:
        self._fills = fills
        self._rest = rest
        self._mode = mode
        self._notify_event = notify_event
        self._auto_order_ids: set[str] = set()
        self._liquidation_order_ids: set[str] = set()
        self._pnl_consumed: set[tuple[str, int]] = set()  # 已消费的 position_close 记录键
        self._tasks: set[asyncio.Task] = set()  # 回填任务强引用（PR2 审查模式）

    # ---------- 推送入口 ----------

    async def handle_user_trade(self, raw: dict) -> None:
        """成交推送：幂等落库 + 分类 + 平仓类调度 pnl 回填。"""
        trade = parse_user_trade(raw)
        await self._persist(trade)

    async def handle_auto_order(self, raw: dict) -> None:
        """自动订单推送：触发订单 id 入集合，乱序成交补正为 tpsl_close。"""
        order_id = extract_triggered_order_id(raw)
        if not order_id:
            return
        self._remember(self._auto_order_ids, order_id)
        await self._reattribute(order_id, "tpsl_close")

    async def handle_liquidation(self, raw: dict) -> None:
        """强平推送：强平订单 id 入集合，乱序成交补正为 liquidation。"""
        order_id = extract_liquidation_order_id(raw)
        if not order_id:
            return
        self._remember(self._liquidation_order_ids, order_id)
        await self._reattribute(order_id, "liquidation")

    # ---------- 补漏 ----------

    async def catch_up(self, *, first_lookback_s: float = 600.0, overlap_s: float = 60.0) -> None:
        """事件驱动补漏（启动/断线重连）：水线重叠窗内逐条走同一落库路径。"""
        try:
            trades = await asyncio.to_thread(self._rest.list_my_trades, None, 100)
        except Exception:
            logger.exception("成交补漏拉取失败（下次重连/启动再试）")
            return
        if not trades:
            return
        watermark = await self._fills.latest_exchange_ts(self._mode)
        if watermark is not None:
            since = watermark - overlap_s
        else:
            # 首启无水线：以拉取结果的最大 create_time 为服务器"现在"基准，
            # 不与本地时钟直接比较（本地快于服务器时本地时钟会漏掉窗口边界成交）
            since = max(t.create_time for t in trades) - first_lookback_s
        missed = [t for t in trades if t.create_time > since]
        for trade in reversed(missed):  # REST 倒序返回，按时间正序落库
            await self._persist(trade)
        if missed:
            logger.info("成交补漏完成：水线 %.0f，候选 %d 条", since, len(missed))

    async def run_safety_net(self) -> None:
        """低频安全网主循环：每 5 分钟幂等补漏一次（shutdown 时随任务取消终止）。

        为什么不靠 on_reconnected：gatews 对会话中断连（网络抖动/NAT 超时/keepalive
        收尸）做内部静默重连且不重放私有推送，run() 不返回，重连钩子不可见——
        秒级断线窗口内的成交（尤其强平/止盈止损这类无主成交）只能靠定时兜底找回。
        """
        while True:
            await asyncio.sleep(_SAFETY_NET_INTERVAL_S)
            await self.catch_up()

    # ---------- 内部 ----------

    async def _persist(self, trade: ExchangeTrade) -> None:
        """分类 + 幂等落库 + 事件 + 平仓类 pnl 回填调度（WS 与补漏共用）。"""
        source, round_id = await self._classify(trade)
        row_id = await self._fills.save_exchange_trade(
            exchange_trade_id=trade.id,
            exchange_order_id=trade.order_id,
            round_id=round_id,
            mode=self._mode,
            contract=trade.contract,
            size=trade.size,
            price=trade.price,
            fee=trade.fee,
            pnl=Decimal(0),
            source=source,
            created_at=trade.create_time,
        )
        if row_id is None:
            return  # WS/REST 双通道重复：幂等丢弃
        self._emit(trade.contract)
        if source in _CLOSE_SOURCES:
            self._schedule_pnl_backfill(row_id, trade.contract, trade.create_time, trade.id)

    async def _classify(self, trade: ExchangeTrade) -> tuple[str, str]:
        """返回 (source, round_id)：本地订单 > 强平集合 > 自动订单集合 > 未知。"""
        attr = await self._fills.order_attribution(trade.order_id, self._mode)
        if attr is not None:
            round_id, trade_source, is_close = attr
            if trade_source == "user_close":
                return "user_close", round_id
            return ("llm_close" if is_close else "llm_open"), round_id
        if trade.order_id in self._liquidation_order_ids:
            return "liquidation", ""
        if trade.order_id in self._auto_order_ids:
            return "tpsl_close", ""
        return "", ""

    async def _reattribute(self, order_id: str, source: str) -> None:
        """乱序补正：订单 id 命中的无来源成交行 UPDATE + 再发事件 + 补做 pnl 回填。"""
        rows = await self._fills.find_by_exchange_order_id(order_id, self._mode)
        for row_id, old_source, contract, created_at in rows:
            if old_source:
                continue  # 本地订单分类优先，不覆盖
            await self._fills.update_attribution(row_id, source=source, round_id="")
            self._emit(contract)
            self._schedule_pnl_backfill(row_id, contract, created_at, order_id)

    def _schedule_pnl_backfill(
        self, row_id: int, contract: str, fill_ts: float, label: str
    ) -> None:
        task = asyncio.get_running_loop().create_task(
            self._backfill_pnl(row_id, contract, fill_ts, label)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """取消未完成的 pnl 回填任务（shutdown 用）：db 关闭后任务醒来落库会报错噪音。"""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _backfill_pnl(self, row_id: int, contract: str, fill_ts: float, label: str) -> None:
        """两次机会查 position_close 回填 pnl；再不中记 0（日志），不阻塞落库。"""
        for delay in (_FIRST_PNL_DELAY_S, _RETRY_PNL_DELAY_S):
            await asyncio.sleep(delay)
            try:
                rows = await asyncio.to_thread(
                    self._rest.list_position_close, contract, fill_ts - 120, fill_ts + 5
                )
            except Exception:
                logger.exception("position_close 查询失败（%s）", contract)
                continue
            record = self._match_position_close(rows, contract, fill_ts)
            if record is not None:
                await self._fills.update_pnl(row_id, record.pnl)
                self._emit(contract)
                return
        logger.warning("pnl 回填两次未命中，记 0：%s %s", contract, label)

    def _match_position_close(
        self, rows: list[PositionCloseRecord], contract: str, fill_ts: float
    ) -> PositionCloseRecord | None:
        """取与成交时间最接近（fill_ts-5s ~ +2s 窗内）且未被其他成交消费的记录。

        两侧时间均为 Gate 服务器时钟，无本地偏移问题。下界 -5s 挡掉旧未消费记录
        错配（如网页端手动平仓留下的记录）；最近邻替代"最新一条"，防 2s 内同合约
        两笔平仓时 pnl 互换（粒度/下界以实测定，慢速强平可再放宽）。
        """
        candidates = [
            r
            for r in rows
            if fill_ts - 5 <= r.time <= fill_ts + 2
            and (contract, int(r.time)) not in self._pnl_consumed
        ]
        if not candidates:
            return None
        record = min(candidates, key=lambda r: abs(r.time - fill_ts))
        self._pnl_consumed.add((contract, int(record.time)))
        return record

    def _remember(self, order_set: set[str], order_id: str) -> None:
        if len(order_set) >= _ORDER_SET_CAP:
            order_set.clear()  # 超限清空：老单补正机会让给新单（补正窗口本就短暂）
        order_set.add(order_id)

    def _emit(self, contract: str) -> None:
        if self._notify_event is not None:
            self._notify_event(
                {"type": "trades_updated", "data": {"contracts": [contract], "count": 1}}
            )
