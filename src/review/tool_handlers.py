"""复盘工具的异步执行函数 + 依赖载体（ReviewToolDeps）。

约定：
- 参数校验失败抛本地 ToolArgError，由 ReviewToolRegistry 统一转错误文本（不向上抛）；
- 所有 handler 返回中文纯文本（str），供 LLM 直接阅读；
- 只经 Repo / StrategyStore 读写，无任何 Gateway 依赖（安全不变量，spec §7.1）；
- 参数校验辅助与 src/agent/tool_handlers.py 同风格、本地实现（本包不 import src/agent/*）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from src.memory.repo import Repo
from src.review.stats import compute_review_stats, format_stats_text
from src.review.strategy import StrategyStore, StrategyValidationError

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
    """

    repo: Repo
    store: StrategyStore
    mode: str
    created_version_id: int | None = None


# ---------- 参数校验辅助 ----------


def _need_str(args: dict, name: str) -> str:
    v = args.get(name)
    if not isinstance(v, str) or not v.strip():
        raise ToolArgError(f"缺少必填参数 {name}（非空字符串）")
    return v.strip()


def _opt_str(args: dict, name: str) -> str | None:
    v = args.get(name)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ToolArgError(f"参数 {name} 必须是非空字符串")
    return v.strip()


def _to_int(v: Any, name: str) -> int:
    if v is None or isinstance(v, bool):
        raise ToolArgError(f"参数 {name} 必须是整数")
    try:
        return int(v)
    except (TypeError, ValueError) as e:
        raise ToolArgError(f"参数 {name} 必须是整数") from e


def _opt_int(args: dict, name: str, default: int) -> int:
    return default if args.get(name) is None else _to_int(args[name], name)


def _need_ts(args: dict, name: str) -> float:
    """必填 Unix 秒时间戳（数字；bool 拒绝）。"""
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
    return max(lo, min(hi, v))


# ---------- 排版辅助 ----------


def _fmt_time(ts: float) -> str:
    """Unix 秒 → 本地时间字符串。"""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _one_line(text: str, limit: int = 60) -> str:
    """取首行并截断，作为一行摘要。"""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit] + ("…" if len(line) > limit else "") if line else "（空）"


def _truncate(text: str, max_chars: int) -> str:
    """超长截断并标注原文长度。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…（已截断，原文共 {len(text)} 字符）"


# ---------- 只读工具 ----------


async def get_review_stats(deps: ReviewToolDeps, args: dict) -> str:
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


# ---------- 写工具（唯一出口） ----------


async def submit_strategy_revision(deps: ReviewToolDeps, args: dict) -> str:
    new_prompt_md = _need_str(args, "new_prompt_md")
    reason = _need_str(args, "reason")
    try:
        version = await deps.store.revise(new_prompt_md, reason, created_by="review_agent")
    except StrategyValidationError as e:
        return "校验拒绝：" + "；".join(e.reasons) + "（原策略书未改动，修正后可重新提交）"
    deps.created_version_id = version.id
    return f"校验通过，策略已更新至 v{version.id}（md5={version.md5[:8]}），下一轮决策生效"
