"""复盘报告 bundle 组装：代码确定性统计段 + 报告与研报复盘的单事务落库编排（issue #113）。

LLM 只产出报告文本与各条批改内容；研报复盘条数、客观结果数据状态分布等
可枚举计数一律由代码从本轮暂存草稿（deps.pending_research_reviews）计算，
追加为报告末尾的「## 研报复盘统计」段，LLM 不可伪造。
"""

from __future__ import annotations

import json
from typing import Any

from src.memory.models import ReviewReport
from src.memory.repo import Repo
from src.memory.research_review_repo import compute_scan_cursor_ack
from src.memory.review_repo import SCAN_CURSOR_UNSET
from src.review.tool_handlers import ReviewToolDeps


def _count_fact_statuses(pending: list[dict[str, Any]]) -> dict[str, int]:
    """从本轮复盘草稿的 evidence_reviews_json 统计事实核对枚举分布（代码计算）。

    参数：
        pending: list[dict[str, Any]]，本轮暂存的研报复盘草稿

    返回：
        dict[str, int]：fact_status → 条数；坏 JSON 的条目按 unknown 计
    """
    counts: dict[str, int] = {}
    for item in pending:
        try:
            evidence = json.loads(item["evidence_reviews_json"])
        except (json.JSONDecodeError, KeyError):
            evidence = []
        if not isinstance(evidence, list):
            evidence = []
        for entry in evidence:
            status = entry.get("fact_status") if isinstance(entry, dict) else None
            counts[status or "unknown"] = counts.get(status or "unknown", 0) + 1
    return counts


def render_research_review_stats(pending: list[dict[str, Any]]) -> str:
    """由本轮研报复盘草稿确定性计算统计段文本；无草稿时返回空串（不追加段落）。

    参数：
        pending: list[dict[str, Any]]，本轮暂存的研报复盘草稿（含
            report_id/contract/outcome_json/evidence_reviews_json 等代码可枚举字段）

    返回：
        str：「## 研报复盘统计」Markdown 段；空列表时返回空串
    """
    if not pending:
        return ""
    contracts: dict[str, int] = {}
    statuses: dict[str, int] = {}
    report_ids: set[int] = set()
    for item in pending:
        contracts[item["contract"]] = contracts.get(item["contract"], 0) + 1
        report_ids.add(item["report_id"])
        try:
            status = json.loads(item["outcome_json"]).get("data_status") or "unknown"
        except json.JSONDecodeError:
            status = "unknown"
        statuses[status] = statuses.get(status, 0) + 1
    fact_statuses = _count_fact_statuses(pending)
    lines = [
        "## 研报复盘统计（代码计算，非 LLM 产出）",
        f"批改条数：{len(pending)}（涉及研报 {len(report_ids)} 份）",
        "合约分布：" + "；".join(f"{c} {n} 条" for c, n in sorted(contracts.items())),
        "客观结果数据状态：" + "；".join(f"{s} {n} 条" for s, n in sorted(statuses.items())),
        "依据事实核对："
        + ("；".join(f"{s} {n} 条" for s, n in sorted(fact_statuses.items())) or "无"),
    ]
    return "\n".join(lines)


async def save_review_bundle(
    repo: Repo,
    deps: ReviewToolDeps,
    *,
    period_start: float,
    period_end: float,
    stats_json: str,
    report_md: str,
    strategy_action: str,
    round_id: str,
) -> ReviewReport:
    """组装最终报告文本（追加代码计算的研报复盘统计段）并单事务落库。

    研报复盘草稿（deps.pending_research_reviews）随报告同事务写入；草稿为空时
    退化为纯报告落库（与 save_review_report 成功路径等价）。本轮做过候选扫描
    （deps.scan_cursor_loaded）时，游标 ack 也随同事务落库（R6-1）：扫到尾部
    落 NULL 重置，否则推进到「最后一个已复盘或已跳过候选」处，停在首个已预检
    可用但本轮未复盘的候选之前；本轮失败不调用本函数，库中游标原地不动。

    参数：
        repo: Repo，持久化仓库
        deps: ReviewToolDeps，本轮工具依赖（读取研报复盘草稿、新建策略版本编号
            与扫描 lease 字段）
        period_start: float，复盘区间起点时间戳
        period_end: float，复盘区间终点时间戳
        stats_json: str，代码预统计 JSON 文本
        report_md: str，LLM 产出的复盘报告正文
        strategy_action: str，策略书处理动作（rewrite/none）
        round_id: str，关联的审计轮次编号

    返回：
        ReviewReport：已提交的复盘报告（report_md 为含统计段的最终文本）
    """
    pending = list(deps.pending_research_reviews.values())
    stats_section = render_research_review_stats(pending)
    final_md = f"{report_md}\n\n{stats_section}" if stats_section else report_md
    scan_cursor: Any = SCAN_CURSOR_UNSET
    if deps.scan_cursor_loaded:
        scan_cursor = (
            None
            if deps.scan_tail
            else compute_scan_cursor_ack(deps.scan_log, set(deps.pending_research_reviews))
        )
    return await repo.review.save_review_bundle(
        period_start,
        period_end,
        stats_json,
        final_md,
        strategy_action,
        new_version_id=deps.created_version_id,
        round_id=round_id,
        research_reviews=pending,
        scan_cursor=scan_cursor,
    )
