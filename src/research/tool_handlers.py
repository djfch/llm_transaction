"""研报工具实现：9 只读 + 1 写（submit_causal_links）。

安全不变量：本层无任何交易工具；数据源失败（ResearchSourceError）一律转中文
"数据不可用"哨兵返回给 LLM（不编造数值、不中断本轮）；参数校验失败返回错误文本。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.memory.repo import Repo
from src.research.providers.base import (
    ResearchDataProvider,
    ResearchSourceError,
)
from src.research.providers.jin10 import BEIJING_TZ


class ToolArgError(ValueError):
    """工具参数错误（调用方返回错误文本，不抛异常中断）。"""


@dataclass
class ResearchToolDeps:
    """研报工具依赖：数据聚合器 + 存取层 + 运行模式 + 本轮因果链暂存区。

    pending_causal_links：submit_causal_links 校验通过后的暂存区（H1 修复——
    本轮研报 id 在工具循环结束后才生成，LLM 无法预知）；agent 落研报后由代码
    回填 report_id 批量落库；本轮失败时随 deps 一并丢弃，不会错挂历史研报。
    """

    provider: ResearchDataProvider
    repo: Repo
    mode: str
    pending_causal_links: list[dict] = field(default_factory=list)


def _fmt_ts(ts: float) -> str:
    """时间戳 → 'MM-DD HH:MM'（北京时间）：与数据源头串口径一致，UTC 部署机不偏移。"""
    return datetime.fromtimestamp(ts, tz=BEIJING_TZ).strftime("%m-%d %H:%M")


def _parse_int(args: dict, key: str, default: int, lo: int, hi: int) -> tuple[int, str | None]:
    """解析整数参数：缺失用默认值；非数字/越界返回错误文本（L1 参数容错）。"""
    raw = args.get(key)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 0, f"参数错误：{key} 必须为整数"
    if not lo <= value <= hi:
        return 0, f"参数错误：{key} 须在 {lo}-{hi} 之间"
    return value, None


def _today_markers() -> tuple[str, str]:
    """今日/明日日期串（北京时间 YYYY-MM-DD）：与日历 pub_time 同一时区口径。"""
    now = datetime.now(BEIJING_TZ)
    return now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")


# ---------- 只读工具 ----------


async def fetch_calendar(deps: ResearchToolDeps, args: dict) -> str:
    """日历：今日+明日 star≥3 事件。"""
    today, tomorrow = _today_markers()
    try:
        events = await deps.provider.fetch_calendar()
    except ResearchSourceError as exc:
        return f"日历数据不可用：{exc}"
    rows = [
        e
        for e in events
        if e.star >= 3 and (e.pub_time.startswith(today) or e.pub_time.startswith(tomorrow))
    ]
    if not rows:
        return "今日+明日无高星（star≥3）经济事件"
    lines = ["## 经济日历（今日+明日，star≥3）"]
    for e in rows:
        star = "★" * e.star
        values = f"实际={e.actual or '—'}|预期={e.consensus or '—'}|前值={e.previous or '—'}"
        lines.append(f"- {e.pub_time} [{star}] {e.title}（{e.affect_txt or '未知'}，{values}）")
    return "\n".join(lines)


async def fetch_flash(deps: ResearchToolDeps, args: dict) -> str:
    """全量快讯紧凑文本（时间+标题+摘要）。"""
    hours, err = _parse_int(args, "hours", 24, 1, 48)
    if err:
        return err
    try:
        items = await deps.provider.fetch_flash(hours)
    except ResearchSourceError as exc:
        return f"快讯数据不可用：{exc}"
    if not items:
        return f"近 {hours}h 无快讯"
    lines = [f"## 快讯（近 {hours}h，{len(items)} 条）"]
    for item in items[-200:]:  # 上限 200 条防上下文爆炸
        lines.append(
            f"- [{_fmt_ts(item.published_at)}] [{item.source}] {item.title}：{item.summary[:150]}"
        )
    if len(items) > 200:
        lines.append(f"_（仅显示最近 200 条，共 {len(items)} 条）_")
    return "\n".join(lines)


async def fetch_indicators(deps: ResearchToolDeps, args: dict) -> str:
    """硬数据指标快照。"""
    try:
        return await deps.provider.fetch_indicators()
    except ResearchSourceError as exc:
        return f"指标数据不可用：{exc}"


async def get_macro_series(deps: ResearchToolDeps, args: dict) -> str:
    """FRED 宏观序列。"""
    indicator = str(args.get("indicator") or "").strip()
    if not indicator:
        return "参数错误：indicator 必填（如 cpi / 10y_treasury / m2）"
    look_back, err = _parse_int(args, "look_back", 365, 30, 1825)
    if err:
        return err
    try:
        return await deps.provider.get_macro_series(indicator, look_back)
    except ResearchSourceError as exc:
        return f"FRED 数据不可用：{exc}"


async def get_prediction_markets(deps: ResearchToolDeps, args: dict) -> str:
    """Polymarket 预测概率。"""
    topic = str(args.get("topic") or "").strip()
    if not topic:
        return "参数错误：topic 必填（如 Fed rate cut）"
    limit, err = _parse_int(args, "limit", 6, 1, 10)
    if err:
        return err
    try:
        return await deps.provider.get_prediction_markets(topic, limit)
    except ResearchSourceError as exc:
        return f"Polymarket 数据不可用：{exc}"


async def fetch_article_detail(deps: ResearchToolDeps, args: dict) -> str:
    """单条快讯/文章全文。"""
    item_id = str(args.get("id") or "").strip()
    if not item_id:
        return "参数错误：id 必填"
    try:
        detail = await deps.provider.fetch_article_detail(item_id)
    except ResearchSourceError as exc:
        return f"详情不可用：{exc}"
    return detail or "（该条无全文）"


async def search_news(deps: ResearchToolDeps, args: dict) -> str:
    """关键词检索历史快讯/文章。"""
    keyword = str(args.get("keyword") or "").strip()
    if not keyword:
        return "参数错误：keyword 必填"
    limit, err = _parse_int(args, "limit", 20, 1, 30)
    if err:
        return err
    try:
        items = await deps.provider.search_news(keyword, limit=limit)
    except ResearchSourceError as exc:
        return f"检索不可用：{exc}"
    if not items:
        return f"未找到关键词 {keyword!r} 的相关快讯/文章"
    lines = [f"## 检索结果：{keyword}（{len(items)} 条）"]
    for item in items:
        lines.append(f"- [{_fmt_ts(item.published_at)}] [{item.source}] {item.title}")
    return "\n".join(lines)


async def read_timeline(deps: ResearchToolDeps, args: dict) -> str:
    """事实层近 N 天（客观记录）。"""
    days, err = _parse_int(args, "days", 7, 1, 30)
    if err:
        return err
    limit, err = _parse_int(args, "limit", 200, 1, 500)
    if err:
        return err
    rows = await deps.repo.research.list_timeline(time.time() - days * 86400, limit=limit)
    if not rows:
        return f"近 {days} 天事实层无记录"
    lines = [f"## 事件时间线（近 {days} 天，{len(rows)} 条）"]
    for r in rows:
        lines.append(f"- [{_fmt_ts(r.published_at)}] [{r.source}] {r.title}")
    return "\n".join(lines)


async def read_judgments(deps: ResearchToolDeps, args: dict) -> str:
    """判断层近 N 天（含验证结果与错因，自我纠错输入）。"""
    days, err = _parse_int(args, "days", 7, 1, 30)
    if err:
        return err
    reports = await deps.repo.research.list_reports(days)
    if not reports:
        return f"近 {days} 天无研报记录"
    lines = [f"## 历史研报结论（近 {days} 天，{len(reports)} 条）"]
    for r in reports:
        verify = r.verify_result or "未验证"
        lines.append(
            f"- [{_fmt_ts(r.created_at)}] {r.direction}/{r.confidence}（{r.horizon}）"
            f" 依据：{r.evidence_json[:100]} 验证：{verify}"
        )
    return "\n".join(lines)


# ---------- 写工具 ----------


async def submit_causal_links(deps: ResearchToolDeps, args: dict) -> str:
    """提交链式因果链（唯一写出口）：校验通过后暂存 deps。

    H1 修复：LLM 无需也无法预知本轮研报 id（id 在工具循环结束后落库才生成），
    故 report_id 不再是 LLM 参数——agent 落研报后由代码回填并批量落库；
    本轮研报失败时暂存链随 deps 丢弃，不会错挂历史研报。
    """
    chain = args.get("chain")
    confidence = args.get("confidence")
    if not isinstance(chain, list) or not 2 <= len(chain) <= 6:
        return "参数错误：chain 必须为 2-6 个节点的有序数组"
    for node in chain:
        if not isinstance(node, dict) or not str(node.get("node") or "").strip():
            return "参数错误：chain 每个节点必须是含 node 字段的对象"
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return "参数错误：confidence 必须为 0-1 的数值"
    if not 0 <= confidence <= 1:
        return "参数错误：confidence 须在 0-1 之间"
    evidence = args.get("evidence") or []
    if not isinstance(evidence, list):
        return "参数错误：evidence 必须为字符串数组"
    deps.pending_causal_links.append(
        {
            "chain_json": json.dumps(chain, ensure_ascii=False),
            "confidence": confidence,
            "evidence_json": json.dumps(evidence, ensure_ascii=False),
        }
    )
    nodes = " → ".join(str(n.get("node", ""))[:30] for n in chain)
    return f"因果链已暂存（{len(chain)} 节点）：{nodes}（将随本轮研报自动关联落库）"
