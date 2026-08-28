"""研报复盘子仓库：复盘候选查询、复盘案例取数与复盘记录 CRUD（issue #113）。

子仓库模式：与 Repo 共享同一 Database（同一连接、同一事务语义），由
Repo.__init__ 挂载为 repo.research_review；本模块只依赖 db/models
（不反向 import Repo），无循环依赖。

口径约定：
- 候选 = 研报逐标的结论中 horizon 合法（当日/3日/周）、窗口已到期、且未被任何
  正式复盘批改过的 (report_id, contract)；存量非法 horizon 行被 IN 过滤天然排除；
- 复盘记录只在复盘报告整体落库成功后才写入（见 C4 的 save_review_bundle），
  因此"已复盘"判定等价于 research_reviews 中存在目标行；
- _insert_review 不 commit，供单条保存与 bundle 多行同事务两种路径复用；
- 人工重评授权（R5-2）由 research_rereview_requests 承载：同一目标最多一条
  未消费授权（部分唯一索引强制），消费与批改落库同事务（_consume_rereview_request）。
"""

from __future__ import annotations

import sqlite3
import time

import aiosqlite

from src.memory.db import Database
from src.memory.models import (
    ResearchAssetView,
    ResearchReport,
    ResearchRereviewRequest,
    ResearchReview,
    ResearchReviewCandidate,
)


def _now() -> float:
    """取当前 Unix 时间戳（秒），作为复盘记录的 created_at 落库时间。

    参数：无

    返回：
        float：当前 Unix 时间戳（秒）
    """
    return time.time()


# horizon→秒数的 SQL 映射：与 payload_v2.HORIZON_SECONDS 唯一对应，改动须同步
_HORIZON_CASE_SQL = (
    "CASE v.horizon WHEN '当日' THEN 86400 WHEN '3日' THEN 259200 WHEN '周' THEN 604800 END"
)

# 候选联表查询：内层算出到期时刻，外层按到期过滤、排除已复盘目标并按 keyset
# 游标（due_at, report_id, contract 三元组严格大于）续扫；排序键与游标同序，
# 页间不重复不遗漏（R5：替代 offset 分页，候选集在扫描期间随复盘落库收缩时
# offset 会跳行/重扫，keyset 不受影响）
_CANDIDATES_SQL = f"""
SELECT * FROM (
    SELECT v.report_id, v.contract, v.direction, v.confidence, v.horizon,
           r.report_type, r.created_at AS report_created_at,
           r.created_at + {_HORIZON_CASE_SQL} AS due_at
    FROM research_asset_views v
    JOIN research_reports r ON r.id = v.report_id
    WHERE v.horizon IN ('当日', '3日', '周')
) c
WHERE c.due_at <= ?
  AND NOT EXISTS (
      SELECT 1 FROM research_reviews rr
      WHERE rr.report_id = c.report_id AND rr.contract = c.contract
  )
  AND (c.due_at, c.report_id, c.contract) > (?, ?, ?)
ORDER BY c.due_at, c.report_id, c.contract
LIMIT ?
"""

# keyset 游标起点哨兵：真实候选的 due_at（Unix 秒）与 report_id 恒大于 0，
# 故 (0.0, 0, '') 等价于"从头扫"，免去了有无游标两套 SQL
_CURSOR_ORIGIN: tuple[float, int, str] = (0.0, 0, "")

_REVIEW_COLUMNS = (
    "review_report_id,report_id,contract,direction_relation,direction_reason,"
    "reasoning_quality,reasoning_review,evidence_reviews_json,"
    "confidence_assessment,confidence_reason,improvement_advice,outcome_json,created_at,"
    "review_kind,rereview_reason,rereview_of_id"
)


async def _consume_rereview_request(
    conn: aiosqlite.Connection, request_id: int, round_id: str
) -> None:
    """把人工重评授权标记为已消费并绑定复盘轮次；不 commit（随 bundle 同事务）。

    参数：
        conn: aiosqlite.Connection，bundle 事务连接
        request_id: int，被消费的重评授权编号
        round_id: str，消费该授权的复盘轮次编号

    返回：
        None：授权行 consumed_round_id 就地更新（提交由调用方事务边界控制）
    """
    await conn.execute(
        "UPDATE research_rereview_requests SET consumed_round_id=? WHERE id=?",
        (round_id, request_id),
    )


