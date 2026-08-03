"""交易所真实成交的存取方法集合（testnet/live 成交回报对账）。

与 Repo 分文件的原因：repo.py 已接近 500 行体量上限；本类包同一 Database 连接
（PlansRepo/ReviewRepo 先例）。所有写操作立即 commit。
"""

from __future__ import annotations

from decimal import Decimal

import aiosqlite

from src.memory.db import Database


class ExchangeFillsRepo:
    """trades 表交易所侧扩展：exchange_trade_id 幂等落库、归属补正、pnl 回填。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        return self._db.conn

    async def save_exchange_trade(
        self,
        *,
        exchange_trade_id: str,
        exchange_order_id: str,
        round_id: str,
        mode: str,
        contract: str,
        size: Decimal,
        price: Decimal,
        fee: Decimal,
        pnl: Decimal,
        source: str,
        created_at: float,
    ) -> int | None:
        """按交易所成交 id 幂等落库：新插入返回行 id，冲突（WS/REST 双通道重复）返回 None。"""
        # 部分唯一索引作冲突目标必须带 WHERE 谓词（SQLite 语法要求；本插入该列恒非空）
        cur = await self._conn.execute(
            "INSERT INTO trades(round_id,mode,contract,size,price,fee,pnl,source,created_at,"
            "exchange_trade_id,exchange_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(exchange_trade_id) WHERE exchange_trade_id IS NOT NULL DO NOTHING",
            (
                round_id,
                mode,
                contract,
                str(size),
                str(price),
                str(fee),
                str(pnl),
                source,
                created_at,
                exchange_trade_id,
                exchange_order_id,
            ),
        )
        await self._conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None

    async def latest_exchange_ts(self, mode: str) -> float | None:
        """最近一次交易所成交的 created_at（补漏水线）；无记录返回 None。按 mode 隔离：
        同一 db 切换 testnet/live 时两套环境成交互不影响水线。"""
        cur = await self._conn.execute(
            "SELECT MAX(created_at) FROM trades WHERE exchange_trade_id IS NOT NULL AND mode=?",
            (mode,),
        )
        row = await cur.fetchone()
        return row[0] if row is not None and row[0] is not None else None

    async def find_by_exchange_order_id(
        self, exchange_order_id: str, mode: str
    ) -> list[tuple[int, str, str, float]]:
        """按交易所订单 id 查本地成交行（乱序补正用）：(id, source, contract, created_at)
        列表。按 mode 隔离（testnet/live 订单 id 序列独立，数值可重叠）。"""
        cur = await self._conn.execute(
            "SELECT id, source, contract, created_at FROM trades "
            "WHERE exchange_order_id=? AND mode=?",
            (exchange_order_id, mode),
        )
        return [
            (r["id"], r["source"], r["contract"], r["created_at"]) for r in await cur.fetchall()
        ]

    async def update_attribution(self, trade_id: int, *, source: str, round_id: str) -> None:
        """补正来源与归属（订单/自动订单/强平信息晚到时）；调用方负责再发 trades_updated。"""
        await self._conn.execute(
            "UPDATE trades SET source=?, round_id=? WHERE id=?", (source, round_id, trade_id)
        )
        await self._conn.commit()

    async def update_pnl(self, trade_id: int, pnl: Decimal) -> None:
        """回填已实现盈亏（position_close 对账）；调用方负责再发 trades_updated。"""
        await self._conn.execute("UPDATE trades SET pnl=? WHERE id=?", (str(pnl), trade_id))
        await self._conn.commit()

    async def order_attribution(self, order_id: str, mode: str) -> tuple[str, str, bool] | None:
        """按交易所订单 id 查本地归属：(round_id, trade_source, is_close)；无本地订单
        返回 None。按 mode 隔离（两套环境订单 id 撞号时不会错配归属）。"""
        cur = await self._conn.execute(
            "SELECT round_id, trade_source, is_close FROM orders WHERE id=? AND mode=?",
            (order_id, mode),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return row["round_id"], row["trade_source"], bool(row["is_close"])
