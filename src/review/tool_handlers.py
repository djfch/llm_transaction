"""复盘工具的异步执行函数 + 依赖载体（ReviewToolDeps）。

约定：
- 参数校验失败抛本地 ToolArgError，由 ReviewToolRegistry 统一转错误文本（不向上抛）；
- 所有 handler 返回中文纯文本（str），供 LLM 直接阅读；
- 只经 Repo / StrategyStore 读写，无任何 Gateway 依赖；
- 参数校验辅助与 src/agent/tool_handlers.py 同风格、本地实现（本包不 import src/agent/*）。
"""

from __future__ import annotations

from dataclasses import field as dataclass_field

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from src.market.indicator_service import IndicatorService
from src.memory.repo import Repo
from src.review.indicator_config import IndicatorConfigStore
from src.review.stats import compute_review_stats, format_stats_text
from src.review.strategy import StrategyStore, StrategyValidationError
from src.utils import calc_expression

_MAX_CHARS_LIMIT = 20000  # max_chars 上限
_CHAIN_SNIPPET = 500  # 工具调用链单条参数/结果截断长度
_VERSION_LIST_LIMIT = 50  # 版本列表最多返回条数


class ToolArgError(Exception):
    """复盘工具参数校验失败（转错误文本返回给 LLM，不中断本轮）。"""


@dataclass
class ReviewToolDeps:
    """复盘工具执行所需依赖。

    created_version_id：submit_strategy_revision 成功时置位，供 ReviewAgent
    在轮末判定 strategy_action 并做版本↔报告关联。
    indicator_config_version_id：submit_indicator_config 成功时置位，轮末由
    ReviewAgent 经 repo.indicator_config.attach_report_to_version 关联到报告。
    indicator_service / indicator_config_store：指标工具依赖（见
    tool_indicators.py），None（未装配）时指标工具返回「指标功能未配置」降级提示；
    watchlist 为空（未接线）时 get_indicators 不做合约归属限制。
    """

    repo: Repo
    store: StrategyStore
    mode: str
    created_version_id: int | None = None
    indicator_service: IndicatorService | None = None
    indicator_config_store: IndicatorConfigStore | None = None
    watchlist: tuple[str, ...] = ()
    indicator_config_version_id: int | None = None
    # 本轮落库的草稿版本 id（issue #62/#73）：报告成功经 store.apply_version 统一生效，
    # 失败/取消置 discarded——写工具不再直接改文件
    strategy_draft_ids: list[int] = dataclass_field(default_factory=list)
    indicator_draft_ids: list[int] = dataclass_field(default_factory=list)


# ---------- 参数校验辅助 ----------


def _need_str(args: dict, name: str) -> str:
    """读取必填的字符串参数并去除首尾空白。

    参数：
        args: dict，工具调用参数字典
        name: str，参数名

    返回：
        str：非空字符串参数值（已 strip）

    异常：
        ToolArgError：参数缺失、不是字符串或仅含空白时抛出
    """
    v = args.get(name)
    if not isinstance(v, str) or not v.strip():
        raise ToolArgError(f"缺少必填参数 {name}（非空字符串）")
    return v.strip()


def _opt_str(args: dict, name: str) -> str | None:
    """读取可选的字符串参数；未提供时返回 None。

    参数：
        args: dict，工具调用参数字典
        name: str，参数名

    返回：
        str | None：非空字符串参数值（已 strip）；参数未提供时返回 None

    异常：
        ToolArgError：参数已提供但不是非空字符串时抛出
    """
    v = args.get(name)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ToolArgError(f"参数 {name} 必须是非空字符串")
    return v.strip()


def _to_int(v: Any, name: str) -> int:
    """把参数值转换为整数（拒绝 None 与布尔值）。

    参数：
        v: Any，待转换的参数值
        name: str，参数名（用于错误提示）

    返回：
        int：转换后的整数

    异常：
        ToolArgError：值为 None、布尔值或无法转为整数时抛出
    """
    if v is None or isinstance(v, bool):
        raise ToolArgError(f"参数 {name} 必须是整数")
    try:
        return int(v)
    except (TypeError, ValueError) as e:
        raise ToolArgError(f"参数 {name} 必须是整数") from e


