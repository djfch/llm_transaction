"""预注入组装：冻结白名单与八类研报上下文（时间→日历→指标→快讯→时间线→判断史→因果链→复盘记录）。

数据源失败段标注不可用（不中断研报轮）；返回 Markdown 文本。
从 tool_handlers 复用 ResearchToolDeps / 时间格式化 / 日期标记（单向依赖，无循环）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from src.research.judgments import render_judgments

from src.research.providers.base import (
    KIND_CALENDAR,
    KIND_FLASH,
    SOURCE_JIN10,
    ResearchSourceError,
)
from src.research.providers.jin10 import BEIJING_TZ, parse_ts
from src.research.tool_handlers import (
    ResearchToolDeps,
    _fmt_ts,
    _today_markers,
)


async def _section_calendar(deps: ResearchToolDeps) -> str:
    """拉取经济日历并增量写入事实层，再渲染今日与明日的高星事件段落。

    参数：
        deps: ResearchToolDeps，提供研报数据源与事实仓库的共享依赖

    返回：
        str，经济日历 Markdown 段落；数据源失败时包含不可用原因
    """
    today, tomorrow = _today_markers()
    try:
        events = await deps.provider.fetch_calendar()
    except ResearchSourceError as exc:
        return f"## 经济日历\n（数据不可用：{exc}）"
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
    """拉取宏观与加密指标硬数据并渲染指标快照段落。

    参数：
        deps: ResearchToolDeps，提供研报数据源的共享依赖

    返回：
        str，指标快照文本；数据源失败或无内容时返回对应降级说明
    """
    try:
        indicators = await deps.provider.fetch_indicators()
    except ResearchSourceError as exc:
        return f"## 指标快照\n（数据不可用：{exc}）"
    return indicators or "## 指标快照\n（空）"


async def _section_flash(deps: ResearchToolDeps, hours: int) -> str:
    """拉取指定窗口的全部快讯、增量写入事实层并渲染紧凑快讯段落。

    参数：
        deps: ResearchToolDeps，提供研报数据源与事实仓库的共享依赖
        hours: int，快讯回看窗口小时数

    返回：
        str，包含时间、来源、标题与摘要的快讯 Markdown 段落
    """
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
                    # dedup_key 时间戳按秒取整：与聚合器内存去重键同口径（B 复审发现修复）
                    "dedup_key": f"{item.source}|{int(item.published_at)}|{item.title[:40]}",
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
    """读取近七天且早于本次快讯窗口的历史事实并渲染事件时间线。

    参数：
        deps: ResearchToolDeps，提供事实仓库的共享依赖
        hours: int，本轮快讯窗口小时数，用于排除重复时间段

    返回：
        str，按时间展示的事件时间线段落；无记录时返回空状态说明
    """
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
    """读取近七天研报并按报告与合约渲染历史判断段落。

    参数：
        deps: ResearchToolDeps，提供研报仓库的共享依赖

    返回：
        str，历史研报结论段落；无历史时返回首次研报提示
    """
    reports = await deps.repo.research.list_reports(7)
    if not reports:
        return "## 历史研报结论\n（暂无记录，这是你的首次研报）"
    title = f"## 历史研报结论（近 7 天，{len(reports)} 份）"
    return await render_judgments(deps.repo.research, reports, title)


async def _section_pending_links(deps: ResearchToolDeps) -> str:
    """待跟踪因果链段：前 10 条待跟踪当前版，带链 id 供 supersedes_id 引用。

    不按时间淘汰（事件发展需要时间）；提示 LLM 事件有新进展时提交修正版。

    参数：
        deps: ResearchToolDeps，提供因果链仓库的共享依赖

    返回：
        str，最多十条待跟踪当前版因果链及其编号的 Markdown 段落
    """
    links = await deps.repo.research.list_pending_causal_links(limit=10)
    if not links:
        return "## 待跟踪因果链\n（暂无）"
    lines = [
        "## 待跟踪因果链"
        f"（前 {len(links)} 条，跟踪中；事件有新进展请提交修正版并声明 supersedes_id）"
    ]
    for link in links:
        try:
            chain = json.loads(link.chain_json)
        except (TypeError, ValueError):
            chain = []
        nodes = " → ".join(str(n.get("node", ""))[:30] for n in chain if isinstance(n, dict))
        lines.append(
            f"- [链#{link.id}][{link.topic or '无主题'}] {nodes}（置信度 {link.confidence}）"
        )
    return "\n".join(lines)


async def _section_recent_reviews(deps: ResearchToolDeps) -> str:
    """近期研报复盘记录段：最近 20 条正式复盘批改，按时间正序。

    不依附原研报是否在近 7 天窗口；同一研报被多次复盘时全部保留，
    供研报 agent 识别反复出现的偏差（复盘记录是历史反馈，不是方向信号）。

    参数：
        deps: ResearchToolDeps，提供研报复盘仓库的共享依赖

    返回：
        str，近期复盘记录 Markdown 段落；无记录时返回空态说明
    """
    reviews = await deps.repo.research_review.list_reviews(limit=20)
    if not reviews:
        return "## 近期研报复盘记录\n（暂无）"
    lines = [f"## 近期研报复盘记录（最近 {len(reviews)} 条，按时间正序）"]
    for r in reviews:
        lines.append(
            f"- [{_fmt_ts(r.created_at)}] 复盘#{r.review_report_id} → "
            f"研报#{r.report_id}/{r.contract}：方向关系={r.direction_relation or '未评'} "
            f"推理质量={r.reasoning_quality or '未评'}"
        )
        if r.confidence_assessment:
            lines.append(f"  置信度合规：{r.confidence_assessment}")
        if r.improvement_advice:
            lines.append(f"  改进建议：{r.improvement_advice}")
    return "\n".join(lines)


def _section_watchlist(deps: ResearchToolDeps) -> str:
    """渲染本轮已冻结合约白名单与逐标的市场工具调用约束。

    参数：
        deps: ResearchToolDeps，包含本轮合约白名单快照的共享依赖

    返回：
        str，本轮白名单及工具调用要求的 Markdown 段落
    """
    contracts = list(deps.watchlist_snapshot)
    if not contracts:
        return "## 本轮白名单\n（空；本轮不得生成逐标的结论）"
    listed = "\n".join(f"- {contract}" for contract in contracts)
    return (
        "## 本轮白名单（已冻结）\n"
        + listed
        + "\n必须对以上每个合约恰好调用一次 get_research_market_data。"
    )


async def build_preinjection(deps: ResearchToolDeps, hours: int = 24) -> str:
    """组装第一轮 user 消息的预注入数据段（时间→日历→指标→快讯→时间线→判断史→待跟踪因果链→复盘记录）。

    日历与快讯拉取结果先增量写入事实层（timeline，代码管辖、LLM 零写权限）；
    首段标注当前北京时间（M12：给 LLM 提供时间锚点，防臆测未来数据；与数据源
    时间串同一时区口径，UTC 部署机不偏移）。

    参数：
        deps: ResearchToolDeps，研报预注入所需数据源、仓库与白名单依赖
        hours: int，本轮快讯与去重窗口小时数

    返回：
        str，按固定顺序拼接的完整研报首轮用户消息数据段
    """
    now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    return "\n\n".join(
        [
            f"## 当前时间\n{now}（北京时间，所有分析以此为准）",
            _section_watchlist(deps),
            await _section_calendar(deps),
            await _section_indicators(deps),
            await _section_flash(deps, hours),
            await _section_timeline(deps, hours),
            await _section_judgments(deps),
            await _section_pending_links(deps),
            await _section_recent_reviews(deps),
        ]
    )
