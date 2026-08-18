"""研报子仓库：事实层 timeline、判断层 research_reports、分析笔记 causal_links 三组存取。

子仓库模式：ResearchRepo 与 Repo 共享同一 Database（同一连接、同一事务语义），由
Repo.__init__ 挂载为 repo.research；本模块只依赖 db/models（不反向 import Repo），
无循环依赖。

安全约定：
- timeline（事实层）只由代码写入（append_timeline_many），研报/复盘 agent 零写权限；
- research_reports 由研报 agent 代码直接落库（不经工具）；
- causal_links 由工具 submit_causal_links 校验暂存、研报落库后由 agent 代码回填
  report_id 批量落库（LLM 无法预知本轮研报 id，H1 修复后口径）；
- causal_links 版本化：新链可声明 supersedes_id 替代旧链（同事务把旧链 status 标记
  superseded，旧链保留留档）；待验证链（await_verification=1）进入未闭合监控池，
  由预注入持续跟进，直到被替代或复盘验证结案。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import time

import aiosqlite

from src.memory.db import Database
from src.memory.models import AuditRound, CausalLink, ResearchReport, Timeline
from src.memory.research_asset_repo import ResearchAssetRepoMixin


def _now() -> float:
    """取当前 Unix 时间戳（秒），作为记录的 created_at/fetched_at 落库时间。

    参数：无

    返回：
        float：当前 Unix 时间戳（秒）
    """
    return time.time()


class ResearchRepo(ResearchAssetRepoMixin):
    """研报系统存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        """初始化研报仓库，绑定与主 Repo 共享的数据库实例。

        参数：
            db: Database，数据库连接封装（与 Repo 及其他子仓库共用同一连接、同一事务语义）

        返回：
            None，仅将连接引用保存到实例属性
        """
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        """取共享的 aiosqlite 连接，供本仓库各存取方法执行 SQL。

        参数：无

        返回：
            aiosqlite.Connection：与 Repo 共享的同一数据库连接
        """
        return self._db.conn

    # ---------- timeline（事实层，代码写入） ----------

    @property
    def _db_path(self) -> Path:
        """提供 SQLite 数据库文件路径，供逐标的存取（ResearchAssetRepoMixin）独立开连接使用。

        参数：无

        返回：
            Path：SQLite 数据库文件路径
        """
        return self._db.path

    async def append_timeline_many(self, items: list[dict]) -> int:
        """批量增量追加事实记录，返回实际新插入条数。

        幂等实现：dedup_key 唯一约束 + INSERT OR IGNORE，重复采集自动跳过；
        条数用连接级 total_changes 差值统计（忽略的行不计入）。

        参数：
            items: list[dict]，待增量写入的事实记录字典列表

        返回：
            int：批量增量追加事实记录，返回实际新插入条数
        """
        if not items:
            return 0
        before = self._conn.total_changes
        await self._conn.executemany(
            "INSERT OR IGNORE INTO timeline(source,kind,title,url,published_at,"
            "meta_json,dedup_key,fetched_at) VALUES(:source,:kind,:title,:url,"
            ":published_at,:meta_json,:dedup_key,:fetched_at)",
            items,
        )
        await self._conn.commit()
        return self._conn.total_changes - before

    async def list_timeline(
        self, start_ts: float = 0.0, end_ts: float | None = None, limit: int = 500
    ) -> list[Timeline]:
        """按时间范围查询事实记录（[start, end)），取窗口内**最新** limit 条按正序返回。

        参数：
            start_ts: float，查询区间起始时间戳
            end_ts: float | None，查询区间结束时间戳；为空时不限制上界
            limit: int，每页最多返回的记录数量

        返回：
            list[Timeline]：按时间范围查询事实记录（[start, end)），取窗口内**最新** limit 条按正序返回
        """
        sql = "SELECT * FROM timeline WHERE published_at >= ?"
        params: list = [start_ts]
        if end_ts is not None:
            sql += " AND published_at < ?"
            params.append(end_ts)
        sql += " ORDER BY published_at DESC LIMIT ?"
        params.append(limit)
        cur = await self._conn.execute(sql, params)
        rows = [Timeline(**dict(r)) for r in await cur.fetchall()]
        rows.reverse()  # 最新在前 → 时间正序（便于拼接上下文）
        return rows

    async def latest_dedup_keys(self, limit: int = 2000) -> set[str]:
        """最近 limit 条的 dedup_key 集合（增量定位：只追加集合外的新记录）。

        参数：
            limit: int，每页最多返回的记录数量

        返回：
            set[str]：最近 limit 条的 dedup_key 集合（增量定位：只追加集合外的新记录）
        """
        cur = await self._conn.execute(
            "SELECT dedup_key FROM timeline ORDER BY id DESC LIMIT ?", (limit,)
        )
        return {r["dedup_key"] for r in await cur.fetchall()}

    # ---------- research_reports（判断层，研报 agent 产出） ----------

    async def save_failed_report(
        self,
        *,
        report_type: str,
        error: str,
        raw_json: str = "{}",
        round_id: str = "",
    ) -> ResearchReport:
        """保存当前结构的失败报告头；失败报告不生成逐标的结论。

            参数：
                report_type: str，研报盘口类型
                error: str，研报失败原因
                raw_json: str，失败时保留的 LLM 原始 JSON
                round_id: str，决策轮编号

            返回：
                ResearchReport：保存当前结构的失败报告头；失败报告不生成逐标的结论

        异常：
            ValueError：error 为空，无法形成失败报告时抛出
        """
        if not error:
            raise ValueError("失败报告必须包含 error")
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO research_reports(report_type,schema_version,summary,"
            "cross_market_view,global_risks_json,raw_json,error,round_id,created_at)"
            " VALUES(?,2,?,?,?,?,?,?,?)",
            (report_type, "", "", "[]", raw_json, error, round_id, ts),
        )
        await self._conn.commit()
        return ResearchReport(
            id=cur.lastrowid or 0,
            report_type=report_type,
            schema_version=2,
            raw_json=raw_json,
            error=error,
            round_id=round_id,
            created_at=ts,
        )

    async def find_report_by_round_id(self, round_id: str) -> ResearchReport | None:
        """按审计轮次编号反查研报（含失败记录），同轮多份取最新一份；查无返回 None。

        供取消收尾使用：取消可能掐在「成功 INSERT/COMMIT 已执行、保存函数未返回」的
        窗口，调用方内存中的 report_id 仍为 None——此时不信内存布尔位，按 round_id
        反查数据库确认成功报告是否其实已提交，避免同轮成功/失败双写。

        参数：
            round_id: str，审计轮次编号

        返回：
            ResearchReport | None：该轮最新一份研报；查无此轮记录返回 None
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_reports WHERE round_id=? ORDER BY id DESC LIMIT 1",
            (round_id,),
        )
        row = await cur.fetchone()
        return ResearchReport(**dict(row)) if row else None

    async def list_reports(self, days: int = 7) -> list[ResearchReport]:
        """近 days 天研报，按创建时间正序（最旧在前，便于拼接上下文）。

        参数：
            days: int，向前查询的自然日数

        返回：
            list[ResearchReport]：近 days 天研报，按创建时间正序（最旧在前，便于拼接上下文）
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_reports WHERE created_at >= ? AND error = '' ORDER BY id",
            (_now() - days * 86400,),
        )
        return [ResearchReport(**dict(r)) for r in await cur.fetchall()]

    async def latest_report(self, include_error: bool = False) -> ResearchReport | None:
        """最近一份研报；默认只取成功（error=''），include_error=True 含失败记录。

        参数：
            include_error: bool，是否允许返回失败研报

        返回：
            ResearchReport | None：最近一份研报；默认只取成功（error=''），include_error=True 含失败记录
        """
        sql = "SELECT * FROM research_reports"
        if not include_error:
            sql += " WHERE error = ''"
        sql += " ORDER BY id DESC LIMIT 1"
        cur = await self._conn.execute(sql)
        row = await cur.fetchone()
        return ResearchReport(**dict(row)) if row else None

    async def get_report(self, report_id: int) -> ResearchReport | None:
        """按 id 取研报（含失败记录）；不存在返回 None。

        参数：
            report_id: int，研报记录编号

        返回：
            ResearchReport | None：按 id 取研报（含失败记录）；不存在返回 None
        """
        cur = await self._conn.execute("SELECT * FROM research_reports WHERE id=?", (report_id,))
        row = await cur.fetchone()
        return ResearchReport(**dict(row)) if row else None

    async def list_reports_page(
        self, limit: int = 20, offset: int = 0
    ) -> tuple[list[ResearchReport], int]:
        """分页查询研报（含失败记录），最新在前；返回 (items, total)。

        total 用独立 COUNT(*) 统计：越界页 items 为空但 total 仍准确。

        参数：
            limit: int，每页最多返回的记录数量
            offset: int，分页起始偏移量

        返回：
            tuple[list[ResearchReport], int]：分页查询研报（含失败记录），最新在前；返回 (items, total)
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_reports ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        items = [ResearchReport(**dict(r)) for r in await cur.fetchall()]
        cur = await self._conn.execute("SELECT COUNT(*) AS total FROM research_reports")
        row = await cur.fetchone()
        total = int(row["total"]) if row else 0
        return items, total

    async def has_report_since(self, report_type: str, since_ts: float) -> bool:
        """since_ts 后是否存在该 report_type 的研报记录（成功或失败都算——对齐复盘幂等口径：失败不自动重试，防 LLM 故障时每分钟重发）。

        参数：
            report_type: str，研报盘口类型
            since_ts: float，幂等检查起始时间戳

        返回：
            bool：since_ts 后是否存在该 report_type 的研报记录（成功或失败都算——对齐复盘幂等口径：失败不自动重试，防 LLM 故障时每分钟重发）
        """
        cur = await self._conn.execute(
            "SELECT 1 FROM research_reports WHERE report_type=? AND created_at>=? LIMIT 1",
            (report_type, since_ts),
        )
        return await cur.fetchone() is not None

    async def claim_schedule_run(self, schedule_id: str, scheduled_date: date) -> bool:
        """原子认领某调度在指定 UTC+8 计划日期的执行权。

        参数：
            schedule_id: str，调度项唯一标识
            scheduled_date: date，UTC+8 计划执行日期

        返回：
            bool：首次认领返回 True，重复认领返回 False
        """
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO research_schedule_runs"
            "(schedule_id,scheduled_date,claimed_at) VALUES(?,?,?)",
            (schedule_id, scheduled_date.isoformat(), _now()),
        )
        await self._conn.commit()
        return cur.rowcount == 1

    # ---------- causal_links（分析笔记，研报 agent 提交） ----------

    async def save_causal_link(
        self,
        *,
        report_id: int,
        chain_json: str,
        confidence: float,
        evidence_json: str = "[]",
        topic: str = "",
        supersedes_id: int | None = None,
        await_verification: bool = True,
    ) -> CausalLink:
        """落库一条因果链；status 默认 pending（第二期复盘标记 verified/failed）。

        版本化：supersedes_id 非空时同一事务内先插入新链、再把旧链 status 标记为
        superseded（旧链保留留档，复盘可对比各版本）；任一步失败整体不 commit
        （aiosqlite 单连接串行，未 commit 自然回滚）。

        参数：
            report_id: int，研报记录编号
            chain_json: str，因果链节点 JSON
            confidence: float，因果链置信度
            evidence_json: str，因果链证据 JSON
            topic: str，因果链主题
            supersedes_id: int | None，被当前链替代的旧链编号
            await_verification: bool，该链是否仍需后续验证

        返回：
            CausalLink：落库一条因果链；status 默认 pending（第二期复盘标记 verified/failed）
        """
        cur = await self._conn.execute(
            "INSERT INTO causal_links(report_id,chain_json,confidence,evidence_json,"
            "status,broken_at,topic,supersedes_id,await_verification,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                report_id,
                chain_json,
                confidence,
                evidence_json,
                "pending",
                None,
                topic,
                supersedes_id,
                int(bool(await_verification)),
                _now(),
            ),
        )
        if supersedes_id is not None:
            await self._conn.execute(
                "UPDATE causal_links SET status='superseded' WHERE id=?", (supersedes_id,)
            )
        await self._conn.commit()
        return CausalLink(
            id=cur.lastrowid or 0,
            report_id=report_id,
            chain_json=chain_json,
            confidence=confidence,
            evidence_json=evidence_json,
            topic=topic,
            supersedes_id=supersedes_id,
            await_verification=bool(await_verification),
            created_at=_now(),
        )

    async def get_causal_link(self, link_id: int) -> CausalLink | None:
        """按 id 取因果链（supersedes 校验用）；不存在返回 None。

        参数：
            link_id: int，因果链记录编号

        返回：
            CausalLink | None：按 id 取因果链（supersedes 校验用）；不存在返回 None
        """
        cur = await self._conn.execute("SELECT * FROM causal_links WHERE id=?", (link_id,))
        row = await cur.fetchone()
        return CausalLink(**dict(row)) if row else None

    async def list_pending_causal_links(self, limit: int = 10) -> list[CausalLink]:
        """未闭合链：待验证声明（await_verification=1）且未被替代（status=pending）。

        预注入用：最新在前取 limit 条后按 id 正序返回；不按时间淘汰——事件发展
        需要时间，直到被替代或复盘盖章才闭合。

        参数：
            limit: int，每页最多返回的记录数量

        返回：
            list[CausalLink]：未闭合链：待验证声明（await_verification=1）且未被替代（status=pending）
        """
        cur = await self._conn.execute(
            "SELECT * FROM causal_links WHERE status='pending' AND await_verification=1"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [CausalLink(**dict(r)) for r in await cur.fetchall()]
        rows.reverse()  # 最新在前 → 时间正序（便于拼接上下文）
        return rows

    async def list_causal_links(
        self, days: int = 7, topic: str | None = None, limit: int = 200
    ) -> list[CausalLink]:
        """近 days 天因果链（含历史版与全部状态），按创建时间正序。

        topic 非空时只取该主题的链（read_causal_links 工具按主题查族谱用）。

        参数：
            days: int，向前查询的自然日数
            topic: str | None，因果链主题
            limit: int，每页最多返回的记录数量

        返回：
            list[CausalLink]：近 days 天因果链（含历史版与全部状态），按创建时间正序
        """
        sql = "SELECT * FROM causal_links WHERE created_at >= ?"
        params: list = [_now() - days * 86400]
        if topic:
            sql += " AND topic = ?"
            params.append(topic)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = await self._conn.execute(sql, params)
        rows = [CausalLink(**dict(r)) for r in await cur.fetchall()]
        rows.reverse()
        return rows

    async def list_causal_links_by_report(self, report_id: int) -> list[CausalLink]:
        """按研报 id 取因果链，id 正序（同一研报内按提交顺序）。

        参数：
            report_id: int，研报记录编号

        返回：
            list[CausalLink]：按研报 id 取因果链，id 正序（同一研报内按提交顺序）
        """
        cur = await self._conn.execute(
            "SELECT * FROM causal_links WHERE report_id=? ORDER BY id", (report_id,)
        )
        return [CausalLink(**dict(r)) for r in await cur.fetchall()]

    # ---------- audit_rounds（研报审计轮取数） ----------

    async def latest_research_audit_round(self, mode: str) -> AuditRound | None:
        """按模式取最近一轮研报审计（wake_source='research' 过滤，排序口径同 latest_audit_round）。

        交易轮（timer 等）再多也不参与；无研报轮返回 None。

        参数：
            mode: str，交易运行模式

        返回：
            AuditRound | None：按模式取最近一轮研报审计（wake_source='research' 过滤，排序口径同 latest_audit_round）
        """
        cur = await self._conn.execute(
            "SELECT * FROM audit_rounds WHERE mode=? AND wake_source='research'"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (mode,),
        )
        row = await cur.fetchone()
        return AuditRound(**dict(row)) if row else None