def _opt_int(args: dict, name: str, default: int) -> int:
    """读取可选整数参数；未提供时返回默认值。

    参数：
        args: dict，工具调用参数字典
        name: str，参数名
        default: int，参数未提供时的默认值

    返回：
        int：参数转换后的整数；未提供时返回 default
    """
    return default if args.get(name) is None else _to_int(args[name], name)


def _need_ts(args: dict, name: str) -> float:
    """必填 Unix 秒时间戳（数字；bool 拒绝）。

    参数：
        args: dict，调用方传入的工具参数字典
        name: str，字段、工具或资源名称

    返回：
        float：必填 Unix 秒时间戳（数字；bool 拒绝）

    异常：
        ToolArgError：f'缺少必填参数 {name}（Unix 秒数字）' 所描述的条件发生时
        ToolArgError：f'参数 {name} 必须是数字' 所描述的条件发生时
    """
    v = args.get(name)
    if v is None:
        raise ToolArgError(f"缺少必填参数 {name}（Unix 秒数字）")
    if isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
        raise ToolArgError(f"参数 {name} 必须是数字")
    try:
        return float(Decimal(str(v)))
    except InvalidOperation as e:
        raise ToolArgError(f"参数 {name} 必须是数字") from e


def _clamp(v: int, lo: int, hi: int) -> int:
    """把整数限制在闭区间 [lo, hi] 内。

    参数：
        v: int，待限制的数值
        lo: int，下界
        hi: int，上界

    返回：
        int：不小于 lo 且不大于 hi 的数值
    """
    return max(lo, min(hi, v))


# ---------- 排版辅助 ----------


def _fmt_time(ts: float) -> str:
    """Unix 秒 → 本地时间字符串。

    参数：
        ts: float，Unix 秒时间戳

    返回：
        str：Unix 秒 → 本地时间字符串
    """
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _one_line(text: str, limit: int = 60) -> str:
    """取首行并截断，作为一行摘要。

    参数：
        text: str，待处理的文本
        limit: int，最多读取或返回的记录数量

    返回：
        str：取首行并截断，作为一行摘要
    """
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit] + ("…" if len(line) > limit else "") if line else "（空）"


