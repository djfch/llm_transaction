"""复盘侧的研报复盘工具（issue #113）：3 只读 + 1 写。

- list_research_review_candidates：已到期未复盘且客观行情可批改的逐标的结论候选；
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

from src.memory.models import ResearchReview, ResearchReviewCandidate
from src.research.payload_v2 import HORIZON_SECONDS
from src.review.research_outcome import (
    PARTIAL_MIN_COVERAGE_PCT,
    compute_outcome,
    partial_acceptable,
)
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
# 候选扫描预算（V5）：单次 list_research_review_candidates 调用最多预检的候选数
# （= 最多发起的 K 线请求数）。每候选预检是一次 from/to 窗口 K 线拉取，不设上限
# 时大量数据不可用候选会把单次工具调用拖成数百次网关请求；预算用尽时返回
# offset 续扫游标，由复盘方下一轮或显式续扫接力。
MAX_CANDIDATE_SCAN = 200

# 复盘枚举（值 → 中文释义）：schema 描述、工具校验与预注入渲染共用同一来源，改动须同步
DIRECTION_RELATIONS = {
    "realized": "兑现",
    "diverged": "背离",
    "digested": "震荡消化",
    "invalidated": "失效",
    "unverifiable": "无法核对",
}
# 总体推理质量枚举为 issue #113 定稿 sound/partial/flawed/unreviewable
# （逐条依据层的 reasoning_status 枚举是另一套，见 EVIDENCE_REASONING_STATUSES，勿混淆）
REASONING_QUALITIES = {
    "sound": "成立",
    "partial": "部分成立",
    "flawed": "有缺陷",
    "unreviewable": "无法评价",
}
CONFIDENCE_ASSESSMENTS = {
    "appropriate": "匹配合理",
    "too_high": "偏高",
    "too_low": "偏低",
    "unreviewable": "无法评价",
}
EVIDENCE_FACT_STATUSES = {
    "confirmed": "已证实",
    "contradicted": "已证伪",
    "unverifiable": "无法核实",
}
EVIDENCE_REASONING_STATUSES = {
    "supported": "支撑结论",
    "partially_supported": "部分支撑",
    "unsupported": "不支撑",
    "counterevidence": "构成反证",
    "unverifiable": "无法核实",
}


def _enum_text(options: dict[str, str]) -> str:
    """把枚举字典渲染为「值=释义」顿号串（用于校验错误提示）。

    参数：
        options: dict[str, str]，枚举值到中文释义的映射

    返回：
        str：如「realized=兑现、diverged=背离」的枚举说明串
    """
    return "、".join(f"{value}={label}" for value, label in options.items())


def _need_enum(args: dict, name: str, options: dict[str, str]) -> str:
    """读取必填枚举参数并校验取值合法。

    参数：
        args: dict，工具调用参数字典
        name: str，参数名
        options: dict[str, str]，合法枚举值到中文释义的映射

    返回：
        str：校验通过的枚举值

    异常：
        ToolArgError：参数缺失或取值不在枚举内时抛出
    """
    value = _need_str(args, name)
    if value not in options:
        raise ToolArgError(f"参数 {name} 取值非法：{value}（合法取值：{_enum_text(options)}）")
    return value


def _check_enum_item(item: dict, pos: int, name: str, options: dict[str, str]) -> str:
    """校验依据评价元素内单个枚举字段并返回其值。

    参数：
        item: dict，单条依据评价
        pos: int，该条在列表中的位置（错误提示用）
        name: str，枚举字段名
        options: dict[str, str]，合法枚举值到中文释义的映射

    返回：
        str：校验通过的枚举值

    异常：
        ToolArgError：字段缺失或取值非法时抛出
    """
    value = item.get(name)
    if not isinstance(value, str) or value not in options:
        raise ToolArgError(
            f"evidence_reviews[{pos}].{name} 必须是合法枚举（{_enum_text(options)}）"
        )
    return value


def _parse_evidence_reviews(args: dict) -> list[dict[str, Any]]:
    """解析并校验逐条依据评价列表（evidence_index 整数 + 两个枚举 + explanation 非空）。

    每条结构：evidence_index（原研报依据序号，从 0 开始）、fact_status（事实核对
    枚举）、reasoning_status（推理支撑枚举）、explanation（评价说明，须写明核对
    来源）；1:1 完整性由 submit 校验。

    参数：
        args: dict，工具调用参数字典

    返回：
        list[dict[str, Any]]：结构合法的依据评价列表

    异常：
        ToolArgError：字段缺失、不是列表、元素结构或枚举非法时抛出
    """
    value = args.get("evidence_reviews")
    if not isinstance(value, list):
        raise ToolArgError("缺少必填参数 evidence_reviews（逐条依据评价列表）")
    items: list[dict[str, Any]] = []
    for pos, item in enumerate(value):
        if not isinstance(item, dict):
            raise ToolArgError(
                f"evidence_reviews[{pos}] 必须是对象（含 evidence_index/fact_status/"
                "reasoning_status/explanation）"
            )
        index = item.get("evidence_index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ToolArgError(f"evidence_reviews[{pos}].evidence_index 必须是整数")
        fact_status = _check_enum_item(item, pos, "fact_status", EVIDENCE_FACT_STATUSES)
        reasoning_status = _check_enum_item(
            item, pos, "reasoning_status", EVIDENCE_REASONING_STATUSES
        )
        explanation = item.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ToolArgError(
                f"evidence_reviews[{pos}].explanation 必须是非空文本（写明核对来源与结论）"
            )
        items.append(
            {
                "evidence_index": index,
                "fact_status": fact_status,
                "reasoning_status": reasoning_status,
                "explanation": explanation.strip(),
            }
        )
    return items


def _evidence_reviews_error(evidence_reviews: list[dict[str, Any]], expected: int) -> str | None:
    """校验逐条依据评价与原研报依据的 1:1 完整性（数量相等且 evidence_index 覆盖 0..N-1）。

    参数：
        evidence_reviews: list[dict[str, Any]]，已结构校验的依据评价列表
        expected: int，原研报依据条数

    返回：
        str | None：不通过时返回错误文本，通过时返回 None
    """
    indexes = sorted(item["evidence_index"] for item in evidence_reviews)
    if len(evidence_reviews) == expected and indexes == list(range(expected)):
        return None
    return (
        f"参数错误：evidence_reviews 须与原研报依据一一对应（共 {expected} 条，"
        f"evidence_index 不重不漏覆盖 0..{max(expected - 1, 0)}），"
        f"实际收到 {len(evidence_reviews)} 条（evidence_index={indexes}）"
    )


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
    # R3 后 start/end/高/低要么齐全（有完整落窗 K 线）要么全缺，无需分支止价缺失
    line = (
        f"data_status={status}（K线 {outcome['candles_actual']}/{outcome['candles_expected']}）"
        f" | 起价 {outcome['start_price']} → 止价 {outcome['end_price']}"
        f" | 涨跌 {outcome['return_pct']}%"
        f" | 区间最高 {outcome['high']}（{outcome['max_up_pct']}%）"
        f" | 区间最低 {outcome['low']}（{outcome['max_down_pct']}%）"
    )
    # 价格时点仅在有完整落窗 K 线时存在（旧落库记录无此字段，缺省不展示）
    if outcome.get("price_start_at") is not None:
        line += f" | 首价时点 {outcome['price_start_at']} | 末价时点 {outcome['price_end_at']}"
    return line


def _format_review_row(row: ResearchReview, prompt_md5: str = "") -> str:
    """把一条复盘记录渲染成多行完整文本（供历史查询逐条展示）。

    参数：
        row: ResearchReview，复盘记录
        prompt_md5: str，被复盘研报所用研报提示词正文 md5（无记录时空串，不展示）

    返回：
        str：含全部评价维度（枚举+理由）、提示词归因与客观结果摘要的多行文本
    """
    lines = [
        f"复盘#{row.id} | 研报#{row.report_id}/{row.contract} | 复盘报告#{row.review_report_id}"
        f" | 时间={_fmt_time(row.created_at)}"
        + (f" | 研报提示词 md5={prompt_md5[:8]}…" if prompt_md5 else ""),
        f"  方向关系：{row.direction_relation} | 理由：{row.direction_reason}",
        f"  推理质量：{row.reasoning_quality} | 复核：{row.reasoning_review}",
        f"  置信度合规：{row.confidence_assessment} | 理由：{row.confidence_reason}",
        f"  改进建议：{row.improvement_advice}",
    ]
    try:
        evidence = json.loads(row.evidence_reviews_json)
    except json.JSONDecodeError:
        evidence = []
    if evidence:
        lines.append(
            "  依据评价："
            + "；".join(
                f"[{e.get('evidence_index')}] 事实={e.get('fact_status')}"
                f" 推理={e.get('reasoning_status')}：{e.get('explanation')}"
                for e in evidence
            )
        )
    try:
        outcome = json.loads(row.outcome_json)
    except json.JSONDecodeError:
        outcome = {}
    if outcome:
        lines.append(f"  客观结果：{_format_outcome(outcome)}")
    return "\n".join(lines)


def _format_candidate_line(c: ResearchReviewCandidate) -> str:
    """把一条复盘候选渲染成一行摘要文本。

    参数：
        c: ResearchReviewCandidate，候选行

    返回：
        str：含 report_id/contract/方向/置信/horizon/研报时间/到期时刻/类型的一行文本
    """
    return (
        f"- 研报#{c.report_id}/{c.contract} | 方向={c.direction} | 置信={c.confidence}"
        f" | horizon={c.horizon} | 研报时间={_fmt_time(c.report_created_at)}"
        f" | 到期={_fmt_time(c.due_at)} | 类型={c.report_type}"
    )


def _skipped_names(skipped: list[ResearchReviewCandidate]) -> str:
    """把被跳过的候选渲染成身份串（最多列 5 条，超出以「等 N 条」收尾）。

    参数：
        skipped: list[ResearchReviewCandidate]，被跳过候选列表

    返回：
        str：研报#id/contract 顿号串；超 5 条时追加「等 N 条」
    """
    names = "、".join(f"研报#{c.report_id}/{c.contract}" for c in skipped[:5])
    return f"{names} 等 {len(skipped)} 条" if len(skipped) > 5 else names


async def _candidate_usable(deps: ReviewToolDeps, c: ResearchReviewCandidate) -> bool:
    """快速计算候选的客观行情结果，以提交侧同一 partial_acceptable 门槛判定可用性。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（用其 candle_source 拉取窗口 K 线，
            调用方须先确认已装配非 None）
        c: ResearchReviewCandidate，候选行

    返回：
        bool：True 表示数据足以支撑批改（complete 或达标 partial）；pending/
        unavailable/不达门槛的 partial 一律 False——预检口径与提交门禁一致，
        避免列出提交时必然被拒的候选（V2）
    """
    outcome = await compute_outcome(c.contract, c.report_created_at, c.horizon, deps.candle_source)
    return partial_acceptable(outcome)


async def _list_candidates_unchecked(deps: ReviewToolDeps, now: float, limit: int) -> str:
    """K 线来源未装配时的降级列出：不做可用性预检，全量列出并附说明（R10）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        now: float，当前时间戳
        limit: int，条数上限

    返回：
        str：候选清单文本；无候选时返回提示
    """
    candidates = await deps.repo.research_review.list_review_candidates(now, limit)
    if not candidates:
        return "当前无已到期的研报复盘候选"
    lines = [
        f"已到期待复盘候选共 {len(candidates)} 条（按到期时刻升序；"
        "K线来源未装配，未做客观数据可用性预检）："
    ]
    lines.extend(_format_candidate_line(c) for c in candidates)
    return "\n".join(lines)


async def _scan_usable_candidates(
    deps: ReviewToolDeps, now: float, limit: int, offset: int
) -> tuple[list[ResearchReviewCandidate], list[ResearchReviewCandidate], int, int]:
    """分页扫描候选并逐条预检可用性（受 MAX_CANDIDATE_SCAN 预算约束）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        now: float，当前时间戳
        limit: int，要凑满的可用候选条数
        offset: int，扫描起点游标（只在实际预检过的候选上推进，不漏扫）

    返回：
        tuple：可用候选列表、被跳过候选列表、已预检条数、推进后的游标
    """
    usable: list[ResearchReviewCandidate] = []
    skipped: list[ResearchReviewCandidate] = []
    scanned = 0  # 已预检候选数（= 已发起的 K 线请求数），受扫描预算约束
    while len(usable) < limit and scanned < MAX_CANDIDATE_SCAN:
        page = min(limit, MAX_CANDIDATE_SCAN - scanned)
        batch = await deps.repo.research_review.list_review_candidates(now, page, offset)
        if not batch:
            break
        for c in batch:
            if len(usable) >= limit:
                break
            scanned += 1
            offset += 1
            if await _candidate_usable(deps, c):
                usable.append(c)
            else:
                skipped.append(c)
        if len(batch) < page:
            break
    return usable, skipped, scanned, offset


async def list_research_review_candidates(deps: ReviewToolDeps, args: dict) -> str:
    """列出已到期、未被正式复盘且客观行情达提交门槛的逐标的结论候选（按到期时刻升序）。

    K线来源装配时对每候选快速计算客观结果，以提交侧同一 partial_acceptable
    门槛做可用性预检（complete 或达标 partial 才保留），跳过数据不达门槛的
    候选并列出其身份（V2）；扫描有界——单次调用最多预检 MAX_CANDIDATE_SCAN
    条候选（= K 线请求数上限），预算用尽而未凑满 limit 时在结果中给出 offset
    续扫游标（V5）；来源未装配时不做可用性预检、全量列出并附说明
    （issue #113 R10）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（用其 repo.research_review 分页取数、
            candle_source 做可用性预检）
        args: dict，工具参数：limit（可选，默认 20，限制在 1~100）、offset
            （可选，默认 0；续扫游标，上轮预算用尽时按其提示传入）

    返回：
        str：候选清单文本（含 report_id/contract/方向/horizon/到期时刻、跳过
        候选身份与续扫提示）；无候选或无可用候选时返回提示
    """
    limit = _clamp(_opt_int(args, "limit", 20), 1, 100)
    now = time.time()
    if deps.candle_source is None:
        return await _list_candidates_unchecked(deps, now, limit)
    offset = max(0, _opt_int(args, "offset", 0))
    usable, skipped, scanned, offset = await _scan_usable_candidates(deps, now, limit, offset)
    budget_hint = ""
    if scanned >= MAX_CANDIDATE_SCAN and len(usable) < limit:
        budget_hint = (
            f"（本轮扫描预算 {MAX_CANDIDATE_SCAN} 条已用尽，传 offset={offset} "
            "继续扫描，或留待下一轮复盘）"
        )
    if not usable:
        if skipped:
            return (
                f"已到期待复盘候选共 {len(skipped)} 条，但客观行情数据均不达提交门槛"
                f"（{_skipped_names(skipped)}），留待后续轮次；若逐条核对后确认行情"
                "数据不可恢复，可用 get_research_review_case 读案例后以 "
                f"reasoning_quality=unreviewable 结案{budget_hint}"
            )
        return "当前无已到期的研报复盘候选"
    lines = [f"已到期待复盘候选共 {len(usable)} 条（按到期时刻升序）："]
    lines.extend(_format_candidate_line(c) for c in usable)
    if skipped:
        lines.append(
            f"（另跳过 {len(skipped)} 条客观行情数据不达提交门槛的候选："
            f"{_skipped_names(skipped)}；留待后续轮次，若逐条核对后确认行情数据"
            "不可恢复，可读案例后以 reasoning_quality=unreviewable 结案）"
        )
    if budget_hint:
        lines.append(budget_hint)
    return "\n".join(lines)


async def get_research_review_case(deps: ReviewToolDeps, args: dict) -> str:
    """取单个复盘案例的完整材料并登记到已读案例缓存（submit 的前置）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（登记 loaded_research_cases；用其
            candle_source 计算客观行情，未装配时 outcome 以 unavailable 降级）
        args: dict，工具参数：report_id（必填）、contract（必填）

    返回：
        str：案例材料文本（原文+快照+归一化记录+当时因果链+客观结果）；
        目标不存在时返回核对提示
    """
    report_id = _to_int(args.get("report_id"), "report_id")
    contract = _need_str(args, "contract")
    case = await deps.repo.research_review.get_case(report_id, contract)
    if case is None:
        return f"未找到研报#{report_id}/{contract} 的逐标的结论（请用 list_research_review_candidates 核对）"
    report, view = case
    outcome = await _case_outcome(deps, report, view, contract)
    policy_adjustments, evidence, risks = _parse_case_jsons(report, view)
    deps.loaded_research_cases[(report_id, contract)] = {
        "outcome": outcome,
        "evidence_count": len(evidence),
        # 案例窗口登记：复盘侧历史数据工具（read_timeline/get_macro_series）
        # 只允许回看 [created_at, min(window_end, now)] 区间，防止引用未来数据
        "created_at": report.created_at,
        "window_end": (
            report.created_at + HORIZON_SECONDS[view.horizon]
            if view.horizon in HORIZON_SECONDS
            else None
        ),
    }
    snapshot_text = await _case_context_text(deps, report)
    causal_links = await deps.repo.research.list_causal_links_by_report(report_id)
    prompt_version = (
        # 按研报时点归因：只认当时已生效的版本，后来的同 md5 版本（回滚再生）不篡改归因
        await deps.repo.research_prompt.get_version_by_md5(
            report.research_prompt_md5, as_of_ts=report.created_at
        )
        if report.research_prompt_md5
        else None
    )
    lines = _format_case_lines(
        report,
        view,
        outcome,
        evidence,
        risks,
        policy_adjustments,
        snapshot_text,
        causal_links,
        prompt_version,
    )
    return "\n".join(lines)


async def _case_outcome(
    deps: ReviewToolDeps, report: Any, view: Any, contract: str
) -> dict[str, Any]:
    """计算案例的客观行情结果；K线来源未装配时以 unavailable 降级。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（用其 candle_source，异步窄协议）
        report: 研报报告行（取 created_at 作窗口起点）
        view: 逐标的结论行（取 horizon 作窗口长度）
        contract: str，合约代码

    返回：
        dict[str, Any]：compute_outcome 的结果字典或不可用占位
    """
    if deps.candle_source is None:
        return {"data_status": "unavailable", "error": "K线来源未配置"}
    return await compute_outcome(contract, report.created_at, view.horizon, deps.candle_source)


def _parse_case_jsons(report: Any, view: Any) -> tuple[list, list, list]:
    """解析案例三段 JSON 字段（调仓记录/依据/风险），坏 JSON 一律降级为空列表。

    参数：
        report: 研报报告行（取 raw_json 中的 policy_adjustments）
        view: 逐标的结论行（取 evidence_json/risks_json）

    返回：
        tuple[list, list, list]：（policy_adjustments, evidence, risks）
    """
    try:
        policy_adjustments = json.loads(report.raw_json).get("policy_adjustments", [])
    except json.JSONDecodeError:
        policy_adjustments = []
    try:
        evidence = json.loads(view.evidence_json)
    except json.JSONDecodeError:
        evidence = []
    try:
        risks = json.loads(view.risks_json)
    except json.JSONDecodeError:
        risks = []
    return policy_adjustments, evidence, risks


async def _case_context_text(deps: ReviewToolDeps, report: Any) -> str:
    """取研报轮上下文快照文本；无关联轮或无快照时返回占位提示。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（用其 repo 取审计轮）
        report: 研报报告行（取 round_id 关联研报轮）

    返回：
        str：截断后的上下文快照文本或占位提示
    """
    audit = await deps.repo.get_audit_round(report.round_id) if report.round_id else None
    if audit is None or not audit.context_snapshot:
        return "（无研报轮上下文快照）"
    return _truncate(audit.context_snapshot, _CASE_SNAPSHOT_LIMIT)


def _format_case_lines(
    report: Any,
    view: Any,
    outcome: dict[str, Any],
    evidence: list,
    risks: list,
    policy_adjustments: list,
    snapshot_text: str,
    causal_links: list,
    prompt_version: Any,
) -> list[str]:
    """把案例材料拼装成展示文本行（纯函数，不读写依赖）。

    参数：
        report: 研报报告行（取报告级 summary/cross_market_view/global_risks_json
            与 research_prompt_md5 归因字段）
        view: 逐标的结论行
        outcome: dict[str, Any]，客观行情结果
        evidence: list，依据列表
        risks: list，风险列表
        policy_adjustments: list，代码归一化调仓记录
        snapshot_text: str，研报轮上下文快照文本
        causal_links: list，当时随研报提交的因果链（CausalLink 行，只读展示）
        prompt_version: 研报提示词版本行（ResearchPromptVersion）或 None；
            由调用方按 report.research_prompt_md5 反解（issue #113 R6）

    返回：
        list[str]：案例材料文本行
    """
    try:
        global_risks = json.loads(report.global_risks_json)
    except (TypeError, ValueError):
        global_risks = []
    md5 = report.research_prompt_md5
    if not md5:
        prompt_text = "（无记录）"
    elif prompt_version is not None:
        prompt_text = f"v{prompt_version.id}（md5 {md5[:8]}…）"
    else:
        prompt_text = f"md5 {md5[:8]}…（未归档版本）"
    lines = [
        f"研报#{report.id}/{view.contract} | round={report.round_id or '—'}"
        f" | 方向={view.direction} | 置信={view.confidence} | horizon={view.horizon}"
        f" | 市场状态={view.market_regime} | 技术确认={view.technical_confirmation}"
        f" | 依据类型={view.basis_type} | 研报时间={_fmt_time(report.created_at)}",
        "报告摘要："
        + (_truncate(report.summary, _CASE_SNAPSHOT_LIMIT) if report.summary else "（无）"),
        "跨市场观察："
        + (
            _truncate(report.cross_market_view, _CASE_SNAPSHOT_LIMIT)
            if report.cross_market_view
            else "（无）"
        ),
        "全局风险："
        + (
            _truncate("；".join(str(r) for r in global_risks), _CASE_SNAPSHOT_LIMIT)
            if global_risks
            else "（无）"
        ),
        f"研报提示词版本：{prompt_text}",
        f"结论正文：{view.narrative or '（空）'}",
        f"依据（共 {len(evidence)} 条，提交复盘时须逐条评价，index 从 0 开始）：",
    ]
    lines += [
        f"  [{i}] {item.get('point', '')}（来源：{item.get('source', '')}）"
        for i, item in enumerate(evidence)
    ] or ["  （无依据记录）"]
    lines.append("风险：" + ("；".join(risks) if risks else "（无）"))
    lines.append("当时市场快照：" + _truncate(view.market_context_json, _CASE_SNAPSHOT_LIMIT))
    lines.append(f"研报轮上下文快照：{snapshot_text}")
    lines.append(
        "代码归一化记录（policy_adjustments）："
        + ("；".join(policy_adjustments) if policy_adjustments else "无")
    )
    lines.append("当时提交的因果链（只读，供核对当时推理方法；链内容对错不在复盘中评价）：")
    lines += [_format_causal_link_line(link) for link in causal_links] or ["  （当时未提交因果链）"]
    lines.append(
        f"客观行情结果（代码计算，仅供批改参考，不可由你提交）：{_format_outcome(outcome)}"
    )
    return lines


def _format_causal_link_line(link: Any) -> str:
    """把一条因果链渲染成单行只读摘要（链 id/主题/节点链/置信度/状态）。

    参数：
        link: CausalLink 行（chain_json 为节点链 JSON，坏 JSON 降级为空链）

    返回：
        str：单行因果链摘要
    """
    try:
        chain = json.loads(link.chain_json)
    except (TypeError, ValueError):
        chain = []
    nodes = " → ".join(str(n.get("node", ""))[:30] for n in chain if isinstance(n, dict))
    return (
        f"  [链#{link.id}][{link.topic or '无主题'}] {nodes or '（空链）'}"
        f"（置信度 {link.confidence}，状态 {link.status}）"
    )


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
    md5_map = await deps.repo.research_review.get_reports_prompt_md5([r.report_id for r in rows])
    return f"共 {len(rows)} 条研报复盘记录（按时间正序）：\n" + "\n".join(
        _format_review_row(r, md5_map.get(r.report_id, "")) for r in rows
    )


async def submit_research_review(deps: ReviewToolDeps, args: dict) -> str:
    """提交对单个逐标的结论的复盘批改（暂存内存草稿，随本轮报告落库生效）。

    校验：须先经 get_research_review_case 读过案例；后端自查案例仍存在、
    horizon 窗口已到期（不依赖已读缓存的陈旧状态）；已被正式复盘过的目标默认
    拒绝重复提交，人工重评须显式传 manual_rereview=true（必须是真布尔值）并用
    rereview_reason 写明理由（理由随工具调用入审计），放行后追加新记录而非覆盖
    原复盘（V6）；方向关系/推理质量/置信度合规为枚举（非法取值拒绝），各自必须
    配独立理由文本；已读案例缓存的客观结果须过 partial_acceptable 门槛——
    complete 放行，partial 须起止价/涨跌幅齐全、覆盖率 ≥80% 且两个价格时点贴近
    窗口端点（缺头/缺尾段的 partial 不放行），pending/unavailable 与不达标
    partial 一律拒绝并留待后续轮次（R1）；数据不足的候选不得闭合为正常复盘，
    仅当复盘方核对案例后确认行情数据不可恢复时，可显式以
    reasoning_quality=unreviewable 结案逃生；outcome 由代码从已读案例缓存附加，
    LLM 携带 outcome 字段一律拒绝；evidence_reviews 与原研报依据强制 1:1（数量
    相等且 evidence_index 不重不漏覆盖 0..N-1），每条须含事实核对与推理支撑双
    枚举及写明核对来源的 explanation。同轮对同一目标重复提交时更新内存草稿。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（读写 loaded_research_cases 与
            pending_research_reviews）
        args: dict，工具参数：report_id/contract/direction_relation/direction_reason/
            reasoning_quality/reasoning_review/evidence_reviews/confidence_assessment/
            confidence_reason/improvement_advice（均必填）、manual_rereview/
            rereview_reason（可选；人工重评开关与理由，后者在开关为 true 时必填）

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
    # 后端自查（不依赖已读缓存的陈旧状态）：案例仍存在、窗口已到期、未被正式复盘
    fresh = await deps.repo.research_review.get_case(report_id, contract)
    if fresh is None:
        return (
            f"参数错误：研报#{report_id}/{contract} 的逐标的结论不存在"
            "（请用 list_research_review_candidates 核对）"
        )
    report, view = fresh
    seconds = HORIZON_SECONDS.get(view.horizon)
    if seconds is None or report.created_at + seconds > time.time():
        return (
            f"参数错误：研报#{report_id}/{contract} 的 horizon 窗口未到期"
            "（或 horizon 非法），暂不可复盘"
        )
    # 人工重评开关必须是真布尔值（防字符串 "true" 蒙混）；已被正式复盘过的目标
    # 默认拒绝，仅显式人工重评放行追加新记录（理由随工具调用入审计，V6）
    manual_rereview = args.get("manual_rereview", False)
    if not isinstance(manual_rereview, bool):
        raise ToolArgError("参数 manual_rereview 必须是布尔值（true/false）")
    if await deps.repo.research_review.has_review(report_id, contract) and not manual_rereview:
        return (
            f"参数错误：研报#{report_id}/{contract} 已被正式复盘批改过，不得重复提交；"
            "确需人工重评（如发现原复盘误判）时，须显式传 manual_rereview=true "
            "并用 rereview_reason 写明理由（重评追加新记录，不覆盖原复盘）"
        )
    rereview_reason = _need_str(args, "rereview_reason") if manual_rereview else ""
    direction_relation = _need_enum(args, "direction_relation", DIRECTION_RELATIONS)
    reasoning_quality = _need_enum(args, "reasoning_quality", REASONING_QUALITIES)
    confidence_assessment = _need_enum(args, "confidence_assessment", CONFIDENCE_ASSESSMENTS)
    outcome = case["outcome"]
    # 数据不足的候选不得闭合为正常复盘；unreviewable 是确认数据不可恢复后的显式逃生口
    if not partial_acceptable(outcome) and reasoning_quality != "unreviewable":
        return (
            f"参数错误：案例客观行情数据不足（data_status={outcome.get('data_status')}，"
            f"完整落窗 K 线 {outcome.get('candles_actual', 0)}/"
            f"{outcome.get('candles_expected', 0)} 根；partial 放行门槛为起止价/涨跌幅"
            f"齐全、覆盖率 ≥{PARTIAL_MIN_COVERAGE_PCT}% 且价格时点贴近窗口端点），"
            "不足以支撑批改；请核对 K 线来源装配或留待后续轮次；"
            "若确认行情数据不可恢复，可以 reasoning_quality=unreviewable 结案"
        )
    evidence_reviews = _parse_evidence_reviews(args)
    direction_reason = _need_str(args, "direction_reason")
    reasoning_review = _need_str(args, "reasoning_review")
    confidence_reason = _need_str(args, "confidence_reason")
    improvement_advice = _need_str(args, "improvement_advice")
    expected = case["evidence_count"]
    evidence_error = _evidence_reviews_error(evidence_reviews, expected)
    if evidence_error is not None:
        return evidence_error
    ordered = sorted(evidence_reviews, key=lambda item: item["evidence_index"])
    existed = key in deps.pending_research_reviews
    deps.pending_research_reviews[key] = {
        "report_id": report_id,
        "contract": contract,
        "direction_relation": direction_relation,
        "direction_reason": direction_reason,
        "reasoning_quality": reasoning_quality,
        "reasoning_review": reasoning_review,
        "evidence_reviews_json": json.dumps(ordered, ensure_ascii=False),
        "confidence_assessment": confidence_assessment,
        "confidence_reason": confidence_reason,
        "improvement_advice": improvement_advice,
        "outcome_json": json.dumps(case["outcome"], ensure_ascii=False),
    }
    verb = "已更新同目标草稿" if existed else "已暂存"
    suffix = f"；人工重评理由（{rereview_reason}）已随调用入审计" if manual_rereview else ""
    return (
        f"研报复盘{verb}：研报#{report_id}/{contract}（依据评价 {len(ordered)}/{expected} 条）"
        f"{suffix}；将随本轮复盘报告落库统一生效，报告失败则自动丢弃"
    )
