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
    market_context: dict[str, Any] | None = None,
) -> ResearchReport:
    """创建当前结构测试报告；失败报告不生成逐标的结论。"""
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
