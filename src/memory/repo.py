"""存取层：决策/订单/成交/笔记/告警/唤醒/审计的读写。

金额与数量字段以 Decimal 传入、TEXT 落库；时间为 Unix 秒（float）。
业务层只与本模块交互，不直接写 SQL。
"""

from __future__ import annotations

import time
from decimal import Decimal

import aiosqlite

from src.memory.db import Database
from src.memory.indicator_config_repo import IndicatorConfigRepo
from src.memory.models import AuditRound, AuditToolCall, Decision, Note, OrderRecord, Trade
from src.memory.plans_repo import PlansRepo
from src.memory.research_repo import ResearchRepo
from src.memory.review_repo import ReviewRepo, query_page_rows, row_without_total
from src.risk.models import DailyStats


def _now() -> float:
    """返回当前 Unix 秒时间戳，供各表时间字段落库。

    参数：无

    返回：
        float：当前时间的 Unix 秒时间戳
    """
    return time.time()


def _order_from_row(row: aiosqlite.Row) -> OrderRecord:
    """行转模型：空串 price 还原为 None（市价单），TEXT 由 pydantic 还原为 Decimal。

    参数：
        row: aiosqlite.Row，数据库查询行

    返回：
        OrderRecord，将数据库行还原得到的订单记录
    """
    d = dict(row)
    d["price"] = Decimal(d["price"]) if d["price"] else None
    return OrderRecord(**d)


# 单条 CTE 同时取得列表与总数，避免两次 SELECT 被新写入穿插而产生不一致的分页响应。
_DECISIONS_PAGE_SQL = """
WITH total AS (SELECT COUNT(*) AS value FROM decisions),
page AS (SELECT * FROM decisions ORDER BY id DESC LIMIT ? OFFSET ?)
SELECT page.*, total.value AS total
FROM total LEFT JOIN page ON 1 = 1
ORDER BY page.id DESC
"""
_NOTES_PAGE_SQL = """
WITH total AS (SELECT COUNT(*) AS value FROM notes),
page AS (SELECT * FROM notes ORDER BY id DESC LIMIT ? OFFSET ?)
SELECT page.*, total.value AS total
FROM total LEFT JOIN page ON 1 = 1
ORDER BY page.id DESC
"""


