"""存取层：决策/订单/成交/笔记/告警/唤醒/审计的读写。

金额与数量字段以 Decimal 传入、TEXT 落库；时间为 Unix 秒（float）。
业务层只与本模块交互，不直接写 SQL。
"""

from __future__ import annotations

import time
from decimal import Decimal

import aiosqlite

from src.memory.db import Database
from src.memory.models import (
    Alert,
    AuditRound,
    AuditToolCall,
    Decision,
    Note,
    OrderRecord,
    Trade,
)
from src.risk.models import DailyStats


def _now() -> float:
    return time.time()


def _order_from_row(row: aiosqlite.Row) -> OrderRecord:
    """行转模型：空串 price 还原为 None（市价单），TEXT 由 pydantic 还原为 Decimal。"""
    d = dict(row)
    d["price"] = Decimal(d["price"]) if d["price"] else None
    return OrderRecord(**d)


class Repo:
    """存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        return self._db.conn

    # ---------- decisions ----------

    async def save_decision(
        self,
        round_id: str,
        mode: str,
        strategy_version: str = "",
        wake_source: str = "",
        context_summary: str = "",
        llm_raw: str = "",
    ) -> Decision:
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO decisions(round_id,mode,strategy_version,wake_source,"
            "context_summary,llm_raw,created_at) VALUES(?,?,?,?,?,?,?)",
            (round_id, mode, strategy_version, wake_source, context_summary, llm_raw, ts),
        )
        await self._conn.commit()
        return Decision(
            id=cur.lastrowid or 0,
            round_id=round_id,
            mode=mode,
            strategy_version=strategy_version,
            wake_source=wake_source,
            context_summary=context_summary,
            llm_raw=llm_raw,
            created_at=ts,
        )

    async def list_decisions(self, limit: int = 50, offset: int = 0) -> list[Decision]:
        """分页查询，按 id 倒序（最新在前）。"""
        cur = await self._conn.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        return [Decision(**dict(r)) for r in await cur.fetchall()]

    # ---------- orders ----------

    async def save_order(
        self,
        order_id: str,
        round_id: str,
        mode: str,
        contract: str,
        side_size: Decimal,
        price: Decimal | None = None,
        tif: str = "",
        text: str = "",
        status: str = "open",
        finish_as: str = "",
        is_close: bool = False,
    ) -> OrderRecord:
        ts = _now()
        await self._conn.execute(
            "INSERT INTO orders(id,round_id,mode,contract,side_size,price,tif,text,"
            "status,finish_as,is_close,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order_id,
                round_id,
                mode,
                contract,
                str(side_size),
                "" if price is None else str(price),
                tif,
                text,
                status,
                finish_as,
                int(is_close),
                ts,
            ),
        )
        await self._conn.commit()
        return OrderRecord(
            id=order_id,
            round_id=round_id,
            mode=mode,
            contract=contract,
            side_size=side_size,
            price=price,
            tif=tif,
            text=text,
            status=status,
            finish_as=finish_as,
            created_at=ts,
        )

    async def update_order_after_amend(
        self,
        order_id: str,
        price: Decimal | None = None,
        side_size: Decimal | None = None,
    ) -> bool:
        """改单后同步 orders 行的 price/side_size（None 项不改）；返回是否有行被更新。

        供 amend_order 工具落库：已落库的挂单改单后更新原行而非插新行，
        避免 daily_stats 的 orders_today 把一次下单计数两次。
        """
        sets: list[str] = []
        params: list = []
        if price is not None:
            sets.append("price=?")
            params.append(str(price))
        if side_size is not None:
            sets.append("side_size=?")
            params.append(str(side_size))
        if not sets:
            return True  # 无字段需更新，视为已处理
        params.append(order_id)
        cur = await self._conn.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE id=?",
            params,  # 列名为代码常量，值走参数化
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def update_order_status(self, order_id: str, status: str, finish_as: str = "") -> None:
        await self._conn.execute(
            "UPDATE orders SET status=?, finish_as=? WHERE id=?", (status, finish_as, order_id)
        )
        await self._conn.commit()

    async def list_orders(self, round_id: str) -> list[OrderRecord]:
        cur = await self._conn.execute(
            "SELECT * FROM orders WHERE round_id=? ORDER BY created_at, id", (round_id,)
        )
        return [_order_from_row(r) for r in await cur.fetchall()]

    # ---------- trades ----------

    async def save_trade(
        self,
        round_id: str,
        mode: str,
        contract: str,
        size: Decimal,
        price: Decimal,
        fee: Decimal,
        pnl: Decimal,
        created_at: float | None = None,
    ) -> Trade:
        ts = created_at if created_at is not None else _now()
        cur = await self._conn.execute(
            "INSERT INTO trades(round_id,mode,contract,size,price,fee,pnl,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (round_id, mode, contract, str(size), str(price), str(fee), str(pnl), ts),
        )
        await self._conn.commit()
        return Trade(
            id=cur.lastrowid or 0,
            round_id=round_id,
            mode=mode,
            contract=contract,
            size=size,
            price=price,
            fee=fee,
            pnl=pnl,
            created_at=ts,
        )

    async def trades_between(
        self, start_ts: float, end_ts: float, mode: str | None = None
    ) -> list[Trade]:
        """按时间范围查询（[start, end)），供日统计使用；mode 非空时按模式过滤。"""
        sql = "SELECT * FROM trades WHERE created_at >= ? AND created_at < ?"
        params: list = [start_ts, end_ts]
        if mode is not None:
            sql += " AND mode=?"
            params.append(mode)
        cur = await self._conn.execute(sql + " ORDER BY id", params)
        return [Trade(**dict(r)) for r in await cur.fetchall()]

    async def list_trades(self, limit: int = 200, contract: str | None = None) -> list[Trade]:
        """最近 N 笔成交（最新在前）；LIMIT 在 SQL 层生效，contract 可选过滤。"""
        sql = "SELECT * FROM trades"
        params: list = []
        if contract is not None:
            sql += " WHERE contract=?"
            params.append(contract)
        params.append(limit)
        cur = await self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", params)
        return [Trade(**dict(r)) for r in await cur.fetchall()]

    async def daily_stats(self, mode: str, day_start_ts: float) -> DailyStats:
        """当日统计：realized_pnl=当日已实现盈亏合计；orders_today=当日开仓单数。

        均按 mode 过滤。orders_today 只计开仓单：is_close=1 的平仓/减仓单
        （close/reduce_only，落库时置位，见 tool_trading.place_order）在 SQL 层排除；
        side_size != '0' 为遗留兜底（is_close 置位前的历史平仓单 side_size 恒为 '0'，
        开仓单按构造恒非 0，不会误排）；
        pnl 以 TEXT 存 Decimal 字符串，逐行取出后在 Python 侧合计，避免
        SQLite SUM 把 TEXT 转 REAL 引入浮点误差。
        """
        cur = await self._conn.execute(
            "SELECT pnl FROM trades WHERE mode=? AND created_at >= ?", (mode, day_start_ts)
        )
        realized = sum((Decimal(r["pnl"]) for r in await cur.fetchall()), Decimal(0))
        cur = await self._conn.execute(
            "SELECT COUNT(*) FROM orders WHERE mode=? AND created_at >= ?"
            " AND is_close = 0 AND side_size != '0'",
            (mode, day_start_ts),
        )
        row = await cur.fetchone()
        return DailyStats(realized_pnl=realized, orders_today=int(row[0]) if row else 0)

    # ---------- notes ----------

    async def add_note(self, round_id: str, content: str) -> Note:
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO notes(round_id,content,created_at) VALUES(?,?,?)",
            (round_id, content, ts),
        )
        await self._conn.commit()
        return Note(id=cur.lastrowid or 0, round_id=round_id, content=content, created_at=ts)

    async def recent_notes(self, n: int = 10) -> list[Note]:
        """最近 N 条笔记，按时间正序返回（最旧在前，便于拼接上下文）。"""
        cur = await self._conn.execute("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (n,))
        notes = [Note(**dict(r)) for r in await cur.fetchall()]
        notes.reverse()
        return notes

    # ---------- alerts ----------

    async def add_alert(
        self, round_id: str, contract: str, direction: str, price: Decimal
    ) -> Alert:
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO alerts(round_id,contract,direction,price,active,created_at)"
            " VALUES(?,?,?,?,1,?)",
            (round_id, contract, direction, str(price), ts),
        )
        await self._conn.commit()
        return Alert(
            id=cur.lastrowid or 0,
            round_id=round_id,
            contract=contract,
            direction=direction,
            price=price,
            active=True,
            created_at=ts,
        )

    async def deactivate_alert(self, alert_id: int) -> None:
        await self._conn.execute("UPDATE alerts SET active=0 WHERE id=?", (alert_id,))
        await self._conn.commit()

    async def list_alerts(self, active_only: bool = True) -> list[Alert]:
        sql = "SELECT * FROM alerts" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        cur = await self._conn.execute(sql)
        return [Alert(**dict(r)) for r in await cur.fetchall()]

    # ---------- wakeup ----------

    async def record_wakeup(self, scheduled_at: float, source: str) -> None:
        await self._conn.execute(
            "INSERT INTO wakeup(scheduled_at,source,created_at) VALUES(?,?,?)",
            (scheduled_at, source, _now()),
        )
        await self._conn.commit()

    # ---------- audit ----------

    async def start_audit_round(
        self,
        round_id: str,
        mode: str,
        wake_source: str = "",
        prompt_md5: str = "",
        prompt_snapshot: str = "",
        context_snapshot: str = "",
        started_at: float | None = None,
    ) -> None:
        """一轮开始时写入审计主表（llm_raw/ended_at/error 由 finish_audit_round 补全）。"""
        await self._conn.execute(
            "INSERT INTO audit_rounds(round_id,mode,wake_source,prompt_md5,prompt_snapshot,"
            "context_snapshot,started_at) VALUES(?,?,?,?,?,?,?)",
            (
                round_id,
                mode,
                wake_source,
                prompt_md5,
                prompt_snapshot,
                context_snapshot,
                started_at if started_at is not None else _now(),
            ),
        )
        await self._conn.commit()

    async def finish_audit_round(
        self,
        round_id: str,
        llm_raw: str = "",
        ended_at: float | None = None,
        error: str = "",
    ) -> None:
        await self._conn.execute(
            "UPDATE audit_rounds SET llm_raw=?, ended_at=?, error=? WHERE round_id=?",
            (llm_raw, ended_at if ended_at is not None else _now(), error, round_id),
        )
        await self._conn.commit()

    async def update_audit_context(self, round_id: str, context_snapshot: str) -> None:
        """回填上下文快照：审计行先于上下文构建落库（保证失败轮有痕迹），构建完成后回填。"""
        await self._conn.execute(
            "UPDATE audit_rounds SET context_snapshot=? WHERE round_id=?",
            (context_snapshot, round_id),
        )
        await self._conn.commit()

    async def get_audit_round(self, round_id: str) -> AuditRound | None:
        cur = await self._conn.execute("SELECT * FROM audit_rounds WHERE round_id=?", (round_id,))
        row = await cur.fetchone()
        return AuditRound(**dict(row)) if row else None

    async def list_audit_rounds(self, round_ids: list[str]) -> dict[str, AuditRound]:
        """按 round_id 批量取审计轮（一次 IN 查询），结果以 round_id 为键。

        供列表端点使用，避免逐轮 get_audit_round 的 N+1 查询。
        """
        if not round_ids:
            return {}
        marks = ",".join(["?"] * len(round_ids))  # 占位符个数由参数决定，值仍走参数化
        cur = await self._conn.execute(
            f"SELECT * FROM audit_rounds WHERE round_id IN ({marks})", round_ids
        )
        return {a.round_id: a for a in (AuditRound(**dict(r)) for r in await cur.fetchall())}

    async def save_audit_tool_call(
        self,
        round_id: str,
        seq: int,
        tool: str,
        args_json: str = "{}",
        risk_verdict: str = "",
        risk_reason: str = "",
        result_json: str = "{}",
        duration_ms: int = 0,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO audit_tool_calls(round_id,seq,tool,args_json,risk_verdict,"
            "risk_reason,result_json,duration_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                round_id,
                seq,
                tool,
                args_json,
                risk_verdict,
                risk_reason,
                result_json,
                duration_ms,
                _now(),
            ),
        )
        await self._conn.commit()

    async def list_audit_tool_calls(self, round_id: str) -> list[AuditToolCall]:
        """按 round_id 查询，按 seq 正序（调用顺序）。"""
        cur = await self._conn.execute(
            "SELECT * FROM audit_tool_calls WHERE round_id=? ORDER BY seq", (round_id,)
        )
        return [AuditToolCall(**dict(r)) for r in await cur.fetchall()]
