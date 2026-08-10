"""研报判断史渲染：按报告与合约分组。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from src.memory.models import ResearchAssetView, ResearchReport


class _ResearchRepo(Protocol):
    async def list_asset_views_by_report(self, report_id: int) -> list[ResearchAssetView]: ...


def _fmt_ts(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


async def render_judgments(
    repo: _ResearchRepo,
    reports: Sequence[ResearchReport],
    title: str,
) -> str:
    """渲染判断史；报告头与其逐标的结论保持相邻。"""
    lines = [title]
    for report in reports:
        lines.append(
            f"- [{_fmt_ts(report.created_at)}] 报告#{report.id}：{report.summary or '无总览'}"
        )
        views = await repo.list_asset_views_by_report(report.id)
        lines.extend(_asset_lines(views))
    return "\n".join(lines)


def _asset_lines(views: Sequence[ResearchAssetView]) -> list[str]:
    lines = []
    for view in views:
        verify = view.verify_result or "未验证"
        lines.append(
            f"  - {view.contract}：{view.direction}/{view.confidence}（{view.horizon}）"
            f" 结构={view.market_regime} 依据={view.basis_type} "
            f"技术={view.technical_confirmation} 数据={view.data_status} 验证={verify}"
        )
    return lines