async def _insert_review(
    conn: aiosqlite.Connection,
    *,
    review_report_id: int,
    report_id: int,
    contract: str,
    direction_relation: str,
    direction_reason: str,
    reasoning_quality: str,
    reasoning_review: str,
    evidence_reviews_json: str,
    confidence_assessment: str,
    confidence_reason: str,
    improvement_advice: str,
    outcome_json: str,
    created_at: float,
    review_kind: str = "auto",
    rereview_reason: str = "",
    rereview_of_id: int | None = None,
) -> int:
    """插入一条研报复盘记录并返回自增 id；不 commit（事务边界由调用方控制）。

    参数：
        conn: aiosqlite.Connection，共享数据库连接
        review_report_id: int，产生本记录的复盘报告编号
        report_id: int，被复盘的研报编号
        contract: str，被复盘的合约
        direction_relation: str，方向关系枚举评价
        direction_reason: str，方向关系评价理由
        reasoning_quality: str，推理质量枚举评价
        reasoning_review: str，推理质量评价理由
        evidence_reviews_json: str，逐条依据评价 JSON（与原研报 evidence 1:1）
        confidence_assessment: str，置信度合规枚举评价
        confidence_reason: str，置信度合规评价理由
        improvement_advice: str，改进建议
        outcome_json: str，代码计算的客观行情结果 JSON（LLM 不可写）
        created_at: float，落库时间戳
        review_kind: str，复盘种类：auto（自动复盘）/ manual（人工授权重评，R5-2）
        rereview_reason: str，人工授权理由原文（自动复盘为空串）
        rereview_of_id: int | None，被本条重评替代的上一条复盘记录 id

    返回：
        int：新插入行的自增 id
    """
    cur = await conn.execute(
        f"INSERT INTO research_reviews({_REVIEW_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            review_report_id,
            report_id,
            contract,
            direction_relation,
            direction_reason,
            reasoning_quality,
            reasoning_review,
            evidence_reviews_json,
            confidence_assessment,
            confidence_reason,
            improvement_advice,
            outcome_json,
            created_at,
            review_kind,
            rereview_reason,
            rereview_of_id,
        ),
    )
    return cur.lastrowid or 0


