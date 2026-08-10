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
        """初始化交易所成交仓库，绑定共享数据库连接。

        参数：
            db: Database，数据库连接封装（与 PlansRepo/ReviewRepo 共享同一连接）

        返回：
            None，仅将连接引用保存到实例属性
        """
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        """暴露底层 aiosqlite 连接，供本类各方法执行 SQL。

        参数：无

        返回：
            aiosqlite.Connection：当前数据库连接对象
        """
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
        """按交易所成交编号幂等保存真实成交，避免 WebSocket 与 REST 重复落库。

        参数：
            exchange_trade_id: str，交易所唯一成交编号
            exchange_order_id: str，成交所属的交易所订单编号
            round_id: str，成交归属的决策轮编号
            mode: str，testnet 或 live 运行模式
            contract: str，成交合约名称
            size: Decimal，带方向的成交张数
            price: Decimal，成交价格
            fee: Decimal，成交手续费
            pnl: Decimal，本次成交已实现盈亏
            source: str，成交来源分类
            created_at: float，交易所成交时间戳

        返回：
            int | None，新插入的本地成交行编号；重复成交返回 None
        """
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
        同一 db 切换 testnet/live 时两套环境成交互不影响水线。

        参数：
            mode: str，限定补漏水线的运行模式

        返回：
            float | None，该模式最近一次真实成交时间戳；无记录时返回 None
        """
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
        列表。按 mode 隔离（testnet/live 订单 id 序列独立，数值可重叠）。

        参数：
            exchange_order_id: str，待匹配的交易所订单编号
            mode: str，限定查询的运行模式

        返回：
            list[tuple[int, str, str, float]]，本地成交编号、来源、合约与成交时间列表
        """
        cur = await self._conn.execute(
            "SELECT id, source, contract, created_at FROM trades "
            "WHERE exchange_order_id=? AND mode=?",
            (exchange_order_id, mode),
        )
        return [
            (r["id"], r["source"], r["contract"], r["created_at"]) for r in await cur.fetchall()
        ]

    async def update_attribution(self, trade_id: int, *, source: str, round_id: str) -> None:
        """在订单、自动订单或强平信息晚到时补正成交来源与决策轮归属。

        参数：
            trade_id: int，待补正的本地成交行编号
            source: str，修正后的成交来源
            round_id: str，修正后的决策轮编号

        返回：
            None，更新成交行并立即提交数据库事务；调用方负责广播成交更新
        """
        await self._conn.execute(
            "UPDATE trades SET source=?, round_id=? WHERE id=?", (source, round_id, trade_id)
        )
        await self._conn.commit()

    async def update_pnl(self, trade_id: int, pnl: Decimal) -> None:
        """在持仓关闭对账后回填指定成交的已实现盈亏。

        参数：
            trade_id: int，待回填的本地成交行编号
            pnl: Decimal，对账确认的已实现盈亏

        返回：
            None，更新成交行并立即提交数据库事务；调用方负责广播成交更新
        """
        await self._conn.execute("UPDATE trades SET pnl=? WHERE id=?", (str(pnl), trade_id))
        await self._conn.commit()

    async def order_attribution(self, order_id: str, mode: str) -> tuple[str, str, bool] | None:
        """按交易所订单 id 查本地归属：(round_id, trade_source, is_close)；无本地订单
        返回 None。按 mode 隔离（两套环境订单 id 撞号时不会错配归属）。

        参数：
            order_id: str，交易所订单编号
            mode: str，限定查询的运行模式

        返回：
            tuple[str, str, bool] | None，决策轮编号、交易来源与是否平仓；无记录时返回 None
        """
        cur = await self._conn.execute(
            "SELECT round_id, trade_source, is_close FROM orders WHERE id=? AND mode=?",
            (order_id, mode),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return row["round_id"], row["trade_source"], bool(row["is_close"])
