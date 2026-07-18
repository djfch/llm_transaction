"""非交易类 LLM 工具的异步执行函数 + 共享基础设施（ToolDeps/校验辅助）。

约定：
- 参数校验失败抛 ToolArgError，由 ToolRegistry 统一转成错误文本（不向上抛异常）
- 交易类工具（place_order / amend_order / cancel_order / set_leverage）在 tool_trading，
  内部先过风控，拒绝则返回理由文本
- 金额/数量一律 Decimal；返回 ToolOutcome 携带风控判定（供审计落库）
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from src.agent.context import compute_equity, summarize_candles
from src.config import RiskConfig
from src.gateway.base import Gateway, GatewayError
from src.market.candles import CandleCache
from src.market.triggers import TriggerManager
from src.memory.repo import Repo
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats


class ToolArgError(Exception):
    """工具参数校验失败（转错误文本返回给 LLM，不中断本轮）。"""


@dataclass
class ToolOutcome:
    """工具执行结果。risk_verdict: allow/deny/空串（非风控工具）。"""

    text: str
    risk_verdict: str = ""
    risk_reason: str = ""


DailyStatsFn = Callable[[], Awaitable[DailyStats]]


@dataclass
class ToolDeps:
    """工具执行所需依赖（round_id 由决策循环每轮写入）。"""

    gateway: Gateway
    risk_engine: RiskEngine
    risk_config: RiskConfig
    watchlist: list[str]
    repo: Repo
    candles: CandleCache
    triggers: TriggerManager
    daily_stats_fn: DailyStatsFn
    mode: str = "paper"
    set_next_wake: Callable[[int], int] | None = None  # 返回钳制后实际生效分钟数
    round_id: str = ""
    # 无 drain_fills 钩子（真实网关）时为 True：已成交单由工具层直接落 trades 表；
    # paper 模式由决策循环 drain 统一落库，工具层不再落（防双计）
    save_fills_inline: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# ---------- 参数校验辅助 ----------


def _need_str(args: dict, name: str) -> str:
    v = args.get(name)
    if not isinstance(v, str) or not v.strip():
        raise ToolArgError(f"缺少必填参数 {name}（非空字符串）")
    return v.strip()


def _to_decimal(v: Any, name: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
        raise ToolArgError(f"参数 {name} 必须是数字")
    try:
        return Decimal(str(v))
    except InvalidOperation as e:
        raise ToolArgError(f"参数 {name} 必须是数字") from e


def _need_decimal(args: dict, name: str) -> Decimal:
    if name not in args or args[name] is None:
        raise ToolArgError(f"缺少必填参数 {name}（数字）")
    return _to_decimal(args[name], name)


def _opt_decimal(args: dict, name: str) -> Decimal | None:
    return None if args.get(name) is None else _to_decimal(args[name], name)


def _need_int(args: dict, name: str) -> int:
    v = args.get(name)
    if v is None or isinstance(v, bool):
        raise ToolArgError(f"缺少必填参数 {name}（整数）")
    try:
        return int(v)
    except (TypeError, ValueError) as e:
        raise ToolArgError(f"参数 {name} 必须是整数") from e


def _opt_int(args: dict, name: str, default: int) -> int:
    return default if args.get(name) is None else _need_int(args, name)


def _opt_enum(args: dict, name: str, options: set[str]) -> str | None:
    v = args.get(name)
    if v is None:
        return None
    if not isinstance(v, str) or v not in options:
        raise ToolArgError(f"参数 {name} 取值非法：{v!r}（可选 {'/'.join(sorted(options))}）")
    return v


def _need_enum(args: dict, name: str, options: set[str]) -> str:
    v = _opt_enum(args, name, options)
    if v is None:
        raise ToolArgError(f"缺少必填参数 {name}（可选 {'/'.join(sorted(options))}）")
    return v


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


# ---------- 工具执行函数 ----------


async def get_market_data(deps: ToolDeps, args: dict) -> ToolOutcome:
    contract = _need_str(args, "contract")
    intervals = {"10s", "1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d", "7d"}
    interval = _opt_enum(args, "interval", intervals) or "1h"
    limit = _clamp(_opt_int(args, "limit", 24), 1, 100)
    candles = deps.candles.get_recent(contract, interval, limit)
    lines = [summarize_candles(contract, interval, candles)]
    try:
        meta = deps.gateway.get_contract(contract)
        lines.append(f"标记价: {meta.mark_price}；资金费率: {meta.funding_rate}")
    except GatewayError:
        lines.append("（无法获取合约行情）")
    return ToolOutcome("\n".join(lines))


async def get_account(deps: ToolDeps, args: dict) -> ToolOutcome:
    account = deps.gateway.get_account()
    positions = deps.gateway.list_positions()
    lines = [
        f"账户权益(估值): {compute_equity(account, positions)}",
        f"可用余额: {account.available}；未实现盈亏: {account.unrealised_pnl}",
        f"持仓数: {len(positions)}",
    ]
    for p in positions:
        lines.append(
            f"持仓 {p.contract}: size={p.size}，入场价 {p.entry_price}，"
            f"标记价 {p.mark_price}，杠杆 {p.leverage}x，浮盈 {p.unrealised_pnl}"
        )
    return ToolOutcome("\n".join(lines))


async def set_price_alert(deps: ToolDeps, args: dict) -> ToolOutcome:
    contract = _need_str(args, "contract")
    direction = _need_enum(args, "direction", {"above", "below"})
    price = _need_decimal(args, "price")
    if price <= 0:
        raise ToolArgError("price 必须为正数")
    trigger = deps.triggers.add(contract, ">=" if direction == "above" else "<=", price)
    await deps.repo.add_alert(deps.round_id, contract, direction, price)
    return ToolOutcome(f"已设置价格预警：{contract} {direction} {price}（触发器 {trigger.id}）")


async def set_next_wakeup(deps: ToolDeps, args: dict) -> ToolOutcome:
    minutes = _need_int(args, "minutes")
    if minutes <= 0:
        raise ToolArgError("minutes 必须为正整数")
    if deps.set_next_wake is None:
        return ToolOutcome("错误：调度器未接入，无法设置下次唤醒")
    effective = deps.set_next_wake(minutes)
    return ToolOutcome(f"下次唤醒已设置为 {effective} 分钟后（请求 {minutes} 分钟）")


async def write_note(deps: ToolDeps, args: dict) -> ToolOutcome:
    content = _need_str(args, "content")
    note = await deps.repo.add_note(deps.round_id, content)
    return ToolOutcome(f"笔记已保存（id={note.id}）")


async def get_history(deps: ToolDeps, args: dict) -> ToolOutcome:
    limit = _clamp(_opt_int(args, "limit", 20), 1, 50)
    trades = (await deps.repo.trades_between(0.0, time.time()))[-limit:]
    decisions = await deps.repo.list_decisions(limit=min(limit, 10))
    lines = [f"近 {len(trades)} 笔成交："]
    lines += [f"- {t.contract} size={t.size} 价格 {t.price}，盈亏 {t.pnl}" for t in trades]
    lines.append(f"近 {len(decisions)} 轮决策：")
    lines += [
        f"- round={d.round_id[:8]} 唤醒={d.wake_source} "
        f"时间={time.strftime('%m-%d %H:%M', time.localtime(d.created_at))}"
        for d in decisions
    ]
    return ToolOutcome("\n".join(lines))