class ResearchReviewRepo:
    """研报复盘存取方法集合。写操作除 bundle 复用场景外立即 commit。"""

    def __init__(self, db: Database) -> None:
        """绑定共享数据库句柄（与 Repo 及其他子仓库共用同一连接与事务语义）。

        参数：
            db: Database，数据库连接封装

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

    async def list_review_candidates(
        self,
        as_of_ts: float,
        limit: int = 50,
        cursor: tuple[float, int, str] | None = None,
    ) -> list[ResearchReviewCandidate]:
        """已到期且未被正式复盘的逐标的结论，按到期时刻升序（最久未复盘优先）。

        参数：
            as_of_ts: float，到期判定基准时间戳（due_at ≤ as_of_ts 视为已到期）
            limit: int，最多返回的候选数量
            cursor: tuple[float, int, str] | None，keyset 续扫游标
                （last_due_at, last_report_id, last_contract），只返回严格位于
                游标之后的候选；None 表示从头扫（内部以 _CURSOR_ORIGIN 哨兵实现，
                issue #113 R5）

        返回：
            list[ResearchReviewCandidate]：候选列表；失败研报无逐标的结论，
            被联表自然排除；非法 horizon 的存量行被 IN 过滤排除
        """
        due_at, report_id, contract = cursor if cursor is not None else _CURSOR_ORIGIN
        cur = await self._conn.execute(
            _CANDIDATES_SQL, (as_of_ts, due_at, report_id, contract, limit)
        )
        return [ResearchReviewCandidate(**dict(r)) for r in await cur.fetchall()]

    async def get_scan_cursor(self) -> tuple[float, int, str] | None:
        """读取候选扫描的 keyset 续扫游标（单行状态表 research_review_scan_state）。

        参数：无

        返回：
            tuple[float, int, str] | None：已推进到的 (due_at, report_id, contract)；
            无状态行或任一游标字段为 NULL（已扫到候选集尾部被重置）时返回 None，
            表示下轮从头重扫
        """
        cur = await self._conn.execute(
            "SELECT last_due_at, last_report_id, last_contract "
            "FROM research_review_scan_state WHERE id=1"
        )
        row = await cur.fetchone()
        if row is None:
            return None
        due_at, report_id, contract = (
            row["last_due_at"],
            row["last_report_id"],
            row["last_contract"],
        )
        if due_at is None or report_id is None or contract is None:
            return None
        return (due_at, report_id, contract)

    async def save_scan_cursor(self, cursor: tuple[float, int, str] | None) -> None:
        """持久化候选扫描游标（立即 commit）；None 表示重置（扫到候选集尾部）。

        参数：
            cursor: tuple[float, int, str] | None，已预检推进到的位置；
                None 时三字段写 NULL（下轮从头重扫）

        返回：
            None：单行状态被就地更新并提交
        """
        due_at, report_id, contract = cursor if cursor is not None else (None, None, None)
        await self._conn.execute(
            "INSERT OR REPLACE INTO research_review_scan_state"
            "(id, last_due_at, last_report_id, last_contract) VALUES(1, ?, ?, ?)",
            (due_at, report_id, contract),
        )
        await self._conn.commit()

    async def get_case(
        self, report_id: int, contract: str
    ) -> tuple[ResearchReport, ResearchAssetView] | None:
        """取复盘案例的研报头与目标逐标的结论；任一不存在返回 None。

        参数：
            report_id: int，研报编号
            contract: str，合约名

        返回：
            tuple[ResearchReport, ResearchAssetView] | None：研报头与逐标的结论
        """
        cur = await self._conn.execute("SELECT * FROM research_reports WHERE id=?", (report_id,))
        report_row = await cur.fetchone()
        if report_row is None:
            return None
        cur = await self._conn.execute(
            "SELECT * FROM research_asset_views WHERE report_id=? AND contract=?",
            (report_id, contract),
        )
        view_row = await cur.fetchone()
        if view_row is None:
            return None
        return ResearchReport(**dict(report_row)), ResearchAssetView(**dict(view_row))

    async def save_review(
        self,
        *,
        review_report_id: int,
        report_id: int,
        contract: str,
        direction_relation: str = "",
        direction_reason: str = "",
        reasoning_quality: str = "",
        reasoning_review: str = "",
        evidence_reviews_json: str = "[]",
        confidence_assessment: str = "",
        confidence_reason: str = "",
        improvement_advice: str = "",
        outcome_json: str = "{}",
        created_at: float | None = None,
        review_kind: str = "auto",
        rereview_reason: str = "",
        rereview_of_id: int | None = None,
    ) -> ResearchReview:
        """单条落库一条研报复盘记录（立即 commit）。

        生产唯一写路径为 ReviewRepo.save_review_bundle（报告与批改同事务）；
        本方法仅供测试/调试种子数据使用，业务代码不得直接调用。

        参数：
            review_report_id: int，产生本记录的复盘报告编号
            report_id: int，被复盘的研报编号
            contract: str，被复盘的合约
            direction_relation: str，方向关系枚举评价
            direction_reason: str，方向关系评价理由
            reasoning_quality: str，推理质量枚举评价
            reasoning_review: str，推理质量评价理由
            evidence_reviews_json: str，逐条依据评价 JSON
            confidence_assessment: str，置信度合规枚举评价
            confidence_reason: str，置信度合规评价理由
            improvement_advice: str，改进建议
            outcome_json: str，代码计算的客观行情结果 JSON
            created_at: float | None，可选落库时间戳（测试注入用）；None 取当前时间
            review_kind: str，复盘种类：auto（自动复盘）/ manual（人工授权重评）
            rereview_reason: str，人工授权理由原文（自动复盘为空串）
            rereview_of_id: int | None，被本条重评替代的上一条复盘记录 id

        返回：
            ResearchReview：新写入的复盘记录
        """
        ts = created_at if created_at is not None else _now()
        row_id = await _insert_review(
            self._conn,
            review_report_id=review_report_id,
            report_id=report_id,
            contract=contract,
            direction_relation=direction_relation,
            direction_reason=direction_reason,
            reasoning_quality=reasoning_quality,
            reasoning_review=reasoning_review,
            evidence_reviews_json=evidence_reviews_json,
            confidence_assessment=confidence_assessment,
            confidence_reason=confidence_reason,
            improvement_advice=improvement_advice,
            outcome_json=outcome_json,
            created_at=ts,
            review_kind=review_kind,
            rereview_reason=rereview_reason,
            rereview_of_id=rereview_of_id,
        )
        await self._conn.commit()
        return ResearchReview(
            id=row_id,
            review_report_id=review_report_id,
            report_id=report_id,
            contract=contract,
            direction_relation=direction_relation,
            direction_reason=direction_reason,
            reasoning_quality=reasoning_quality,
            reasoning_review=reasoning_review,
            evidence_reviews_json=evidence_reviews_json,
            confidence_assessment=confidence_assessment,
            confidence_reason=confidence_reason,
            improvement_advice=improvement_advice,
            outcome_json=outcome_json,
            created_at=ts,
            review_kind=review_kind,
            rereview_reason=rereview_reason,
            rereview_of_id=rereview_of_id,
        )

    async def list_reviews(
        self,
        start_ts: float = 0.0,
        end_ts: float | None = None,
        contract: str | None = None,
        limit: int = 50,
    ) -> list[ResearchReview]:
        """按时间窗与合约过滤复盘记录，倒序取最新 limit 条后按 id 正序返回。

        参数：
            start_ts: float，时间窗起点（含）
            end_ts: float | None，时间窗终点（不含）；None 不限上界
            contract: str | None，合约过滤；None 不过滤
            limit: int，最多返回的记录数量

        返回：
            list[ResearchReview]：窗口内最新 limit 条，按 id 正序（最旧在前）
        """
        sql = "SELECT * FROM research_reviews WHERE created_at >= ?"
        params: list = [start_ts]
        if end_ts is not None:
            sql += " AND created_at < ?"
            params.append(end_ts)
        if contract is not None:
            sql += " AND contract = ?"
            params.append(contract)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = await self._conn.execute(sql, params)
        rows = [ResearchReview(**dict(r)) for r in await cur.fetchall()]
        rows.reverse()  # 最新在前 → id 正序（便于拼接上下文与展示）
        return rows

    async def list_reviews_by_report(self, report_id: int) -> list[ResearchReview]:
        """按 id 正序返回某份研报的全部复盘记录（同一研报可被多次复盘，故为多条）。

        参数：
            report_id: int，被复盘的研报编号

        返回：
            list[ResearchReview]：该研报的复盘记录，按 id 正序（最旧在前）；
            未被复盘过时返回空列表
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_reviews WHERE report_id=? ORDER BY id",
            (report_id,),
        )
        return [ResearchReview(**dict(r)) for r in await cur.fetchall()]

    async def get_reports_prompt_md5(self, report_ids: list[int]) -> dict[int, str]:
        """批量取研报的 research_prompt_md5（复盘历史行归因展示用，issue #113 R6）。

        参数：
            report_ids: list[int]，研报编号列表（去重后联查）

        返回：
            dict[int, str]：研报 id → research_prompt_md5；id 不存在或字段为空时不出现
        """
        ids = sorted(set(report_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        cur = await self._conn.execute(
            f"SELECT id, research_prompt_md5 FROM research_reports WHERE id IN ({placeholders})",
            ids,
        )
        return {
            r["id"]: r["research_prompt_md5"]
            for r in await cur.fetchall()
            if r["research_prompt_md5"]
        }

    async def has_review(self, report_id: int, contract: str) -> bool:
        """目标逐标的结论是否已被任何正式复盘批改过。

        参数：
            report_id: int，研报编号
            contract: str，合约名

        返回：
            bool：存在复盘记录为 True
        """
        cur = await self._conn.execute(
            "SELECT 1 FROM research_reviews WHERE report_id=? AND contract=? LIMIT 1",
            (report_id, contract),
        )
        return await cur.fetchone() is not None

    async def latest_review_id(self, report_id: int, contract: str) -> int | None:
        """取目标最新一条复盘记录的 id（人工重评的 rereview_of_id 替代指向用）。

        参数：
            report_id: int，研报编号
            contract: str，合约名

        返回：
            int | None：最新复盘记录 id；从未复盘过时返回 None
        """
        cur = await self._conn.execute(
            "SELECT id FROM research_reviews WHERE report_id=? AND contract=? "
            "ORDER BY id DESC LIMIT 1",
            (report_id, contract),
        )
        row = await cur.fetchone()
        return int(row["id"]) if row is not None else None

    async def create_rereview_request(
        self, report_id: int, contract: str, reason: str, requested_by: str = "human"
    ) -> tuple[ResearchRereviewRequest, bool]:
        """登记一条人工重评授权（幂等）：同一目标已存在未消费授权时返回既有记录。

        未消费唯一性由部分唯一索引 idx_rereview_pending 强制；并发重复登记撞
        唯一约束时回退为读取既有授权（幂等返回），不产生重复待办。

        参数：
            report_id: int，被授权重评的研报编号
            contract: str，被授权重评的合约
            reason: str，人工登记的重评理由（随重评记录入库）
            requested_by: str，授权发起人标识（当前固定为人工 'human'）

        返回：
            tuple[ResearchRereviewRequest, bool]：授权记录与是否复用既有授权
            （True = 幂等命中既有未消费授权，未新建）
        """
        existing = await self.get_pending_rereview_request(report_id, contract)
        if existing is not None:
            return existing, True
        ts = _now()
        try:
            cur = await self._conn.execute(
                "INSERT INTO research_rereview_requests"
                "(report_id, contract, reason, requested_by, created_at) VALUES(?,?,?,?,?)",
                (report_id, contract, reason, requested_by, ts),
            )
            await self._conn.commit()
        except sqlite3.IntegrityError:
            # 并发撞部分唯一索引：他人已登记同目标授权，回滚本次插入后幂等返回既有行
            await self._conn.rollback()
            raced = await self.get_pending_rereview_request(report_id, contract)
            if raced is not None:
                return raced, True
            raise
        return (
            ResearchRereviewRequest(
                id=cur.lastrowid or 0,
                report_id=report_id,
                contract=contract,
                reason=reason,
                requested_by=requested_by,
                created_at=ts,
            ),
            False,
        )

    async def get_pending_rereview_request(
        self, report_id: int, contract: str
    ) -> ResearchRereviewRequest | None:
        """取目标当前未消费的人工重评授权；无待消费授权返回 None。

        参数：
            report_id: int，研报编号
            contract: str，合约名

        返回：
            ResearchRereviewRequest | None：未消费授权记录；不存在时返回 None
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_rereview_requests "
            "WHERE report_id=? AND contract=? AND consumed_round_id='' LIMIT 1",
            (report_id, contract),
        )
        row = await cur.fetchone()
        return ResearchRereviewRequest(**dict(row)) if row is not None else None

    async def list_pending_rereview_requests(self) -> list[ResearchRereviewRequest]:
        """全部未消费的人工重评授权，按登记顺序（id 正序）。

        供候选清单尾部向复盘方展示待处理重评（R5-2：授权须对复盘 agent 可见，
        否则已复盘目标永不进入候选，授权成为死信）。

        参数：无

        返回：
            list[ResearchRereviewRequest]：未消费授权列表；无待办时为空列表
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_rereview_requests WHERE consumed_round_id='' ORDER BY id"
        )
        return [ResearchRereviewRequest(**dict(r)) for r in await cur.fetchall()]
