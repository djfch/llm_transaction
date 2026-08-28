"""研报复盘子仓库：复盘候选查询、复盘案例取数与复盘记录 CRUD（issue #113）。

子仓库模式：与 Repo 共享同一 Database（同一连接、同一事务语义），由
Repo.__init__ 挂载为 repo.research_review；本模块只依赖 db/models
（不反向 import Repo），无循环依赖。

口径约定：
- 候选 = 研报逐标的结论中 horizon 合法（当日/3日/周）、窗口已到期、且未被任何
  正式复盘批改过的 (report_id, contract)；存量非法 horizon 行被 IN 过滤天然排除；
- 复盘记录只在复盘报告整体落库成功后才写入（见 C4 的 save_review_bundle），
  因此"已复盘"判定等价于 research_reviews 中存在目标行；
- _insert_review 不 commit，供单条保存与 bundle 多行同事务两种路径复用。
"""

from __future__ import annotations

import time

import aiosqlite

from src.memory.db import Database
from src.memory.models import (
    ResearchAssetView,
    ResearchReport,
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

# 候选联表查询：内层算出到期时刻，外层按到期过滤并排除已复盘目标
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
ORDER BY c.due_at, c.report_id, c.contract
LIMIT ? OFFSET ?
"""

_REVIEW_COLUMNS = (
    "review_report_id,report_id,contract,direction_relation,direction_reason,"
    "reasoning_quality,reasoning_review,evidence_reviews_json,"
    "confidence_assessment,confidence_reason,improvement_advice,outcome_json,created_at"
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

    返回：
        int：新插入行的自增 id
    """
    cur = await conn.execute(
        f"INSERT INTO research_reviews({_REVIEW_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        self, as_of_ts: float, limit: int = 50, offset: int = 0
    ) -> list[ResearchReviewCandidate]:
        """已到期且未被正式复盘的逐标的结论，按到期时刻升序（最久未复盘优先）。

        参数：
            as_of_ts: float，到期判定基准时间戳（due_at ≤ as_of_ts 视为已到期）
            limit: int，最多返回的候选数量
            offset: int，跳过的候选条数（调用方分页扫描用，issue #113 R10）

        返回：
            list[ResearchReviewCandidate]：候选列表；失败研报无逐标的结论，
            被联表自然排除；非法 horizon 的存量行被 IN 过滤排除
        """
        cur = await self._conn.execute(_CANDIDATES_SQL, (as_of_ts, limit, offset))
        return [ResearchReviewCandidate(**dict(r)) for r in await cur.fetchall()]

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
