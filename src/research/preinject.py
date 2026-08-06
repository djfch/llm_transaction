"""预注入组装：第一轮 user 消息的五段数据（日历→指标→快讯→时间线→判断史）。

数据源失败段标注不可用（不中断研报轮）；返回 Markdown 文本。
从 tool_handlers 复用 ResearchToolDeps / 时间格式化 / 日期标记（单向依赖，无循环）。
"""

from __future__ import annotations

import json
import time

from src.research.providers.base import (
    KIND_CALENDAR,
    KIND_FLASH,
    SOURCE_JIN10,
    ResearchSourceError,
)
from src.research.tool_handlers import (
    ResearchToolDeps,
    _fmt_ts,
    _today_markers,
)


async def _section_calendar(deps: ResearchToolDeps) -> str:
    """日历段：今日+明日 star≥3；拉取结果先增量写入事实层（代码管辖）。"""
    today, tomorrow = _today_markers()
    try:
        events = await deps.provider.fetch_calendar()
    except ResearchSourceError as exc:
        return f"## 经济日历\n（数据不可用：{exc}）"
    from src.research.providers.jin10 import parse_ts

    items = []
    for e in events:
        items.append(
            {
                "source": SOURCE_JIN10,
                "kind": KIND_CALENDAR,
                "title": e.title,
                "url": "",
                "published_at": parse_ts(e.pub_time),
                "meta_json": json.dumps(
                    {
                        "star": e.star,
                        "actual": e.actual,
                        "consensus": e.consensus,
                        "previous": e.previous,
                        "affect_txt": e.affect_txt,
                    },
                    ensure_ascii=False,
                ),
                "dedup_key": f"{SOURCE_JIN10}|calendar|{e.pub_time}|{e.title[:40]}",
                "fetched_at": time.time(),
            }
        )
    if items:
        await deps.repo.research.append_timeline_many(items)
    rows = [
        e
        for e in events
        if e.star >= 3 and (e.pub_time.startswith(today) or e.pub_time.startswith(tomorrow))
    ]
    if not rows:
        return "## 经济日历\n今日+明日无高星（star≥3）事件"
    lines = [f"## 经济日历（今日+明日，star≥3，{len(rows)} 条）"]
    for e in rows:
        values = f"实际={e.actual or '—'}|预期={e.consensus or '—'}|前值={e.previous or '—'}"
        lines.append(
            f"- {e.pub_time} [{'★' * e.star}] {e.title}（{e.affect_txt or '未知'}，{values}）"
        )
    return "\n".join(lines)


async def _section_indicators(deps: ResearchToolDeps) -> str:
    """指标段：硬数据快照。"""
    try:
        indicators = await deps.provider.fetch_indicators()
    except ResearchSourceError as exc:
        return f"## 指标快照\n（数据不可用：{exc}）"
    return indicators or "## 指标快照\n（空）"


async def _section_flash(deps: ResearchToolDeps, hours: int) -> str:
    """快讯段：全量紧凑，一条不丢；拉取结果先增量写入事实层（代码管辖）。"""
    try:
        items = await deps.provider.fetch_flash(hours)
    except ResearchSourceError as exc:
        return f"## 快讯\n（数据不可用：{exc}）"
    if items:
        await deps.repo.research.append_timeline_many(
            [
                {
                    "source": item.source,
                    "kind": KIND_FLASH,
                    "title": item.title,
                    "url": item.url,
                    "published_at": item.published_at,
                    "meta_json": "{}",
                    "dedup_key": f"{item.source}|{item.published_at}|{item.title[:40]}",
                    "fetched_at": time.time(),
                }
                for item in items
            ]
        )
    lines = [f"## 快讯（近 {hours}h 全量，{len(items)} 条，时间+标题+摘要）"]
    for item in items:
        lines.append(
            f"- [{_fmt_ts(item.published_at)}] [{item.source}] {item.title}：{item.summary[:150]}"
        )
    return "\n".join(lines)


async def _section_timeline(deps: ResearchToolDeps, hours: int = 24) -> str:
    """时间线段：事实层近 7 天（排除本次拉取窗口，避免与快讯段重复）。"""
    rows = await deps.repo.research.list_timeline(
        time.time() - 7 * 86400, end_ts=time.time() - hours * 3600, limit=300
    )
    if not rows:
        return "## 事件时间线\n（暂无记录）"
    lines = [f"## 事件时间线（近 7 天，{len(rows)} 条）"]
    for r in rows:
        lines.append(f"- [{_fmt_ts(r.published_at)}] [{r.source}] {r.title}")
    return "\n".join(lines)


async def _section_judgments(deps: ResearchToolDeps) -> str:
    """判断史段：近 7 天研报结论（含验证结果）。"""
    reports = await deps.repo.research.list_reports(7)
    if not reports:
        return "## 历史研报结论\n（暂无记录，这是你的首次研报）"
    lines = [f"## 历史研报结论（近 7 天，{len(reports)} 条，含验证结果）"]
    for r in reports:
        verify = r.verify_result or "未验证"
        lines.append(
            f"- [{_fmt_ts(r.created_at)}] {r.direction}/{r.confidence}（{r.horizon}）"
            f" 依据：{r.evidence_json[:80]} 验证：{verify}"
        )
    return "\n".join(lines)


async def build_preinjection(deps: ResearchToolDeps, hours: int = 24) -> str:
    """组装第一轮 user 消息的预注入数据段（时间→日历→指标→快讯→时间线→判断史）。

    日历与快讯拉取结果先增量写入事实层（timeline，代码管辖、LLM 零写权限）；
    首段标注当前本地时间（M12：给 LLM 提供时间锚点，防臆测未来数据）。
    """
    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    return "\n\n".join(
        [
            f"## 当前时间\n{now}（本地时间，所有分析以此为准）",
            await _section_calendar(deps),
            await _section_indicators(deps),
            await _section_flash(deps, hours),
            await _section_timeline(deps, hours),
            await _section_judgments(deps),
        ]
    )
