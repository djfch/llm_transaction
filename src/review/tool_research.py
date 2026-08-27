"""复盘侧的研报复盘工具（issue #113）：3 只读 + 1 写。

- list_research_review_candidates：已到期未复盘的逐标的结论候选；
- get_research_review_case：单个案例的完整材料（原文+市场快照+研报轮上下文+
  policy_adjustments 归一化记录+代码计算的客观行情），读后登记到
  deps.loaded_research_cases（submit 的前置）；
- list_research_reviews：历史复盘记录查询（修订 prompt 前核对重复问题用）；
- submit_research_review：唯一写出口。只暂存内存草稿（deps.pending_research_reviews），
  报告落库成功才随同事务写入（见 C4 bundle）；outcome 由代码从已读案例缓存附加，
  LLM 提交 outcome 字段一律拒绝。
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.memory.models import ResearchReview
from src.review.research_outcome import compute_outcome
from src.review.tool_handlers import (
    ReviewToolDeps,
    ToolArgError,
    _clamp,
    _fmt_time,
    _need_str,
    _opt_int,
    _opt_str,
    _to_int,
    _truncate,
)

_CASE_SNAPSHOT_LIMIT = 3000  # 案例内市场快照/上下文快照的截断长度


def _parse_evidence_reviews(args: dict) -> list[dict[str, Any]]:
    """解析并校验逐条依据评价列表的结构（index 整数 + comment 非空文本）。

    参数：
        args: dict，工具调用参数字典

    返回：
        list[dict[str, Any]]：结构合法的依据评价列表（1:1 完整性由 submit 校验）

    异常：
        ToolArgError：字段缺失、不是列表、元素结构非法时抛出
    """
    value = args.get("evidence_reviews")
    if not isinstance(value, list):
        raise ToolArgError("缺少必填参数 evidence_reviews（逐条依据评价列表）")
    items: list[dict[str, Any]] = []
    for pos, item in enumerate(value):
        if not isinstance(item, dict):
            raise ToolArgError(f"evidence_reviews[{pos}] 必须是对象（含 index/comment）")
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ToolArgError(f"evidence_reviews[{pos}].index 必须是整数")
        comment = item.get("comment")
        if not isinstance(comment, str) or not comment.strip():
            raise ToolArgError(f"evidence_reviews[{pos}].comment 必须是非空文本")
        items.append({"index": index, "comment": comment.strip()})
    return items


def _format_outcome(outcome: dict[str, Any]) -> str:
    """把客观行情结果字典渲染成一行摘要文本。

    参数：
        outcome: dict[str, Any]，compute_outcome 的结果字典

    返回：
        str：一行客观结果摘要；无价格数据时只呈现状态与说明
    """
    status = outcome.get("data_status", "unknown")
    if outcome.get("start_price") is None:
        error = outcome.get("error") or ""
        return f"data_status={status}（{error or '无价格数据'}）"
    return (
        f"data_status={status}（K线 {outcome['candles_actual']}/{outcome['candles_expected']}）"
        f" | 起价 {outcome['start_price']} → 止价 {outcome['end_price']}"
        f" | 涨跌 {outcome['return_pct']}%"
        f" | 区间最高 {outcome['high']}（{outcome['max_up_pct']}%）"
        f" | 区间最低 {outcome['low']}（{outcome['max_down_pct']}%）"
    )


def _format_review_row(row: ResearchReview) -> str:
    """把一条复盘记录渲染成多行完整文本（供历史查询逐条展示）。

    参数：
        row: ResearchReview，复盘记录

    返回：
        str：含全部评价维度与客观结果摘要的多行文本
    """
    lines = [
        f"复盘#{row.id} | 研报#{row.report_id}/{row.contract} | 复盘报告#{row.review_report_id}"
        f" | 时间={_fmt_time(row.created_at)}",
        f"  方向关系：{row.direction_relation}",
        f"  推理质量：{row.reasoning_quality}",
        f"  置信度合规：{row.confidence_assessment}",
        f"  改进建议：{row.improvement_advice}",
    ]
    try:
        evidence = json.loads(row.evidence_reviews_json)
    except json.JSONDecodeError:
        evidence = []
    if evidence:
        lines.append(
            "  依据评价：" + "；".join(f"[{e.get('index')}] {e.get('comment')}" for e in evidence)
        )
    try:
        outcome = json.loads(row.outcome_json)
    except json.JSONDecodeError:
        outcome = {}
    if outcome:
        lines.append(f"  客观结果：{_format_outcome(outcome)}")
    return "\n".join(lines)


async def list_research_review_candidates(deps: ReviewToolDeps, args: dict) -> str:
    """列出已到期且未被正式复盘的逐标的结论候选（按到期时刻升序）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（用其 repo.research_review 取数）
        args: dict，工具参数：limit（可选，默认 20，限制在 1~100）

    返回：
        str：候选清单文本（含 report_id/contract/方向/horizon/到期时刻）；无候选时返回提示
    """
    limit = _clamp(_opt_int(args, "limit", 20), 1, 100)
    candidates = await deps.repo.research_review.list_review_candidates(time.time(), limit)
    if not candidates:
        return "当前无已到期的研报复盘候选"
    lines = [f"已到期待复盘候选共 {len(candidates)} 条（按到期时刻升序）："]
    for c in candidates:
        lines.append(
            f"- 研报#{c.report_id}/{c.contract} | 方向={c.direction} | 置信={c.confidence}"
            f" | horizon={c.horizon} | 研报时间={_fmt_time(c.report_created_at)}"
            f" | 到期={_fmt_time(c.due_at)} | 类型={c.report_type}"
        )
    return "\n".join(lines)


async def get_research_review_case(deps: ReviewToolDeps, args: dict) -> str:
    """取单个复盘案例的完整材料并登记到已读案例缓存（submit 的前置）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（登记 loaded_research_cases；用其
            candle_source 计算客观行情，未装配时 outcome 以 unavailable 降级）
        args: dict，工具参数：report_id（必填）、contract（必填）

    返回：
        str：案例材料文本（原文+快照+归一化记录+客观结果）；目标不存在时返回核对提示
    """
    report_id = _to_int(args.get("report_id"), "report_id")
    contract = _need_str(args, "contract")
    case = await deps.repo.research_review.get_case(report_id, contract)
    if case is None:
        return f"未找到研报#{report_id}/{contract} 的逐标的结论（请用 list_research_review_candidates 核对）"
    report, view = case
    if deps.candle_source is None:
        outcome: dict[str, Any] = {"data_status": "unavailable", "error": "K线来源未配置"}
    else:
        outcome = compute_outcome(contract, report.created_at, view.horizon, deps.candle_source)
    try:
        policy_adjustments = json.loads(report.raw_json).get("policy_adjustments", [])
    except json.JSONDecodeError:
        policy_adjustments = []
    try:
        evidence = json.loads(view.evidence_json)
    except json.JSONDecodeError:
        evidence = []
    deps.loaded_research_cases[(report_id, contract)] = {
        "outcome": outcome,
        "evidence_count": len(evidence),
    }
    audit = await deps.repo.get_audit_round(report.round_id) if report.round_id else None
    snapshot_text = (
        _truncate(audit.context_snapshot, _CASE_SNAPSHOT_LIMIT)
        if audit is not None and audit.context_snapshot
        else "（无研报轮上下文快照）"
    )
    lines = [
        f"研报#{report.id}/{view.contract} | round={report.round_id or '—'}"
        f" | 方向={view.direction} | 置信={view.confidence} | horizon={view.horizon}"
        f" | 市场状态={view.market_regime} | 技术确认={view.technical_confirmation}"
        f" | 依据类型={view.basis_type} | 研报时间={_fmt_time(report.created_at)}",
        f"结论正文：{view.narrative or '（空）'}",
        f"依据（共 {len(evidence)} 条，提交复盘时须逐条评价，index 从 0 开始）：",
    ]
    lines += [
        f"  [{i}] {item.get('point', '')}（来源：{item.get('source', '')}）"
        for i, item in enumerate(evidence)
    ] or ["  （无依据记录）"]
    try:
        risks = json.loads(view.risks_json)
    except json.JSONDecodeError:
        risks = []
    lines.append("风险：" + ("；".join(risks) if risks else "（无）"))
    lines.append("当时市场快照：" + _truncate(view.market_context_json, _CASE_SNAPSHOT_LIMIT))
    lines.append(f"研报轮上下文快照：{snapshot_text}")
    lines.append(
        "代码归一化记录（policy_adjustments）："
        + ("；".join(policy_adjustments) if policy_adjustments else "无")
    )
    lines.append(
        f"客观行情结果（代码计算，仅供批改参考，不可由你提交）：{_format_outcome(outcome)}"
    )
    return "\n".join(lines)


async def list_research_reviews(deps: ReviewToolDeps, args: dict) -> str:
    """查询历史研报复盘记录（修订 research_prompt 前核对同类问题用）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        args: dict，工具参数：start_ts/end_ts（可选时间窗）、contract（可选过滤）、
            limit（可选，默认 20，限制在 1~100）

    返回：
        str：复盘记录完整文本列表；无记录时返回提示
    """
    start_ts = args.get("start_ts")
    end_ts = args.get("end_ts")
    contract = _opt_str(args, "contract")
    limit = _clamp(_opt_int(args, "limit", 20), 1, 100)
    rows = await deps.repo.research_review.list_reviews(
        start_ts=float(start_ts) if isinstance(start_ts, (int, float)) else 0.0,
        end_ts=float(end_ts) if isinstance(end_ts, (int, float)) else None,
        contract=contract,
        limit=limit,
    )
    if not rows:
        return "无符合条件的研报复盘记录"
    return f"共 {len(rows)} 条研报复盘记录（按时间正序）：\n" + "\n".join(
        _format_review_row(r) for r in rows
    )


async def submit_research_review(deps: ReviewToolDeps, args: dict) -> str:
    """提交对单个逐标的结论的复盘批改（暂存内存草稿，随本轮报告落库生效）。

    校验：须先经 get_research_review_case 读过案例；outcome 由代码从已读案例
    缓存附加，LLM 携带 outcome 字段一律拒绝；evidence_reviews 与原研报依据
    强制 1:1（数量相等且 index 不重不漏覆盖 0..N-1）。同轮对同一目标重复
    提交时更新内存草稿。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（读写 loaded_research_cases 与
            pending_research_reviews）
        args: dict，工具参数：report_id/contract/direction_relation/reasoning_quality/
            evidence_reviews/confidence_assessment/improvement_advice（均必填）

    返回：
        str：提交结果文本；校验失败返回具体原因且不落草稿
    """
    report_id = _to_int(args.get("report_id"), "report_id")
    contract = _need_str(args, "contract")
    key = (report_id, contract)
    case = deps.loaded_research_cases.get(key)
    if case is None:
        return f"参数错误：请先用 get_research_review_case 读取研报#{report_id}/{contract} 的案例材料后再提交批改"
    if "outcome" in args:
        return "参数错误：outcome（客观行情结果）由代码计算附加，不允许提交该字段"
    direction_relation = _need_str(args, "direction_relation")
    reasoning_quality = _need_str(args, "reasoning_quality")
    confidence_assessment = _need_str(args, "confidence_assessment")
    improvement_advice = _need_str(args, "improvement_advice")
    evidence_reviews = _parse_evidence_reviews(args)
    expected = case["evidence_count"]
    indexes = sorted(item["index"] for item in evidence_reviews)
    if len(evidence_reviews) != expected or indexes != list(range(expected)):
        return (
            f"参数错误：evidence_reviews 须与原研报依据一一对应（共 {expected} 条，"
            f"index 不重不漏覆盖 0..{max(expected - 1, 0)}），"
            f"实际收到 {len(evidence_reviews)} 条（index={indexes}）"
        )
    ordered = sorted(evidence_reviews, key=lambda item: item["index"])
    existed = key in deps.pending_research_reviews
    deps.pending_research_reviews[key] = {
        "report_id": report_id,
        "contract": contract,
        "direction_relation": direction_relation,
        "reasoning_quality": reasoning_quality,
        "evidence_reviews_json": json.dumps(ordered, ensure_ascii=False),
        "confidence_assessment": confidence_assessment,
        "improvement_advice": improvement_advice,
        "outcome_json": json.dumps(case["outcome"], ensure_ascii=False),
    }
    verb = "已更新同目标草稿" if existed else "已暂存"
    return (
        f"研报复盘{verb}：研报#{report_id}/{contract}（依据评价 {len(ordered)}/{expected} 条）；"
        "将随本轮复盘报告落库统一生效，报告失败则自动丢弃"
    )
