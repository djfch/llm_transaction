"""复盘子仓库：策略书版本、复盘报告与复盘统计取数三组存取方法。

子仓库模式：ReviewRepo 与 Repo 共享同一 Database（同一连接、同一事务语义），由
Repo.__init__ 挂载为 repo.review，复盘相关调用一律走 repo.review.xxx；
本模块只依赖 db/models（不反向 import Repo），无循环依赖。
分页 CTE 助手为模块级函数，供本类与 Repo 的分页方法共用。
"""

from __future__ import annotations

import time

import aiosqlite

from src.memory.db import Database
from src.memory.models import AuditRound, Decision, ReviewReport, StrategyVersion, Trade
from src.memory.research_review_repo import _insert_review


def _now() -> float:
    """取当前 Unix 时间戳（秒），作为记录的 created_at 落库时间。

    参数：无

    返回：
        float：当前 Unix 时间戳（秒）
    """
    return time.time()


# 单条 CTE 同时取得列表与总数，避免两次 SELECT 被新写入穿插而产生不一致的分页响应。
_REVIEW_REPORTS_PAGE_SQL = """
WITH total AS (SELECT COUNT(*) AS value FROM review_reports),
page AS (SELECT * FROM review_reports ORDER BY id DESC LIMIT ? OFFSET ?)
SELECT page.*, total.value AS total
FROM total LEFT JOIN page ON 1 = 1
ORDER BY page.id DESC
"""


async def query_page_rows(
    conn: aiosqlite.Connection, sql: str, limit: int, offset: int
) -> tuple[list[aiosqlite.Row], int]:
    """执行固定分页 CTE；空页的 LEFT JOIN 占位行仅用于携带 total(总数)。

    参数：
        conn: aiosqlite.Connection，SQLite 异步连接
        sql: str，固定参数化分页 SQL
        limit: int，最多读取或返回的记录数量
        offset: int，分页起始偏移量

    返回：
        tuple[list[aiosqlite.Row], int]：执行固定分页 CTE；空页的 LEFT JOIN 占位行仅用于携带 total(总数)
    """
    cur = await conn.execute(sql, (limit, offset))
    raw_rows = await cur.fetchall()
    total = int(raw_rows[0]["total"]) if raw_rows else 0
    return [row for row in raw_rows if row["id"] is not None], total


def row_without_total(row: aiosqlite.Row) -> dict[str, object]:
    """移除分页 CTE 附带的 total(总数) 列，保留领域模型的原始字段。

    参数：
        row: aiosqlite.Row，SQLite 查询结果行

    返回：
        dict[str, object]：移除分页 CTE 附带的 total(总数) 列，保留领域模型的原始字段
    """
    data = dict(row)
    data.pop("total", None)
    return data


