"""研报最终 payload 持久化：原子保存逐标的结论与市场快照。"""

from __future__ import annotations

import json
from typing import Any

from src.memory.models import ResearchReport
from src.memory.repo import Repo
from src.research.tool_handlers import ResearchToolDeps


def _asset_records(payload: dict, deps: ResearchToolDeps) -> list[dict]:
    records: list[dict] = []
    for view in payload["asset_views"]:
        snapshot = deps.market_snapshots[view["contract"]]
        records.append(
            {
                "contract": view["contract"],
                "direction": view["direction"],
                "confidence": view["confidence"],
                "horizon": view["horizon"],
                "market_regime": view["market_regime"],
                "technical_confirmation": view["technical_confirmation"],
                "basis_type": view["basis_type"],
                "data_status": snapshot.get("data_status", "不可用"),
                "evidence_json": json.dumps(view["evidence"], ensure_ascii=False),
                "risks_json": json.dumps(view["risks"], ensure_ascii=False),
                "narrative": view["narrative"],
                "market_context_json": json.dumps(snapshot, ensure_ascii=False),
            }
        )
    return records


async def persist_payload(
    repo: Repo,
    *,
    report_type: str,
    payload: dict,
    round_id: str,
    deps: ResearchToolDeps,
) -> tuple[ResearchReport, int]:
    """原子保存当前逐标的研报结构。"""
    report, views = await repo.research.save_report_bundle(
        report_type=report_type,
        summary=payload["summary"],
        cross_market_view=payload["cross_market_view"],
        global_risks_json=json.dumps(payload["global_risks"], ensure_ascii=False),
        raw_json=json.dumps(payload, ensure_ascii=False),
        round_id=round_id,
        asset_views=_asset_records(payload, deps),
    )
    return report, len(views)


def success_result(report: ResearchReport, round_id: str, asset_count: int) -> dict[str, Any]:
    return {"ok": True, "report_id": report.id, "round_id": round_id, "asset_count": asset_count}
