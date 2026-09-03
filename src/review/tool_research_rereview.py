"""人工授权重评的提交分派（issue #113 R5-2）：submit_research_review 的注册表入口。

分派口径：目标未被正式复盘过 → 走 tool_research 的自动复盘路径（原样）；
已被正式复盘过 → 必须命中未消费的人工重评授权（research_rereview_requests，
由人工在研报详情页经 POST /api/review/research/rereview 登记）才放行追加一条
review_kind='manual' 的批改记录，否则维持拒绝。授权分支允许以
reasoning_quality=unreviewable 结案（此时强制 direction_relation=unverifiable、
confidence_assessment=unreviewable，三个理由文本照常入库）；授权在批改随复盘
报告落库的同一事务里被消费并绑定 round_id（见 ReviewRepo.save_review_bundle）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.memory.models import ResearchRereviewRequest
from src.research.payload_v2 import HORIZON_SECONDS
from src.review import tool_research
from src.review.research_outcome import (
    PARTIAL_MIN_COVERAGE_PCT,
    compute_outcome,
    partial_acceptable,
)
from src.review.tool_handlers import (
    ReviewToolDeps,
    _need_str,
    _to_int,
)
from src.review.tool_research import (
    CONFIDENCE_ASSESSMENTS,
    DIRECTION_RELATIONS,
    REASONING_QUALITIES,
    _evidence_reviews_error,
    _need_enum,
    _parse_evidence_reviews,
)


async def submit_research_review(deps: ReviewToolDeps, args: dict) -> str:
    """提交复盘批改的分派入口：未复盘目标走自动路径，已复盘目标须命中人工授权。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        args: dict，工具参数（协议同 tool_research.submit_research_review）

    返回：
        str：提交结果文本；无授权重评或校验失败返回具体原因且不落草稿

    异常：
        ToolArgError：report_id/contract 参数结构非法时抛出（注册表转错误文本）
    """
    report_id = _to_int(args.get("report_id"), "report_id")
    contract = _need_str(args, "contract")
    if not await deps.repo.research_review.has_review(report_id, contract):
        return await tool_research.submit_research_review(deps, args)
    request = await deps.repo.research_review.get_pending_rereview_request(report_id, contract)
    if request is None:
        return (
            f"参数错误：研报#{report_id}/{contract} 已被正式复盘批改过；如需重评，"
            "须由人工在研报详情页发起重评授权，之后在本轮提交"
        )
    return await _submit_authorized_rereview(deps, args, request)


def _check_rereview_closure(
    direction_relation: str, reasoning_quality: str, confidence_assessment: str
) -> str | None:
    """授权重评的 unreviewable 结案约束：三枚举必须一致降级（理由文本照常入库）。

    参数：
        direction_relation: str，方向关系枚举值
        reasoning_quality: str，推理质量枚举值
        confidence_assessment: str，置信度合规枚举值

    返回：
        str | None：违反结案约束时返回错误文本，通过时返回 None
    """
    if reasoning_quality != "unreviewable":
        return None
    if direction_relation != "unverifiable" or confidence_assessment != "unreviewable":
        return (
            "参数错误：以 reasoning_quality=unreviewable 结案时，direction_relation 必须取 "
            "unverifiable、confidence_assessment 必须取 unreviewable，"
            "并在各自理由文本中写明数据缺口"
        )
    return None


async def _rereview_outcome(
    deps: ReviewToolDeps, report: Any, view: Any, contract: str, reasoning_quality: str
) -> tuple[dict[str, Any], str | None]:
    """授权重评的客观结果：提交时点尽力重算；非结案重评仍受数据门槛约束。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（用其 candle_source 重算客观结果）
        report: 研报报告行（取 created_at 作窗口起点）
        view: 逐标的结论行（取 horizon 作窗口长度）
        contract: str，合约代码
        reasoning_quality: str，推理质量枚举值（unreviewable 结案不受数据门槛限制）

    返回：
        tuple[dict, str | None]：（客观结果字典，错误文本）；通过时错误文本为 None
    """
    if deps.candle_source is None:
        outcome: dict[str, Any] = {"data_status": "unavailable", "error": "K线来源未配置"}
    else:
        outcome = await compute_outcome(
            contract, report.created_at, view.horizon, deps.candle_source
        )
    if reasoning_quality == "unreviewable":
        return outcome, None  # 结案语义即数据不足，不套提交门槛
    if deps.candle_source is None:
        return outcome, "参数错误：K 线来源未装配，无法核算客观行情，请核对装配后留待后续轮次"
    if not partial_acceptable(outcome):
        return outcome, (
            f"参数错误：案例客观行情数据不足（data_status={outcome.get('data_status')}，"
            f"完整落窗 K 线 {outcome.get('candles_actual', 0)}/"
            f"{outcome.get('candles_expected', 0)} 根；partial 放行门槛为起止价/涨跌幅"
            f"齐全、覆盖率 ≥{PARTIAL_MIN_COVERAGE_PCT}% 且价格时点贴近窗口端点）；"
            "非结案的重评仍需数据支撑，数据不足时请改用 unreviewable 结案口径"
        )
    return outcome, None


async def _submit_authorized_rereview(
    deps: ReviewToolDeps, args: dict, request: ResearchRereviewRequest
) -> str:
    """命中人工授权的重评提交：校验通过后暂存 manual 草稿（随本轮报告落库生效）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（读 loaded_research_cases，写
            pending_research_reviews）
        args: dict，工具参数（评价协议同自动路径）
        request: ResearchRereviewRequest，命中的未消费人工重评授权

    返回：
        str：提交结果文本；校验失败返回具体原因且不落草稿

    异常：
        ToolArgError：枚举或依据评价参数结构非法时抛出（注册表转错误文本）
    """
    report_id, contract = request.report_id, request.contract
    key = (report_id, contract)
    case = deps.loaded_research_cases.get(key)
    if case is None:
        return (
            f"参数错误：请先用 get_research_review_case 读取研报#{report_id}/{contract} "
            "的案例材料后再提交批改"
        )
    if "outcome" in args:
        return "参数错误：outcome（客观行情结果）由代码计算附加，不允许提交该字段"
    fresh = await deps.repo.research_review.get_case(report_id, contract)
    if fresh is None:
        return f"参数错误：研报#{report_id}/{contract} 的逐标的结论已不存在"
    report, view = fresh
    seconds = HORIZON_SECONDS.get(view.horizon)
    if seconds is None or report.created_at + seconds > time.time():
        return f"参数错误：研报#{report_id}/{contract} 的 horizon 窗口未到期（或 horizon 非法）"
    direction_relation = _need_enum(args, "direction_relation", DIRECTION_RELATIONS)
    reasoning_quality = _need_enum(args, "reasoning_quality", REASONING_QUALITIES)
    confidence_assessment = _need_enum(args, "confidence_assessment", CONFIDENCE_ASSESSMENTS)
    closure_error = _check_rereview_closure(
        direction_relation, reasoning_quality, confidence_assessment
    )
    if closure_error is not None:
        return closure_error
    outcome, outcome_error = await _rereview_outcome(
        deps, report, view, contract, reasoning_quality
    )
    if outcome_error is not None:
        return outcome_error
    return await _stage_rereview_draft(
        deps,
        args,
        request,
        direction_relation,
        reasoning_quality,
        confidence_assessment,
        outcome,
        case["evidence_count"],
    )


async def _stage_rereview_draft(
    deps: ReviewToolDeps,
    args: dict,
    request: ResearchRereviewRequest,
    direction_relation: str,
    reasoning_quality: str,
    confidence_assessment: str,
    outcome: dict[str, Any],
    expected_evidence: int,
) -> str:
    """校验理由与依据评价后暂存授权重评草稿（带授权身份与替代指向）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（写 pending_research_reviews）
        args: dict，工具参数（取理由文本与依据评价列表）
        request: ResearchRereviewRequest，命中的未消费人工重评授权
        direction_relation: str，已校验的方向关系枚举值
        reasoning_quality: str，已校验的推理质量枚举值
        confidence_assessment: str，已校验的置信度合规枚举值
        outcome: dict，提交时点重算的客观行情结果
        expected_evidence: int，原研报依据条数（1:1 校验基准）

    返回：
        str：提交结果文本；依据评价 1:1 校验失败返回错误文本且不落草稿

    异常：
        ToolArgError：理由文本缺失或依据评价结构非法时抛出（注册表转错误文本）
    """
    report_id, contract = request.report_id, request.contract
    key = (report_id, contract)
    evidence_reviews = _parse_evidence_reviews(args)
    direction_reason = _need_str(args, "direction_reason")
    reasoning_review = _need_str(args, "reasoning_review")
    confidence_reason = _need_str(args, "confidence_reason")
    improvement_advice = _need_str(args, "improvement_advice")
    evidence_error = _evidence_reviews_error(evidence_reviews, expected_evidence)
    if evidence_error is not None:
        return evidence_error
    ordered = sorted(evidence_reviews, key=lambda item: item["evidence_index"])
    previous_id = await deps.repo.research_review.latest_review_id(report_id, contract)
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
        "outcome_json": json.dumps(outcome, ensure_ascii=False),
        "review_kind": "manual",
        "rereview_reason": request.reason,
        "rereview_of_id": previous_id,
        "rereview_request_id": request.id,
    }
    verb = "已更新同目标草稿" if existed else "已暂存"
    return (
        f"人工授权重评{verb}：研报#{report_id}/{contract}（授权#{request.id}，"
        f"依据评价 {len(ordered)}/{expected_evidence} 条）；将随本轮复盘报告落库统一生效，"
        "生效后该授权即被消费并绑定本轮次"
    )
