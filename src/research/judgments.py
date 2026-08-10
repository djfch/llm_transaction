"""研报判断史渲染：按报告与合约分组。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from src.memory.models import ResearchAssetView, ResearchReport


class _ResearchRepo(Protocol):
    async def list_asset_views_by_report(self, report_id: int) -> list[ResearchAssetView]:
        """按报告 id 读取该次研报的全部逐标的结论。

        参数：
            report_id: int，研报报告头 id

        返回：
            list[ResearchAssetView]：该报告下每个合约的结论视图，按写入顺序排列
        """
        ...


def _fmt_ts(ts: float) -> str:
    """把 Unix 时间戳格式化为「月-日 时:分」的本地时间字符串。

    参数：
        ts: float，Unix 时间戳（秒）

    返回：
        str：形如 "MM-DD HH:MM" 的本地时间字符串，用于判断史行首的时间标注
    """
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


async def render_judgments(
    repo: _ResearchRepo,
    reports: Sequence[ResearchReport],
    title: str,
) -> str:
    """渲染判断史；报告头与其逐标的结论保持相邻。

    参数：
        repo: _ResearchRepo，数据仓储
        reports: Sequence[ResearchReport]，待渲染的研报序列
        title: str，判断史标题

    返回：
        str，报告头与逐标的判断相邻排列的 Markdown 文本
    """
    lines = [title]
    for report in reports:
        lines.append(
            f"- [{_fmt_ts(report.created_at)}] 报告#{report.id}：{report.summary or '无总览'}"
        )
        views = await repo.list_asset_views_by_report(report.id)
        lines.extend(_asset_lines(views))
    return "\n".join(lines)


def _asset_lines(views: Sequence[ResearchAssetView]) -> list[str]:
    """把一份报告下每个合约的结论视图渲染成一行判断史文本。

    参数：
        views: Sequence[ResearchAssetView]，某次研报的逐标的结论视图列表

    返回：
        list[str]：每个合约一行的缩进文本，含方向/信心/持有周期/市场结构/依据类型/
            技术确认/数据状态/复盘验证结果；验证结果为空时显示「未验证」
    """
    lines = []
    for view in views:
        verify = view.verify_result or "未验证"
        lines.append(
            f"  - {view.contract}：{view.direction}/{view.confidence}（{view.horizon}）"
            f" 结构={view.market_regime} 依据={view.basis_type} "
            f"技术={view.technical_confirmation} 数据={view.data_status} 验证={verify}"
        )
    return lines