class Repo:
    """存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        """组装存取层：绑定数据库句柄并挂载各子仓库。

        参数：
            db: Database，已打开的数据库句柄（与各子仓库共享同一连接）

        返回：
            None，就地初始化实例属性（review/plans/indicator_config/research 子仓库）
        """
        self._db = db
        # 子仓库：策略版本/复盘报告/复盘取数集中在 ReviewRepo（共享同一 Database 与连接），
        # 复盘相关调用走 repo.review.xxx（见 src/memory/review_repo.py）
        self.review = ReviewRepo(db)
        # 交易计划子仓库：trade_plans 读写走 repo.plans.xxx（见 src/memory/plans_repo.py）
        self.plans = PlansRepo(db)
        # 指标短名单版本子仓库（见 src/memory/indicator_config_repo.py）
        self.indicator_config = IndicatorConfigRepo(db)
        # 研报系统子仓库：timeline/reports/causal_links（见 src/memory/research_repo.py）
        self.research = ResearchRepo(db)

    @property
    def _conn(self) -> aiosqlite.Connection:
        """返回共享的 SQLite 异步连接。

        参数：无

        返回：
            aiosqlite.Connection：底层数据库连接（本仓库与子仓库共用）
        """
        return self._db.conn

    # ---------- decisions ----------

    async def save_decision(
        self,
        round_id: str,
        mode: str,
        strategy_version: str = "",
        strategy_md5: str = "",
        wake_source: str = "",
        context_summary: str = "",
        llm_raw: str = "",
    ) -> Decision:
        """落库一轮 LLM 决策记录并返回完整记录。

        参数：
            round_id: str，本轮决策的唯一标识
            mode: str，运行模式（paper/testnet/live）
            strategy_version: str，策略版本标识；省略时为空串
            strategy_md5: str，策略书原文 md5（关联策略版本）；省略时为空串
            wake_source: str，本轮唤醒来源（定时/价格触发等）；省略时为空串
            context_summary: str，本轮上下文摘要；省略时为空串
            llm_raw: str，LLM 原始输出文本；省略时为空串

        返回：
            Decision：含数据库自增 id 与落库时间戳的决策记录
        """
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO decisions(round_id,mode,strategy_version,strategy_md5,wake_source,"
            "context_summary,llm_raw,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                round_id,
                mode,
                strategy_version,
                strategy_md5,
                wake_source,
                context_summary,
                llm_raw,
                ts,
            ),
        )
        await self._conn.commit()
        return Decision(
            id=cur.lastrowid or 0,
            round_id=round_id,
            mode=mode,
            strategy_version=strategy_version,
            strategy_md5=strategy_md5,
            wake_source=wake_source,
            context_summary=context_summary,
            llm_raw=llm_raw,
            created_at=ts,
        )

    async def list_decisions(self, limit: int = 50, offset: int = 0) -> list[Decision]:
        """分页查询，按 id 倒序（最新在前）。

        参数：
            limit: int，返回记录数量上限
            offset: int，分页偏移量

        返回：
            list[Decision]，分页查询，按 id 倒序（最新在前）
        """
        cur = await self._conn.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        return [Decision(**dict(r)) for r in await cur.fetchall()]

    async def count_decisions(self) -> int:
        """返回全部决策轮总数，供监控 API 的分页器计算总页数。

        参数：无

        返回：
            int，返回全部决策轮总数，供监控 API 的分页器计算总页数
        """
        cur = await self._conn.execute("SELECT COUNT(*) AS total FROM decisions")
        row = await cur.fetchone()
        return int(row["total"] if row is not None else 0)

    async def list_decisions_page(self, limit: int, offset: int) -> tuple[list[Decision], int]:
        """以单条 SQL 快照返回决策页及总数，越界页仍保留准确总数。

        参数：
            limit: int，返回记录数量上限
            offset: int，分页偏移量

        返回：
            tuple[list[Decision], int]，以单条 SQL 快照返回决策页及总数，越界页仍保留准确总数
        """
        rows, total = await query_page_rows(self._conn, _DECISIONS_PAGE_SQL, limit, offset)
        return [Decision(**row_without_total(row)) for row in rows], total

    async def get_decision_by_round(self, round_id: str) -> Decision | None:
        """按 round_id 取唯一决策记录；不存在返回 None。

        参数：
            round_id: str，决策轮编号

        返回：
            Decision | None，按 round_id 取唯一决策记录；不存在返回 None
        """
        cur = await self._conn.execute("SELECT * FROM decisions WHERE round_id=?", (round_id,))
        row = await cur.fetchone()
        return Decision(**dict(row)) if row else None

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
        trade_source: str = "",
    ) -> OrderRecord:
        """落 orders 行。trade_source 非空时标记下单方（manual_close 传 user_close，
        成交对账据以分类；LLM 单保持 ''，由 is_close 推导 llm_open/llm_close）。

        参数：
            order_id: str，交易所订单编号
            round_id: str，决策轮编号
            mode: str，运行模式
            contract: str，合约标识
            side_size: Decimal，带方向的订单张数
            price: Decimal | None，订单价格，None 表示市价
            tif: str，订单有效期类型
            text: str，订单说明或 MCP 返回文本
            status: str，订单状态
            finish_as: str，订单结束状态
            is_close: bool，订单是否为平仓单
            trade_source: str，订单发起来源

        返回：
            OrderRecord，新写入并读回的订单记录

        """
        ts = _now()
        await self._conn.execute(
            "INSERT INTO orders(id,round_id,mode,contract,side_size,price,tif,text,"
            "status,finish_as,is_close,trade_source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                trade_source,
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

        参数：
            order_id: str，交易所订单编号
            price: Decimal | None，订单价格，None 表示市价
            side_size: Decimal | None，带方向的订单张数

        返回：
            bool，改单后同步 orders 行的 price/side_size（None 项不改）；返回是否有行被更新。  供 amend_order 工具落库：已落库的挂单改单后更新原行而非插新行， 避免 daily_stats 的 orders_today 把一次下单计数两次

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
        """更新订单状态与终态原因（如已成交/已撤销）。

        参数：
            order_id: str，交易所订单 id
            status: str，新状态（如 open/finished）
            finish_as: str，终态原因（如 filled/cancelled）；省略时置空

        返回：
            None，写入数据库
        """
        await self._conn.execute(
            "UPDATE orders SET status=?, finish_as=? WHERE id=?", (status, finish_as, order_id)
        )
        await self._conn.commit()

    async def list_orders(self, round_id: str) -> list[OrderRecord]:
        """按决策轮查询其下全部订单，按下单时间正序。

        参数：
            round_id: str，决策轮唯一标识

        返回：
            list[OrderRecord]：该轮订单列表（按 created_at、id 升序，最早在前）
        """
        cur = await self._conn.execute(
            "SELECT * FROM orders WHERE round_id=? ORDER BY created_at, id", (round_id,)
        )
        return [_order_from_row(r) for r in await cur.fetchall()]

    async def order_round_id(self, order_id: str) -> str | None:
        """按订单 id 查 round_id（成交归属继承用）；订单不存在返回 None。

        参数：
            order_id: str，交易所订单编号

        返回：
            str | None，按订单 id 查 round_id（成交归属继承用）；订单不存在返回 None
        """
        cur = await self._conn.execute("SELECT round_id FROM orders WHERE id=?", (order_id,))
        row = await cur.fetchone()
        return row[0] if row is not None else None

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
        source: str = "",
        created_at: float | None = None,
    ) -> Trade:
        """落库一笔成交。source 取值见 models.Trade（llm_open/llm_close/user_close/
        liquidation/tpsl_close/''），默认 '' 表示历史/未知。

        参数：
            round_id: str，决策轮编号
            mode: str，运行模式
            contract: str，合约标识
            size: Decimal，带方向的成交张数
            price: Decimal，订单价格，None 表示市价
            fee: Decimal，成交手续费
            pnl: Decimal，成交已实现盈亏
            source: str，成交来源
            created_at: float | None，可选成交时间戳

        返回：
            Trade，新写入并读回的成交记录

        """
        ts = created_at if created_at is not None else _now()
        cur = await self._conn.execute(
            "INSERT INTO trades(round_id,mode,contract,size,price,fee,pnl,source,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (round_id, mode, contract, str(size), str(price), str(fee), str(pnl), source, ts),
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
            source=source,
            created_at=ts,
        )

    async def trades_between(
        self, start_ts: float, end_ts: float, mode: str | None = None
    ) -> list[Trade]:
        """按时间范围查询（[start, end)），供日统计使用；mode 非空时按模式过滤。

        参数：
            start_ts: float，查询区间起始时间戳
            end_ts: float，查询区间结束时间戳
            mode: str | None，运行模式

        返回：
            list[Trade]，按时间范围查询（[start, end)），供日统计使用；mode 非空时按模式过滤
        """
        sql = "SELECT * FROM trades WHERE created_at >= ? AND created_at < ?"
        params: list = [start_ts, end_ts]
        if mode is not None:
            sql += " AND mode=?"
            params.append(mode)
        cur = await self._conn.execute(sql + " ORDER BY id", params)
        return [Trade(**dict(r)) for r in await cur.fetchall()]

    async def list_trades(
        self, limit: int = 50, offset: int = 0, contract: str | None = None
    ) -> list[Trade]:
        """成交分页查询（最新在前）；LIMIT/OFFSET 与 contract 过滤均在 SQL 层生效。

        参数：
            limit: int，返回记录数量上限
            offset: int，分页偏移量
            contract: str | None，合约标识

        返回：
            list[Trade]，成交分页查询（最新在前）；LIMIT/OFFSET 与 contract 过滤均在 SQL 层生效
        """
        sql = "SELECT * FROM trades"
        params: list = []
        if contract is not None:
            sql += " WHERE contract=?"
            params.append(contract)
        params += [limit, offset]
        cur = await self._conn.execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", params)
        return [Trade(**dict(r)) for r in await cur.fetchall()]

    async def count_trades(self, contract: str | None = None) -> int:
        """成交总数（contract 可选过滤）；与 list_trades 同过滤口径，供分页取总页数。

        参数：
            contract: str | None，合约标识

        返回：
            int，成交总数（contract 可选过滤）；与 list_trades 同过滤口径，供分页取总页数
        """
        sql = "SELECT COUNT(*) FROM trades"
        params: list = []
        if contract is not None:
            sql += " WHERE contract=?"
            params.append(contract)
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def daily_stats(self, mode: str, day_start_ts: float) -> DailyStats:
        """当日统计：realized_pnl=当日已实现盈亏合计；orders_today=当日开仓单数。

        均按 mode 过滤。orders_today 只计开仓单：is_close=1 的平仓/减仓单
        （close/reduce_only，落库时置位，见 tool_trading.place_order）在 SQL 层排除；
        side_size != '0' 为遗留兜底（is_close 置位前的历史平仓单 side_size 恒为 '0'，
        开仓单按构造恒非 0，不会误排）；
        pnl 以 TEXT 存 Decimal 字符串，逐行取出后在 Python 侧合计，避免
        SQLite SUM 把 TEXT 转 REAL 引入浮点误差。

        参数：
            mode: str，运行模式
            day_start_ts: float，当日零点 Unix 时间戳

        返回：
            DailyStats，当日统计：realized_pnl=当日已实现盈亏合计；orders_today=当日开仓单数。  均按 mode 过滤。orders_today 只计开仓单：is_close=1 的平仓/减仓单 （close/reduce_only，落库时置位，见 tool_trading.place_order）在 SQL 层排除； side_size != '0' 为遗留兜底（is_close 置位前的历史平仓单 side_size 恒为 '0'， 开仓单按构造恒非 0，不会误排）； pnl 以 TEXT 存 Decimal 字符串，逐行取出后在 Python 侧合计，避免 SQLite SUM 把 TEXT 转 REAL 引入浮点误差

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
        """落库一条 Agent 自述笔记并返回完整记录。

        参数：
            round_id: str，产生笔记的决策轮唯一标识
            content: str，笔记正文（跨轮传递上下文用）

        返回：
            Note：含数据库自增 id 与落库时间戳的笔记记录
        """
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO notes(round_id,content,created_at) VALUES(?,?,?)",
            (round_id, content, ts),
        )
        await self._conn.commit()
        return Note(id=cur.lastrowid or 0, round_id=round_id, content=content, created_at=ts)

    async def recent_notes(self, n: int = 10) -> list[Note]:
        """最近 N 条笔记，按时间正序返回（最旧在前，便于拼接上下文）。

        参数：
            n: int，读取笔记数量

        返回：
            list[Note]，最近 N 条笔记，按时间正序返回（最旧在前，便于拼接上下文）
        """
        cur = await self._conn.execute("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (n,))
        notes = [Note(**dict(r)) for r in await cur.fetchall()]
        notes.reverse()
        return notes

    async def list_notes(self, limit: int = 50, offset: int = 0) -> list[Note]:
        """分页读取笔记，按 id 倒序返回，确保监控界面最新内容位于第一页。

        参数：
            limit: int，返回记录数量上限
            offset: int，分页偏移量

        返回：
            list[Note]，分页读取笔记，按 id 倒序返回，确保监控界面最新内容位于第一页
        """
        cur = await self._conn.execute(
            "SELECT * FROM notes ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        return [Note(**dict(r)) for r in await cur.fetchall()]

    async def count_notes(self) -> int:
        """返回全部笔记总数，供监控 API 的分页器计算总页数。

        参数：无

        返回：
            int，返回全部笔记总数，供监控 API 的分页器计算总页数
        """
        cur = await self._conn.execute("SELECT COUNT(*) AS total FROM notes")
        row = await cur.fetchone()
        return int(row["total"] if row is not None else 0)

    async def list_notes_page(self, limit: int, offset: int) -> tuple[list[Note], int]:
        """以单条 SQL 快照返回最新优先的笔记页及总数，避免分页状态撕裂。

        参数：
            limit: int，返回记录数量上限
            offset: int，分页偏移量

        返回：
            tuple[list[Note], int]，以单条 SQL 快照返回最新优先的笔记页及总数，避免分页状态撕裂
        """
        rows, total = await query_page_rows(self._conn, _NOTES_PAGE_SQL, limit, offset)
        return [Note(**row_without_total(row)) for row in rows], total

    async def list_notes_by_rounds(self, round_ids: list[str]) -> dict[str, Note]:
        """按 round_id 批量取归属笔记（一次 IN 查询），每轮只留最新一条；供时间线端点按当前页 join 引文。

        参数：
            round_ids: list[str]，需要批量查询的决策轮编号

        返回：
            dict[str, Note]，按 round_id 批量取归属笔记（一次 IN 查询），每轮只留最新一条；供时间线端点按当前页 join 引文
        """
        if not round_ids:
            return {}
        marks = ",".join(["?"] * len(round_ids))  # 占位符个数由参数决定，值仍走参数化
        cur = await self._conn.execute(
            f"SELECT * FROM notes WHERE round_id IN ({marks}) ORDER BY id DESC", round_ids
        )
        result: dict[str, Note] = {}
        for row in await cur.fetchall():
            result.setdefault(row["round_id"], Note(**dict(row)))  # id DESC 首个命中即该轮最新
        return result

    # ---------- wakeup ----------

    async def record_wakeup(self, scheduled_at: float, source: str) -> None:
        """落库一次唤醒调度记录（计划时刻与触发来源）。

        参数：
            scheduled_at: float，计划唤醒时刻（Unix 秒）
            source: str，唤醒来源（如定时调度/价格触发）

        返回：
            None，写入数据库
        """
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
        strategy_md5: str = "",
        started_at: float | None = None,
    ) -> None:
        """一轮开始时写入审计主表（llm_raw/ended_at/error 由 finish_audit_round 补全）。

        参数：
            round_id: str，决策轮编号
            mode: str，运行模式
            wake_source: str，唤醒来源
            prompt_md5: str，提示词内容摘要
            prompt_snapshot: str，提示词全文快照
            context_snapshot: str，本轮上下文快照全文
            strategy_md5: str，策略正文摘要
            started_at: float | None，可选轮次开始时间戳

        返回：
            None，一轮开始时写入审计主表（llm_raw/ended_at/error 由 finish_audit_round 补全）
        """
        await self._conn.execute(
            "INSERT INTO audit_rounds(round_id,mode,wake_source,prompt_md5,strategy_md5,"
            "prompt_snapshot,context_snapshot,started_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                round_id,
                mode,
                wake_source,
                prompt_md5,
                strategy_md5,
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
        """一轮结束时回填审计主表的 LLM 原始输出、结束时刻与错误信息。

        参数：
            round_id: str，决策轮唯一标识（定位待回填的审计行）
            llm_raw: str，LLM 原始输出文本；省略时为空串
            ended_at: float，结束时刻（Unix 秒）；省略时取当前时间
            error: str，错误信息（无错误时为空串）；省略时为空串

        返回：
            None，写入数据库
        """
        await self._conn.execute(
            "UPDATE audit_rounds SET llm_raw=?, ended_at=?, error=? WHERE round_id=?",
            (llm_raw, ended_at if ended_at is not None else _now(), error, round_id),
        )
        await self._conn.commit()

    async def update_audit_context(self, round_id: str, context_snapshot: str) -> None:
        """回填上下文快照：审计行先于上下文构建落库（保证失败轮有痕迹），构建完成后回填。

        参数：
            round_id: str，决策轮编号
            context_snapshot: str，本轮上下文快照全文

        返回：
            None，回填上下文快照：审计行先于上下文构建落库（保证失败轮有痕迹），构建完成后回填
        """
        sql = "UPDATE audit_rounds SET context_snapshot=? WHERE round_id=?"
        await self._conn.execute(sql, (context_snapshot, round_id))
        await self._conn.commit()

    async def get_audit_round(self, round_id: str) -> AuditRound | None:
        """按决策轮 id 查询审计轮完整记录。

        参数：
            round_id: str，决策轮唯一标识

        返回：
            AuditRound | None：审计轮记录；不存在时返回 None
        """
        cur = await self._conn.execute("SELECT * FROM audit_rounds WHERE round_id=?", (round_id,))
        row = await cur.fetchone()
        return AuditRound(**dict(row)) if row else None

    async def list_audit_rounds(self, round_ids: list[str]) -> dict[str, AuditRound]:
        """按 round_id 批量取审计轮（一次 IN 查询避免列表端点 N+1），结果以 round_id 为键。

        参数：
            round_ids: list[str]，需要批量查询的决策轮编号

        返回：
            dict[str, AuditRound]，按 round_id 批量取审计轮（一次 IN 查询避免列表端点 N+1），结果以 round_id 为键
        """
        if not round_ids:
            return {}
        marks = ",".join(["?"] * len(round_ids))  # 占位符个数由参数决定，值仍走参数化
        cur = await self._conn.execute(
            f"SELECT * FROM audit_rounds WHERE round_id IN ({marks})", round_ids
        )
        return {a.round_id: a for a in (AuditRound(**dict(r)) for r in await cur.fetchall())}

    async def latest_audit_round(
        self, mode: str, exclude_wake_sources: tuple[str, ...] = ()
    ) -> AuditRound | None:
        """按模式取最近一轮审计（started_at 最新并列取后插入者）；exclude_wake_sources 非空时排除该来源（trader live 视图排除复盘/研报轮），默认空元组不加过滤。

        参数：
            mode: str，运行模式
            exclude_wake_sources: tuple[str, ...]，需要排除的唤醒来源

        返回：
            AuditRound | None，按模式取最近一轮审计（started_at 最新并列取后插入者）；exclude_wake_sources 非空时排除该来源（trader live 视图排除复盘/研报轮），默认空元组不加过滤
        """
        sql, params = "SELECT * FROM audit_rounds WHERE mode=?", [mode]
        if exclude_wake_sources:
            # 占位符个数由参数决定，值仍走参数化
            marks = ",".join(["?"] * len(exclude_wake_sources))
            sql += f" AND wake_source NOT IN ({marks})"
            params.extend(exclude_wake_sources)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT 1"
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        return AuditRound(**dict(row)) if row else None

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
        """落库一轮中的一次工具调用审计记录（含风控判定与耗时）。

        参数：
            round_id: str，决策轮唯一标识
            seq: int，本轮内的调用序号（标识调用顺序）
            tool: str，工具名
            args_json: str，调用参数 JSON；省略时为 '{}'
            risk_verdict: str，风控判定结果；省略时为空串
            risk_reason: str，风控判定理由；省略时为空串
            result_json: str，调用结果 JSON；省略时为 '{}'
            duration_ms: int，调用耗时毫秒数；省略时为 0

        返回：
            None，写入数据库
        """
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
        """按 round_id 查询，按 seq 正序（调用顺序）。

        参数：
            round_id: str，决策轮编号

        返回：
            list[AuditToolCall]，按 round_id 查询，按 seq 正序（调用顺序）
        """
        cur = await self._conn.execute(
            "SELECT * FROM audit_tool_calls WHERE round_id=? ORDER BY seq", (round_id,)
        )
        return [AuditToolCall(**dict(r)) for r in await cur.fetchall()]