class ReviewRepo:
    """策略版本/复盘报告/复盘取数的存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        """绑定共享数据库句柄（与 Repo 共用同一连接与事务语义）。

        参数：
            db: Database，已打开的数据库句柄，由 Repo 挂载时传入

        返回：
            None，仅保存数据库引用，不触发任何 IO
        """
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        """取共享的 aiosqlite 连接，供本类各存取方法执行 SQL。

        参数：无

        返回：
            aiosqlite.Connection：与 Repo 共享的同一数据库连接
        """
        return self._db.conn

    # ---------- strategy_versions（策略书版本） ----------

    async def save_strategy_version(
        self,
        content: str,
        md5: str,
        created_by: str,
        reason: str,
        report_id: int | None = None,
        status: str = "applied",
    ) -> StrategyVersion:
        """落库一个策略书版本（content 为完整原文，md5 为关联键）。

        参数：
            content: str，策略书完整正文
            md5: str，策略书正文摘要
            created_by: str，版本创建来源
            reason: str，操作原因或失败说明
            report_id: int | None，研报记录编号
            status: str，版本状态：applied 已生效 / draft 草稿（报告成功才生效，
                issue #62/#73）/ discarded 已废弃

        返回：
            StrategyVersion：落库一个策略书版本（content 为完整原文，md5 为关联键）
        """
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO strategy_versions(content,md5,created_by,reason,report_id,created_at,status)"
            " VALUES(?,?,?,?,?,?,?)",
            (content, md5, created_by, reason, report_id, ts, status),
        )
        await self._conn.commit()
        return StrategyVersion(
            id=cur.lastrowid or 0,
            content=content,
            md5=md5,
            created_by=created_by,
            reason=reason,
            report_id=report_id,
            created_at=ts,
            status=status,
        )

    async def list_strategy_versions(self) -> list[StrategyVersion]:
        """全部版本，按 id 倒序（最新在前）。

        参数：
            无

        返回：
            list[StrategyVersion]：全部版本，按 id 倒序（最新在前）
        """
        cur = await self._conn.execute("SELECT * FROM strategy_versions ORDER BY id DESC")
        return [StrategyVersion(**dict(r)) for r in await cur.fetchall()]

    async def get_strategy_version(self, version_id: int) -> StrategyVersion | None:
        """按 id 读取单个策略书版本。

        参数：
            version_id: int，策略版本 id

        返回：
            StrategyVersion | None：命中的策略版本；id 不存在时返回 None
        """
        cur = await self._conn.execute("SELECT * FROM strategy_versions WHERE id=?", (version_id,))
        row = await cur.fetchone()
        return StrategyVersion(**dict(row)) if row else None

    async def set_version_status(self, version_id: int, status: str) -> None:
        """更新策略书版本状态（draft→applied 生效、draft→discarded 废弃，issue #62/#73）。

        参数：
            version_id: int，策略书版本编号
            status: str，目标状态（applied/discarded）

        返回：
            None，就地更新数据库并提交
        """
        await self._conn.execute(
            "UPDATE strategy_versions SET status=? WHERE id=?", (status, version_id)
        )
        await self._conn.commit()

    async def latest_applied_strategy_version(self) -> StrategyVersion | None:
        """读取最新一个 applied 状态的策略书版本；无则返回 None。

        供启动对账：文件 md5 与最新生效版本不一致时以数据库为准恢复文件。

        参数：无

        返回：
            StrategyVersion | None：最新生效版本；版本表无 applied 记录时 None
        """
        cur = await self._conn.execute(
            "SELECT * FROM strategy_versions WHERE status='applied' ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return StrategyVersion(**dict(row)) if row is not None else None

    async def discard_all_drafts(self) -> int:
        """把全部 draft 状态的策略版本置为 discarded（启动时清理孤儿草稿，issue #100）。

        启动时不存在进行中的复盘轮——此刻仍为 draft 的版本必然是上轮异常残留，
        留在历史里可能被人工回滚激活为过期内容。

        参数：无

        返回：
            int：废弃的草稿数量
        """
        cur = await self._conn.execute(
            "UPDATE strategy_versions SET status='discarded' WHERE status='draft'"
        )
        await self._conn.commit()
        return cur.rowcount

    async def attach_report_to_version(self, version_id: int, report_id: int) -> None:
        """回填触发该版本的复盘报告 id（版本先落库、报告后落库的反向关联）。

        参数：
            version_id: int，策略版本编号
            report_id: int，研报记录编号

        返回：
            None：回填触发该版本的复盘报告 id（版本先落库、报告后落库的反向关联）
        """
        await self._conn.execute(
            "UPDATE strategy_versions SET report_id=? WHERE id=?", (report_id, version_id)
        )
        await self._conn.commit()

    # ---------- review_reports（复盘报告） ----------

    async def save_review_report(
        self,
        period_start: float,
        period_end: float,
        stats_json: str,
        report_md: str,
        strategy_action: str,
        new_version_id: int | None = None,
        error: str = "",
        round_id: str = "",
    ) -> ReviewReport:
        """落库一份复盘报告；error 非空表示该次复盘失败（只留错误记录）。

        round_id 为产生本报告的审计轮 id；省略默认 ''（无关联）。
        实现上委托 save_review_bundle（无研报复盘记录），保持单一写入路径。

        参数：
            period_start: float，复盘区间起点时间戳
            period_end: float，复盘区间终点时间戳
            stats_json: str，复盘统计 JSON 文本
            report_md: str，复盘报告 Markdown 正文
            strategy_action: str，策略书处理动作
            new_version_id: int | None，复盘生成的新策略版本编号
            error: str，需要记录的错误文本
            round_id: str，关联的审计轮次编号

        返回：
            ReviewReport：落库一份复盘报告；error 非空表示该次复盘失败（只留错误记录）
        """
        return await self.save_review_bundle(
            period_start,
            period_end,
            stats_json,
            report_md,
            strategy_action,
            new_version_id=new_version_id,
            error=error,
            round_id=round_id,
        )

    async def save_review_bundle(
        self,
        period_start: float,
        period_end: float,
        stats_json: str,
        report_md: str,
        strategy_action: str,
        new_version_id: int | None = None,
        error: str = "",
        round_id: str = "",
        research_reviews: list[dict] | None = None,
    ) -> ReviewReport:
        """单事务落库一份复盘报告及其全部研报复盘记录；任一步失败整体回滚。

        报告与研报复盘是同一逻辑提交单元（issue #113）：不允许出现"报告已落库
        而批改丢失"的中间态；研报复盘为空时等价于原单报告落库。失败复盘
        （error 非空）不应携带研报复盘记录。
        事务在独立连接上以 BEGIN IMMEDIATE 开始（与 research 侧 save_report_bundle
        同范式）：共享连接在 await 间隙被其他协程 commit 会把本批部分行提前提交，
        届时 rollback 只能回滚最后一段，破坏整批原子性。

        参数：
            period_start: float，复盘区间起点时间戳
            period_end: float，复盘区间终点时间戳
            stats_json: str，复盘统计 JSON 文本
            report_md: str，复盘报告 Markdown 正文（成功路径含代码计算的统计段）
            strategy_action: str，策略书处理动作
            new_version_id: int | None，复盘生成的新策略版本编号
            error: str，需要记录的错误文本
            round_id: str，关联的审计轮次编号
            research_reviews: list[dict] | None，研报复盘草稿列表；元素键与
                research_review_repo._insert_review 的关键字参数一致（不含
                review_report_id/created_at，由本方法统一回填）

        返回：
            ReviewReport：已提交的复盘报告

        异常：
            Exception：INSERT 失败或唯一约束冲突（如同目标重复批改）时
                回滚整批（报告与批改都不残留）并原样上抛
        """
        ts = _now()
        conn = await aiosqlite.connect(str(self._db.path))
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "INSERT INTO review_reports(period_start,period_end,stats_json,report_md,"
                "strategy_action,new_version_id,error,round_id,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    period_start,
                    period_end,
                    stats_json,
                    report_md,
                    strategy_action,
                    new_version_id,
                    error,
                    round_id,
                    ts,
                ),
            )
            report_id = cur.lastrowid or 0
            for item in research_reviews or []:
                await _insert_review(conn, review_report_id=report_id, created_at=ts, **item)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()
        return ReviewReport(
            id=report_id,
            period_start=period_start,
            period_end=period_end,
            stats_json=stats_json,
            report_md=report_md,
            strategy_action=strategy_action,
            new_version_id=new_version_id,
            error=error,
            round_id=round_id,
            created_at=ts,
        )

    async def find_report_by_round_id(self, round_id: str) -> ReviewReport | None:
        """按审计轮次编号反查复盘报告（含失败记录），同轮多份取最新一份；查无返回 None。

        供取消收尾使用：取消可能掐在「成功 INSERT/COMMIT 已执行、保存函数未返回」的
        窗口，调用方内存中的 report_id 仍为 None——此时不信内存布尔位，按 round_id
        反查数据库确认成功报告是否其实已提交，避免同轮成功/失败双写。

        参数：
            round_id: str，审计轮次编号

        返回：
            ReviewReport | None：该轮最新一份复盘报告；查无此轮记录返回 None
        """
        cur = await self._conn.execute(
            "SELECT * FROM review_reports WHERE round_id=? ORDER BY id DESC LIMIT 1",
            (round_id,),
        )
        row = await cur.fetchone()
        return ReviewReport(**dict(row)) if row else None

    async def list_review_reports_page(
        self, limit: int, offset: int
    ) -> tuple[list[ReviewReport], int]:
        """以单条 SQL 快照返回报告页及总数，越界页仍保留准确总数。

        参数：
            limit: int，最多读取或返回的记录数量
            offset: int，分页起始偏移量

        返回：
            tuple[list[ReviewReport], int]：以单条 SQL 快照返回报告页及总数，越界页仍保留准确总数
        """
        rows, total = await query_page_rows(self._conn, _REVIEW_REPORTS_PAGE_SQL, limit, offset)
        return [ReviewReport(**row_without_total(row)) for row in rows], total

    async def get_review_report(self, report_id: int) -> ReviewReport | None:
        """按 id 读取单份复盘报告。

        参数：
            report_id: int，复盘报告 id

        返回：
            ReviewReport | None：命中的复盘报告；id 不存在时返回 None
        """
        cur = await self._conn.execute("SELECT * FROM review_reports WHERE id=?", (report_id,))
        row = await cur.fetchone()
        return ReviewReport(**dict(row)) if row else None

    async def latest_review_period_end(self) -> float | None:
        """最近一次复盘的 period_end；无记录返回 None（调度幂等：不重复复盘同一区间）。

        参数：
            无

        返回：
            float | None：最近一次复盘的 period_end；无记录返回 None（调度幂等：不重复复盘同一区间）
        """
        cur = await self._conn.execute("SELECT MAX(period_end) AS value FROM review_reports")
        row = await cur.fetchone()
        value = row["value"] if row else None
        return float(value) if value is not None else None

    # ---------- 复盘统计取数 ----------

    async def trades_for_review(
        self,
        start_ts: float,
        end_ts: float,
        mode: str,
        contract: str | None = None,
        strategy_md5: str | None = None,
    ) -> list[Trade]:
        """区间内成交（[start, end)，按 id 正序），LEFT JOIN decisions 支持按策略版本过滤。

        mode 必填过滤；strategy_md5/contract 非空时分别加对应过滤。
        当前统计口径：无 strategy_md5 过滤时基础样本只按 source/mode 过滤，孤儿平仓成交
        （round_id=''，decisions 无匹配，如 LLM 未配置期间 drain 的强平/止盈止损）也计入；
        仅按策略统计时无 join 匹配的成交不参与（decisions.strategy_md5=? 自然排除 NULL 行，
        与 INNER JOIN 语义一致）。

        参数：
            start_ts: float，查询区间起始时间戳
            end_ts: float，查询区间结束时间戳
            mode: str，交易运行模式
            contract: str | None，合约名称
            strategy_md5: str | None，策略书内容摘要；为空时不按版本过滤

        返回：
            list[Trade]：区间内成交（[start, end)，按 id 正序），LEFT JOIN decisions 支持按策略版本过滤
        """
        sql = (
            "SELECT trades.* FROM trades"
            " LEFT JOIN decisions ON trades.round_id = decisions.round_id"
            " WHERE trades.created_at >= ? AND trades.created_at < ? AND trades.mode=?"
        )
        params: list = [start_ts, end_ts, mode]
        if strategy_md5:
            sql += " AND decisions.strategy_md5=?"
            params.append(strategy_md5)
        if contract:
            sql += " AND trades.contract=?"
            params.append(contract)
        cur = await self._conn.execute(sql + " ORDER BY trades.id", params)
        return [Trade(**dict(r)) for r in await cur.fetchall()]

    async def decisions_for_review(
        self,
        start_ts: float,
        end_ts: float,
        strategy_md5: str | None = None,
        limit: int = 100,
        mode: str | None = None,
    ) -> list[Decision]:
        """区间内决策（[start, end)，按 id 倒序）；limit 钳制到 1..100；mode 非空时按模式过滤。

        参数：
            start_ts: float，查询区间起始时间戳
            end_ts: float，查询区间结束时间戳
            strategy_md5: str | None，策略书内容摘要；为空时不按版本过滤
            limit: int，最多读取或返回的记录数量
            mode: str | None，交易运行模式

        返回：
            list[Decision]：区间内决策（[start, end)，按 id 倒序）；limit 钳制到 1..100；mode 非空时按模式过滤
        """
        limit = max(1, min(100, limit))
        sql = "SELECT * FROM decisions WHERE created_at >= ? AND created_at < ?"
        params: list = [start_ts, end_ts]
        if strategy_md5:
            sql += " AND strategy_md5=?"
            params.append(strategy_md5)
        if mode is not None:
            sql += " AND mode=?"
            params.append(mode)
        params.append(limit)
        cur = await self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", params)
        return [Decision(**dict(r)) for r in await cur.fetchall()]

    async def list_trades_filtered(
        self,
        start_ts: float,
        end_ts: float,
        contract: str | None = None,
        source: str | None = None,
        limit: int = 200,
        mode: str | None = None,
    ) -> list[Trade]:
        """区间内成交（[start, end)，按 id 正序）；contract/source/mode 可选过滤，limit 钳 1..200。

        参数：
            start_ts: float，查询区间起始时间戳
            end_ts: float，查询区间结束时间戳
            contract: str | None，合约名称
            source: str | None，可选的成交来源过滤条件
            limit: int，最多读取或返回的记录数量
            mode: str | None，交易运行模式

        返回：
            list[Trade]：区间内成交（[start, end)，按 id 正序）；contract/source/mode 可选过滤，limit 钳 1..200
        """
        limit = max(1, min(200, limit))
        sql = "SELECT * FROM trades WHERE created_at >= ? AND created_at < ?"
        params: list = [start_ts, end_ts]
        if contract:
            sql += " AND contract=?"
            params.append(contract)
        if source is not None:
            sql += " AND source=?"
            params.append(source)
        if mode is not None:
            sql += " AND mode=?"
            params.append(mode)
        params.append(limit)
        cur = await self._conn.execute(sql + " ORDER BY id LIMIT ?", params)
        return [Trade(**dict(r)) for r in await cur.fetchall()]

    # ---------- audit_rounds（复盘审计轮取数） ----------

    async def latest_review_audit_round(self, mode: str) -> AuditRound | None:
        """按模式取最近一轮复盘审计（wake_source='review' 过滤，排序口径同 latest_audit_round）。

        供 /api/review/live 使用：交易轮（timer 等）再多也不参与；无复盘轮返回 None。

        参数：
            mode: str，交易运行模式

        返回：
            AuditRound | None：按模式取最近一轮复盘审计（wake_source='review' 过滤，排序口径同 latest_audit_round）
        """
        cur = await self._conn.execute(
            "SELECT * FROM audit_rounds WHERE mode=? AND wake_source='review'"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (mode,),
        )
        row = await cur.fetchone()
        return AuditRound(**dict(row)) if row else None
