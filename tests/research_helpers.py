"""研报测试夹具：按当前逐标的结构创建成功或失败报告。"""

from __future__ import annotations

import json
from typing import Any

from src.memory import Repo
from src.memory.models import ResearchReport


async def save_report_fixture(
    repo: Repo,
    *,
    report_type: str,
    contract: str = "BTC_USDT",
    direction: str = "中性",
    confidence: str = "低",
    horizon: str = "当日",
    market_regime: str = "震荡",
    technical_confirmation: str = "中性",
    basis_type: str = "混合",
    data_status: str = "完整",
    evidence_json: str = "[]",
    risks_json: str = "[]",
    narrative: str = "",
    raw_json: str = "{}",
    summary: str = "",
    cross_market_view: str = "",
    global_risks_json: str = "[]",
    error: str = "",
    round_id: str = "",
    research_prompt_md5: str = "",
    market_context: dict[str, Any] | None = None,
) -> ResearchReport:
    """创建当前结构测试报告；失败报告不生成逐标的结论。

    参数：
        repo: Repo，研报仓储对象
        report_type: str，研报类型
        contract: str，合约名称，默认 BTC_USDT
        direction: str，方向结论
        confidence: str，结论置信度
        horizon: str，结论时间范围
        market_regime: str，市场状态判断
        technical_confirmation: str，技术面确认结论
        basis_type: str，证据基础类型
        data_status: str，输入数据完整状态
        evidence_json: str，证据列表 JSON 文本
        risks_json: str，风险列表 JSON 文本
        narrative: str，逐标的分析正文
        raw_json: str，研报原始 JSON 文本
        summary: str，研报摘要
        cross_market_view: str，跨市场观点
        global_risks_json: str，全局风险 JSON 文本
        error: str，研报生成错误文本
        round_id: str，关联审计轮次编号
        research_prompt_md5: str，生成本研报所用的 research_prompt.md 正文 md5
        market_context: dict[str, Any] | None，生成研报时的市场上下文快照

    返回：
        ResearchReport：新建的成功或失败研报记录
    """
    if error:
        return await repo.research.save_failed_report(
            report_type=report_type,
            error=error,
            raw_json=raw_json,
            round_id=round_id,
        )
    report, _ = await repo.research.save_report_bundle(
        report_type=report_type,
        summary=summary or narrative,
        cross_market_view=cross_market_view,
        global_risks_json=global_risks_json,
        raw_json=raw_json,
        round_id=round_id,
        research_prompt_md5=research_prompt_md5,
        asset_views=[
            {
                "contract": contract,
                "direction": direction,
                "confidence": confidence,
                "horizon": horizon,
                "market_regime": market_regime,
                "technical_confirmation": technical_confirmation,
                "basis_type": basis_type,
                "data_status": data_status,
                "evidence_json": evidence_json,
                "risks_json": risks_json,
                "narrative": narrative,
                "market_context_json": json.dumps(
                    market_context or {"contract": contract},
                    ensure_ascii=False,
                ),
            }
        ],
    )
    return report