def _truncate(text: str, max_chars: int) -> str:
    """超长截断并标注原文长度。

    参数：
        text: str，待处理的文本
        max_chars: int，允许保留的最大字符数

    返回：
        str：超长截断并标注原文长度
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…（已截断，原文共 {len(text)} 字符）"


# ---------- 只读工具 ----------


async def get_review_stats(deps: ReviewToolDeps, args: dict) -> str:
    """统计指定时间区间内的成交记录，输出盈亏等复盘指标文本。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（用其 repo 与 mode 取数）
        args: dict，工具参数：start_ts/end_ts（Unix 秒，必填）、
            strategy_md5、contract（可选过滤条件）

    返回：
        str：区间概要与格式化统计文本，供 LLM 阅读
    """
    start_ts = _need_ts(args, "start_ts")
    end_ts = _need_ts(args, "end_ts")
    strategy_md5 = _opt_str(args, "strategy_md5")
    contract = _opt_str(args, "contract")
    trades = await deps.repo.review.trades_for_review(
        start_ts, end_ts, deps.mode, contract=contract, strategy_md5=strategy_md5
    )
    filters = []
    if strategy_md5:
        filters.append(f"策略md5={strategy_md5[:8]}")
    if contract:
        filters.append(f"合约={contract}")
    head = f"区间 {_fmt_time(start_ts)} ~ {_fmt_time(end_ts)}（{deps.mode}）"
    if filters:
        head += f"；过滤：{'，'.join(filters)}"
    return head + "\n" + format_stats_text(compute_review_stats(trades))


async def list_decision_rounds(deps: ReviewToolDeps, args: dict) -> str:
    """列出指定时间区间内的决策轮次清单（最新在前）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        args: dict，工具参数：start_ts/end_ts（Unix 秒，必填）、
            strategy_md5（可选过滤）、limit（可选，默认 20，限制在 1~100）

    返回：
        str：轮次列表文本（含唤醒来源、摘要与错误信息）；无记录时返回提示
    """
    start_ts = _need_ts(args, "start_ts")
    end_ts = _need_ts(args, "end_ts")
    strategy_md5 = _opt_str(args, "strategy_md5")
    limit = _clamp(_opt_int(args, "limit", 20), 1, 100)
    decisions = await deps.repo.review.decisions_for_review(
        start_ts, end_ts, strategy_md5, limit, mode=deps.mode
    )
    if not decisions:
        return "区间内无决策轮次"
    rounds = await deps.repo.list_audit_rounds([d.round_id for d in decisions])
    lines = [f"区间内共 {len(decisions)} 轮决策（最新在前）："]
    for d in decisions:
        error = rounds[d.round_id].error if d.round_id in rounds else ""
        md5 = d.strategy_md5[:8] if d.strategy_md5 else "—"
        lines.append(
            f"- {_fmt_time(d.created_at)} | round={d.round_id}（{d.round_id[:8]}）"
            f" | 唤醒={d.wake_source or '—'} | 策略md5={md5}"
            f" | 摘要={_one_line(d.context_summary or d.llm_raw)} | 错误={error or '无'}"
        )
    return "\n".join(lines)


async def get_decision_detail(deps: ReviewToolDeps, args: dict) -> str:
    """查看单个决策轮次的上下文摘要与 LLM 原始输出。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        args: dict，工具参数：round_id（必填，决策轮次 ID）、max_chars（可选，
            默认 4000，上限 20000，LLM 原始输出的截断长度）

    返回：
        str：轮次详情文本；轮次不存在时返回核对提示
    """
    round_id = _need_str(args, "round_id")
    max_chars = _clamp(_opt_int(args, "max_chars", 4000), 1, _MAX_CHARS_LIMIT)
    d = await deps.repo.get_decision_by_round(round_id)
    if d is None:
        return f"未找到决策轮次 {round_id}（请用 list_decision_rounds 核对 round_id）"
    md5 = d.strategy_md5[:8] if d.strategy_md5 else "—"
    return "\n".join(
        [
            f"轮次 {d.round_id} | 唤醒={d.wake_source or '—'} | 策略md5={md5}"
            f" | 时间={_fmt_time(d.created_at)}",
            "上下文摘要：",
            d.context_summary or "（空）",
            f"LLM 原始输出（截断至 {max_chars} 字符）：",
            _truncate(d.llm_raw, max_chars),
        ]
    )


async def get_tool_call_chain(deps: ReviewToolDeps, args: dict) -> str:
    """按调用顺序列出某个决策轮次的全部工具调用链。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        args: dict，工具参数：round_id（必填，决策轮次 ID）

    返回：
        str：逐次调用的工具名、耗时、风控结论及截断后的参数与结果；无记录时返回提示
    """
    round_id = _need_str(args, "round_id")
    calls = await deps.repo.list_audit_tool_calls(round_id)
    if not calls:
        return f"轮次 {round_id} 无工具调用记录"
    lines = [f"轮次 {round_id} 共 {len(calls)} 次工具调用（按调用顺序）："]
    for c in calls:
        risk = c.risk_verdict or "—"
        if c.risk_reason:
            risk += f"（{c.risk_reason}）"
        lines.append(
            f"#{c.seq} {c.tool} | 耗时={c.duration_ms}ms | 风控={risk}\n"
            f"  参数：{_truncate(c.args_json, _CHAIN_SNIPPET)}\n"
            f"  结果：{_truncate(c.result_json, _CHAIN_SNIPPET)}"
        )
    return "\n".join(lines)


async def list_trades(deps: ReviewToolDeps, args: dict) -> str:
    """列出指定时间区间内的成交流水（按时间正序）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        args: dict，工具参数：start_ts/end_ts（Unix 秒，必填）、contract、
            source（可选过滤）、limit（可选，默认 50，限制在 1~200）

    返回：
        str：成交列表文本（含方向张数、价格、费用、盈亏与来源）；无记录时返回提示
    """
    start_ts = _need_ts(args, "start_ts")
    end_ts = _need_ts(args, "end_ts")
    contract = _opt_str(args, "contract")
    source = _opt_str(args, "source")
    limit = _clamp(_opt_int(args, "limit", 50), 1, 200)
    trades = await deps.repo.review.list_trades_filtered(
        start_ts, end_ts, contract, source, limit, mode=deps.mode
    )
    if not trades:
        return "区间内无成交记录"
    lines = [f"区间内共 {len(trades)} 笔成交（按时间正序）："]
    for t in trades:
        lines.append(
            f"- {_fmt_time(t.created_at)} | {t.contract} | size={t.size} | 价格={t.price}"
            f" | 费用={t.fee} | 盈亏={t.pnl} | 来源={t.source or '未知'}"
            f" | round={t.round_id[:8]}"
        )
    return "\n".join(lines)


async def get_round_context(deps: ReviewToolDeps, args: dict) -> str:
    """查看某个决策轮次当时的上下文快照。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        args: dict，工具参数：round_id（必填，决策轮次 ID）、max_chars（可选，
            默认 4000，上限 20000，快照截断长度）

    返回：
        str：上下文快照文本；轮次不存在时返回核对提示
    """
    round_id = _need_str(args, "round_id")
    max_chars = _clamp(_opt_int(args, "max_chars", 4000), 1, _MAX_CHARS_LIMIT)
    r = await deps.repo.get_audit_round(round_id)
    if r is None:
        return f"未找到审计轮次 {round_id}（请用 list_decision_rounds 核对 round_id）"
    snapshot = r.context_snapshot or "（无上下文快照）"
    return f"轮次 {round_id} 上下文快照（截断至 {max_chars} 字符）：\n" + _truncate(
        snapshot, max_chars
    )


async def get_strategy_versions(deps: ReviewToolDeps, args: dict) -> str:
    """查看策略书版本历史或指定版本全文，并附当前策略全文。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（用其 store 读取策略版本）
        args: dict，工具参数：version_id（可选；提供时返回该版本详情与全文，
            缺省时返回版本列表与当前策略全文）

    返回：
        str：版本详情或版本列表文本；指定版本不存在时返回提示
    """
    vid_arg = args.get("version_id")
    if vid_arg is not None:
        v = await deps.store.get_version(_to_int(vid_arg, "version_id"))
        if v is None:
            return f"策略版本 v{vid_arg} 不存在"
        return (
            f"v{v.id} | 来源={v.created_by} | md5={v.md5} | 时间={_fmt_time(v.created_at)}"
            f" | 理由={v.reason}\n全文：\n{v.content}"
        )
    versions = (await deps.store.list_versions())[:_VERSION_LIST_LIMIT]
    lines = [f"策略版本共 {len(versions)} 个（最新在前）："]
    for v in versions:
        lines.append(
            f"- v{v.id} | 来源={v.created_by} | md5={v.md5[:8]}"
            f" | 时间={_fmt_time(v.created_at)} | 理由={v.reason}"
        )
    lines += ["", "当前策略全文：", deps.store.current() or "（策略书文件不存在）"]
    return "\n".join(lines)


async def calc(deps: ReviewToolDeps, args: dict) -> str:
    """数学表达式计算（纯函数，不碰任何依赖）；错误以中文文本返回。

    参数：
        deps: ReviewToolDeps，当前模块所需的运行依赖集合
        args: dict，调用方传入的工具参数字典

    返回：
        str：数学表达式计算（纯函数，不碰任何依赖）；错误以中文文本返回
    """
    return calc_expression(_need_str(args, "expression"))


# ---------- 写工具（唯一出口） ----------


async def submit_strategy_revision(deps: ReviewToolDeps, args: dict) -> str:
    """提交修订后的策略书；校验通过则生成新版本，下一轮决策生效。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（成功时回写 created_version_id）
        args: dict，工具参数：new_prompt_md（必填，新策略书全文）、
            reason（必填，修订理由）

    返回：
        str：提交结果文本；校验拒绝时列出原因且原策略书不变
    """
    new_prompt_md = _need_str(args, "new_prompt_md")
    reason = _need_str(args, "reason")
    try:
        version = await deps.store.revise(new_prompt_md, reason, created_by="review_agent")
    except StrategyValidationError as e:
        return "校验拒绝：" + "；".join(e.reasons) + "（原策略书未改动，修正后可重新提交）"
    deps.created_version_id = version.id
    deps.strategy_draft_ids.append(version.id)
    return (
        f"校验通过，修订已存为草稿 v{version.id}（md5={version.md5[:8]}）；"
        "本轮复盘报告提交成功后统一生效，报告失败则自动废弃"
    )
