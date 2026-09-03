"""预注入组装：冻结白名单与八类研报上下文（时间→日历→指标→快讯→时间线→判断史→因果链→复盘记录）。

数据源失败段标注不可用（不中断研报轮）；返回 Markdown 文本。
从 tool_handlers 复用 ResearchToolDeps / 时间格式化 / 日期标记（单向依赖，无循环）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from src.memory.models import ResearchReview
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


_REVIEW_ENTRY_MAX_CHARS = 2000


def _truncate_entry(text: str, max_chars: int) -> str:
    """超长截断并标注原文长度（与复盘层同口径；本包自包含，不 import src/review/*）。

    参数：
        text: str，待处理的文本
        max_chars: int，允许保留的最大字符数

    返回：
        str：未超限时原样返回；超限时截断并追加"…（已截断，原文共 N 字符）"
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…（已截断，原文共 {len(text)} 字符）"


def _format_review_outcome(outcome: dict[str, Any]) -> str:
    """把复盘客观行情结果字典渲染成一行摘要（与复盘层口径一致的本地实现）。

    参数：
        outcome: dict[str, Any]，复盘记录 outcome_json 解析后的字典

    返回：
        str：一行客观结果摘要；无价格数据时只呈现状态与说明
    """
    status = outcome.get("data_status", "unknown")
    if outcome.get("start_price") is None:
        error = outcome.get("error") or ""
        return f"data_status={status}（{error or '无价格数据'}）"
    head = (
        f"data_status={status}"
        f"（K线 {outcome.get('candles_actual')}/{outcome.get('candles_expected')}）"
        f" | 起价 {outcome['start_price']}"
    )
    tail = (
        f" | 区间最高 {outcome.get('high')}（{outcome.get('max_up_pct')}%）"
        f" | 区间最低 {outcome.get('low')}（{outcome.get('max_down_pct')}%）"
    )
    if outcome.get("end_price") is None:
        # 窗口末端无完整 K 线：止价与涨跌幅缺失，只呈现起价与区间高低
        return f"{head} → {outcome.get('error') or '止价缺失'}{tail}"
    return f"{head} → 止价 {outcome['end_price']} | 涨跌 {outcome.get('return_pct')}%{tail}"


def _format_review_entry(review: ResearchReview) -> str:
    """把单条复盘记录渲染为多行完整文本（枚举+理由+逐项依据核对+改进建议+客观结果）。

    空值字段整行跳过；坏 JSON 字段降级为空，不拖垮整段预注入。
    首行主标识为复盘记录自身编号（复盘#{id}），与复盘工具历史查询
    （tool_research._format_review_row）同命名空间，避免同一数字在「复盘记录」
    与「复盘报告」两个序列间错配；复盘报告编号仅作附带标注（R7-3）。

    参数：
        review: ResearchReview，单条正式复盘批改记录

    返回：
        str：多行完整复盘文本（截断由调用方 _truncate_entry 统一负责）
    """
    lines = [
        f"- [{_fmt_ts(review.created_at)}] 复盘#{review.id}"
        f"（复盘报告#{review.review_report_id}） → "
        f"研报#{review.report_id}/{review.contract}"
        + (
            f"（人工重评，替代复盘#{review.rereview_of_id or '—'}；"
            f"授权理由：{review.rereview_reason}）"
            if review.review_kind == "manual"
            else ""
        )
        + "："
    ]
    for label, value, reason in (
        ("方向关系", review.direction_relation, review.direction_reason),
        ("推理质量", review.reasoning_quality, review.reasoning_review),
        ("置信度合规", review.confidence_assessment, review.confidence_reason),
    ):
        if value:
            lines.append(f"  {label}：{value}" + (f" —— {reason}" if reason else ""))
    try:
        evidence = json.loads(review.evidence_reviews_json)
    except (TypeError, ValueError):
        evidence = []
    if isinstance(evidence, list) and evidence:
        lines.append(
            "  依据评价："
            + "；".join(
                f"[{e.get('evidence_index')}] 事实={e.get('fact_status')}"
                f" 推理={e.get('reasoning_status')}：{e.get('explanation')}"
                for e in evidence
                if isinstance(e, dict)
            )
        )
    if review.improvement_advice:
        lines.append(f"  改进建议：{review.improvement_advice}")
    try:
        outcome = json.loads(review.outcome_json)
    except (TypeError, ValueError):
        outcome = {}
    if isinstance(outcome, dict) and outcome:
        lines.append(f"  客观结果：{_format_review_outcome(outcome)}")
    return "\n".join(lines)


async def _section_recent_reviews(deps: ResearchToolDeps) -> str:
    """近期研报复盘记录段：最近 20 条正式复盘批改完整记录，按时间正序、单条超限截断。

    不依附原研报是否在近 7 天窗口；同一研报被多次复盘时全部保留，
    供研报 agent 识别反复出现的偏差（复盘记录是历史反馈，不是方向信号）。
    每条渲染全部评价维度（枚举+理由）、逐项依据核对与客观结果，让研报
    agent 能看到"为什么被这样批改"，而非只看到一个结论枚举。

    参数：
        deps: ResearchToolDeps，提供研报复盘仓库的共享依赖

    返回：
        str，近期复盘记录 Markdown 段落；无记录时返回空态说明
    """
    reviews = await deps.repo.research_review.list_reviews(limit=20)
    if not reviews:
        return "## 近期研报复盘记录\n（暂无）"
    lines = [
        f"## 近期研报复盘记录（最近 {len(reviews)} 条，按时间正序；"
        f"每条超 {_REVIEW_ENTRY_MAX_CHARS} 字符截断）"
    ]
    for review in reviews:
        lines.append(_truncate_entry(_format_review_entry(review), _REVIEW_ENTRY_MAX_CHARS))
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
