"""研报最终 payload 持久化：原子保存逐标的结论与市场快照。"""

from __future__ import annotations

import json
from typing import Any

from src.memory.models import ResearchReport
from src.memory.repo import Repo
from src.research.tool_handlers import ResearchToolDeps


def _asset_records(payload: dict, deps: ResearchToolDeps) -> list[dict]:
    """把研报 payload 中的逐标的结论与对应市场快照拼成落库记录列表。

    参数：
        payload: dict，研报最终 payload（含 asset_views 逐标的结论列表）
        deps: ResearchToolDeps，研报工具依赖（用其 market_snapshots 取各标的市场快照）

    返回：
        list[dict]：逐标的落库记录列表，证据/风险/市场上下文已序列化为 JSON 字符串，
        供 save_report_bundle 原子写入
    """
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
    """原子保存当前逐标的研报结构。

    参数：
        repo: Repo，研报持久化仓储
        report_type: str，研报盘口类型
        payload: dict，待广播、保存或转换的数据载荷
        round_id: str，关联的审计轮次编号
        deps: ResearchToolDeps，当前模块所需的运行依赖集合

    返回：
        tuple[ResearchReport, int]：原子保存当前逐标的研报结构
    """
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
    """组装研报运行成功的返回结果。

    参数：
        report: ResearchReport，已落库的研报记录（取 id 作为 report_id）
        round_id: str，本轮研报的轮次标识
        asset_count: int，本次落库的逐标的结论数量

    返回：
        dict[str, Any]：成功结果字典（ok/report_id/round_id/asset_count），由 agent 返回给调用方
    """
    return {"ok": True, "report_id": report.id, "round_id": round_id, "asset_count": asset_count}
