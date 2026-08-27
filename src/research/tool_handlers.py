"""研报工具实现：11 只读 + 1 写（submit_causal_links）。

安全不变量：本层无任何交易工具；数据源失败（ResearchSourceError）一律转中文
"数据不可用"哨兵返回给 LLM（不编造数值、不中断本轮）；参数校验失败返回错误文本。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from src.research.judgments import render_judgments
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
    market_data: Any | None = None
    watchlist_snapshot: tuple[str, ...] = ()
    market_data_contracts: set[str] = field(default_factory=set)
    market_snapshots: dict[str, dict] = field(default_factory=dict)
    pending_causal_links: list[dict] = field(default_factory=list)


def _fmt_ts(ts: float) -> str:
    """时间戳 → 'MM-DD HH:MM'（北京时间）：与数据源头串口径一致，UTC 部署机不偏移。

    参数：
        ts: float，Unix 秒时间戳

    返回：
        str：时间戳 → 'MM-DD HH:MM'（北京时间）：与数据源头串口径一致，UTC 部署机不偏移
    """
    return datetime.fromtimestamp(ts, tz=BEIJING_TZ).strftime("%m-%d %H:%M")


def _parse_int(args: dict, key: str, default: int, lo: int, hi: int) -> tuple[int, str | None]:
    """解析整数参数：缺失用默认值；非数字/越界返回错误文本（L1 参数容错）。

    布尔与非整数值（True/1.5）一律拒绝，不做静默截断（与 supersedes_id 同口径）。

    参数：
        args: dict，调用方传入的工具参数字典
        key: str，要读取或校验的参数键
        default: int，参数缺失时采用的默认整数
        lo: int，允许的最小整数
        hi: int，允许的最大整数

    返回：
        tuple[int, str | None]：解析整数参数：缺失用默认值；非数字/越界返回错误文本（L1 参数容错）
    """
    raw = args.get(key)
    if raw is None:
        value = default
    else:
        if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
            return 0, f"参数错误：{key} 必须为整数"
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 0, f"参数错误：{key} 必须为整数"
    if not lo <= value <= hi:
        return 0, f"参数错误：{key} 须在 {lo}-{hi} 之间"
    return value, None


def _today_markers() -> tuple[str, str]:
    """今日/明日日期串（北京时间 YYYY-MM-DD）：与日历 pub_time 同一时区口径。

    参数：
        无

    返回：
        tuple[str, str]：今日/明日日期串（北京时间 YYYY-MM-DD）：与日历 pub_time 同一时区口径
    """
    now = datetime.now(BEIJING_TZ)
    return now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")


# ---------- 只读工具 ----------


async def get_research_market_data(deps: ResearchToolDeps, args: dict) -> str:
    """一次返回白名单单合约的 4h/1d K线、指标、资金费率和 OI 结构。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：一次返回白名单单合约的 4h/1d K线、指标、资金费率和 OI 结构

    异常：
        ToolArgError：'contract 不能为空' 所描述的条件发生时
        ToolArgError：f"contract {contract!r} 不在本轮白名单：{', '.join(deps.watchlist_snapshot)}" 所描述的条件发生时
        ToolArgError：f'contract {contract!r} 已成功读取，禁止重复调用市场数据工具' 所描述的条件发生时
        ToolArgError：error 所描述的条件发生时
        ToolArgError：'研报市场数据服务未装配' 所描述的条件发生时
    """
    contract = str(args.get("contract") or "").strip()
    if not contract:
        raise ToolArgError("contract 不能为空")
    if contract not in deps.watchlist_snapshot:
        raise ToolArgError(
            f"contract {contract!r} 不在本轮白名单：{', '.join(deps.watchlist_snapshot)}"
        )
    limit, error = _parse_int(args, "limit", 30, 1, 100)
    if contract in deps.market_data_contracts:
        raise ToolArgError(f"contract {contract!r} 已成功读取，禁止重复调用市场数据工具")
    if error:
        raise ToolArgError(error)
    if deps.market_data is None:
        raise ToolArgError("研报市场数据服务未装配")
    snapshot = await deps.market_data.snapshot(contract, limit=limit)
    deps.market_data_contracts.add(contract)
    deps.market_snapshots[contract] = snapshot
    return json.dumps(snapshot, ensure_ascii=False)


async def fetch_calendar(deps: ResearchToolDeps, args: dict) -> str:
    """日历：今日+明日 star≥3 事件。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：日历：今日+明日 star≥3 事件
    """
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
    """全量快讯紧凑文本（时间+标题+摘要）。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：全量快讯紧凑文本（时间+标题+摘要）
    """
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
    """硬数据指标快照。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：硬数据指标快照
    """
    try:
        return await deps.provider.fetch_indicators()
    except ResearchSourceError as exc:
        return f"指标数据不可用：{exc}"


async def get_macro_series(deps: ResearchToolDeps, args: dict) -> str:
    """FRED 宏观序列。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：FRED 宏观序列
    """
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
    """Polymarket 预测概率。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：Polymarket 预测概率
    """
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
    """单条快讯/文章全文。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：单条快讯/文章全文
    """
    item_id = str(args.get("id") or "").strip()
    if not item_id:
        return "参数错误：id 必填"
    try:
        detail = await deps.provider.fetch_article_detail(item_id)
    except ResearchSourceError as exc:
        return f"详情不可用：{exc}"
    return detail or "（该条无全文）"


async def search_news(deps: ResearchToolDeps, args: dict) -> str:
    """关键词检索历史快讯/文章。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：关键词检索历史快讯/文章
    """
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
    """事实层近 N 天（客观记录）。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：事实层近 N 天（客观记录）
    """
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
    """判断层近 N 天，按报告与合约分组。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：判断层近 N 天，按报告与合约分组
    """
    days, err = _parse_int(args, "days", 7, 1, 30)
    if err:
        return err
    reports = await deps.repo.research.list_reports(days)
    if not reports:
        return f"近 {days} 天无研报记录"
    title = f"## 历史研报结论（近 {days} 天，{len(reports)} 份）"
    return await render_judgments(deps.repo.research, reports, title)


async def read_causal_links(deps: ResearchToolDeps, args: dict) -> str:
    """读取已提交因果链（含历史版与全部状态）：判断某主题是否已提交过、是否该提交修正版。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：读取已提交因果链（含历史版与全部状态）：判断某主题是否已提交过、是否该提交修正版
    """
    days, err = _parse_int(args, "days", 7, 1, 30)
    if err:
        return err
    limit, err = _parse_int(args, "limit", 20, 1, 50)
    if err:
        return err
    topic = str(args.get("topic") or "").strip() or None
    links = await deps.repo.research.list_causal_links(days=days, topic=topic, limit=limit)
    if not links:
        scope = f"主题 {topic!r} " if topic else ""
        return f"近 {days} 天{scope}无已提交因果链"
    scope = f"，主题 {topic}" if topic else ""
    lines = [f"## 已提交因果链（近 {days} 天{scope}，{len(links)} 条）"]
    for link in links:
        try:
            chain = json.loads(link.chain_json)
        except (TypeError, ValueError):
            chain = []
        nodes = " → ".join(str(n.get("node", ""))[:25] for n in chain if isinstance(n, dict))
        tag = "待验证" if link.status == "tracking" else "结论"
        if link.supersedes_id is not None:
            status = f"替代链#{link.supersedes_id}"  # 本链替代了旧链 X
        else:
            status = {"superseded": "已被替代"}.get(link.status, "当前版")
        lines.append(
            f"- [链#{link.id}][{link.topic or '无主题'}][{tag}][{status}] {nodes}"
            f"（置信度 {link.confidence}）"
        )
    return "\n".join(lines)


# ---------- 写工具 ----------


def _parse_await_verification(raw: Any) -> bool | None:
    """解析 await_verification：缺省 True；布尔/数字/常见字符串均可，非法返回 None。

    参数：
        raw: Any，待解析或保留的原始数据

    返回：
        bool | None：解析 await_verification：缺省 True；布尔/数字/常见字符串均可，非法返回 None
    """
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "是"):
            return True
        if lowered in ("0", "false", "no", "否"):
            return False
    return None


async def _validate_supersedes(
    deps: ResearchToolDeps, topic: str, raw: Any
) -> tuple[int | None, str | None]:
    """supersedes_id 校验：可空；非空须为正整数、链存在、未被替代、主题一致。

    主题校验放行空主题目标（历史遗留链 topic=''，允许以新主题修正）；
    同轮内已声明替代过该链则拒绝（防止一轮内重复替代产生双当前版）。
    返回 (校验后的 id, 错误文本)；错误文本非空时调用方直接返回。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        topic: str，因果链主题
        raw: Any，待解析或保留的原始数据

    返回：
        tuple[int | None, str | None]：supersedes_id 校验：可空；非空须为正整数、链存在、未被替代、主题一致
    """
    if raw in (None, ""):
        return None, None
    if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        return None, "参数错误：supersedes_id 必须为整数"
    try:
        link_id = int(raw)
    except (TypeError, ValueError):
        return None, "参数错误：supersedes_id 必须为整数"
    if link_id <= 0:
        return None, "参数错误：supersedes_id 必须为正整数"
    if any(p.get("supersedes_id") == link_id for p in deps.pending_causal_links):
        return None, f"参数错误：链 {link_id} 本轮已声明替代，不能重复替代"
    old = await deps.repo.research.get_causal_link(link_id)
    if old is None:
        return None, f"参数错误：supersedes_id 指向的链 {link_id} 不存在"
    if old.status == "superseded":
        return None, f"参数错误：链 {link_id} 已被替代，只能替代当前版"
    if old.topic and old.topic != topic:
        return None, f"参数错误：链 {link_id} 主题（{old.topic}）与本次（{topic}）不一致"
    return link_id, None


async def submit_causal_links(deps: ResearchToolDeps, args: dict) -> str:
    """提交链式因果链（唯一写出口）：校验通过后暂存 deps。

    H1 修复：LLM 无需也无法预知本轮研报 id（id 在工具循环结束后落库才生成），
    故 report_id 不再是 LLM 参数——agent 落研报后由代码回填并批量落库；
    本轮研报失败时暂存链随 deps 丢弃，不会错挂历史研报。

    版本化（V1）：topic 必填（同主题聚合成族）；supersedes_id 声明替代旧链
    （须同主题当前版），落库时旧链标记 superseded；await_verification 声明
    待验证中间态（默认 true，允许 1 节点半成品观察）或结论链（须 2-6 节点）。

    参数：
        deps: ResearchToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：提交链式因果链（唯一写出口）：校验通过后暂存 deps
    """
    chain = args.get("chain")
    confidence = args.get("confidence")
    topic = str(args.get("topic") or "").strip()
    if not topic:
        return "参数错误：topic 必填（事件主题，如 非农/关税/美联储）"
    await_verification = _parse_await_verification(args.get("await_verification"))
    if await_verification is None:
        return "参数错误：await_verification 必须为布尔值"
    min_nodes, max_nodes = (1, 6) if await_verification else (2, 6)
    if not isinstance(chain, list) or not min_nodes <= len(chain) <= max_nodes:
        return f"参数错误：chain 必须为 {min_nodes}-{max_nodes} 个节点的有序数组"
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
    supersedes_id, err = await _validate_supersedes(deps, topic, args.get("supersedes_id"))
    if err:
        return err
    deps.pending_causal_links.append(
        {
            "chain_json": json.dumps(chain, ensure_ascii=False),
            "confidence": confidence,
            "evidence_json": json.dumps(evidence, ensure_ascii=False),
            "topic": topic,
            "supersedes_id": supersedes_id,
            "status": "tracking" if await_verification else "concluded",
        }
    )
    nodes = " → ".join(str(n.get("node", ""))[:30] for n in chain)
    tag = "待验证" if await_verification else "结论"
    return f"因果链已暂存（{len(chain)} 节点，{tag}）：{nodes}（将随本轮研报自动关联落库）"
