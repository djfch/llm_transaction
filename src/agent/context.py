"""决策上下文组装：账户/持仓/白名单行情摘要/价格预警线/交易计划/近期笔记/近期成交。

产出的 AgentContext.text 同时用作发给 LLM 的 user 消息与审计的上下文快照；
summary 为一行摘要，落 decisions.context_summary。
共享辅助（compute_equity / position_snapshots / summarize_candles）供工具层复用。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.gateway.base import Account, Candle, Gateway, GatewayError, Position
from src.market.candles import CandleCache
from src.market.triggers import MAX_ALERTS, TriggerManager
from src.memory.repo import Repo
from src.risk.models import PositionSnapshot


def compute_equity(account: Account, positions: list[Position]) -> Decimal:
    """账户权益估值 = 可用余额 + Σ持仓保证金 + 账户级未实现盈亏（与 paper 记账口径一致）。"""
    margin = sum((p.margin for p in positions), Decimal(0))
    return account.available + margin + account.unrealised_pnl


def position_snapshots(gateway: Gateway, positions: list[Position]) -> list[PositionSnapshot]:
    """持仓 → 风控快照（quanto_multiplier 取合约元数据；标记价缺失时回退元数据）。"""
    snaps = []
    for p in positions:
        meta = gateway.get_contract(p.contract)
        mark = p.mark_price if p.mark_price > 0 else meta.mark_price
        snaps.append(
            PositionSnapshot(
                contract=p.contract,
                size=p.size,
                mark_price=mark,
                quanto_multiplier=meta.quanto_multiplier,
            )
        )
    return snaps


def summarize_candles(contract: str, interval: str, candles: list[Candle]) -> str:
    """K 线序列压缩为一行摘要：首 open → 末 close、区间高低、简单变化率。"""
    if not candles:
        return f"{contract} {interval}: 无 K 线数据"
    o, c = candles[0].o, candles[-1].c
    h = max(x.h for x in candles)
    low = min(x.l for x in candles)
    change = (c - o) / o * 100 if o else Decimal(0)
    return (
        f"{contract} {interval} 近{len(candles)}根: open {o} → close {c} "
        f"({change:+.2f}%), high {h}, low {low}"
    )


@dataclass
class AgentContext:
    """一轮决策的完整上下文。"""

    text: str  # 完整上下文（user 消息 + 审计快照）
    summary: str  # 一行摘要（decisions.context_summary）


class ContextBuilder:
    """按白名单组装上下文；长度受 candle_n/alerts_n/notes_n/trades_n 约束。"""

    def __init__(
        self,
        gateway: Gateway,
        repo: Repo,
        candles: CandleCache,
        triggers: TriggerManager,
        watchlist: list[str],
        interval: str = "1h",
        candle_n: int = 24,
        alerts_n: int = 20,
        notes_n: int = 10,
        trades_n: int = 20,
    ) -> None:
        self._gateway = gateway
        self._repo = repo
        self._candles = candles
        self._triggers = triggers
        self._watchlist = watchlist
        self._interval = interval
        self._candle_n = candle_n
        self._alerts_n = alerts_n
        self._notes_n = notes_n
        self._trades_n = trades_n

    async def build(self, wake_source: str) -> AgentContext:
        account = self._gateway.get_account()
        positions = self._gateway.list_positions()
        equity = compute_equity(account, positions)
        sections = [
            self._header(wake_source),
            self._account_section(account, positions, equity),
            self._market_section(),
            self._alerts_section(),
            await self._plans_section(),
            await self._notes_section(),
            await self._trades_section(),
        ]
        summary = f"权益 {equity}，持仓 {len(positions)} 个，唤醒源 {wake_source}"
        return AgentContext(text="\n\n".join(sections), summary=summary)

    def _header(self, wake_source: str) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"# 交易决策上下文\n本轮唤醒来源：{wake_source}\n当前时间：{now}"

    def _account_section(self, account: Account, positions: list[Position], equity: Decimal) -> str:
        lines = [
            "## 账户",
            f"权益(估值): {equity}；可用余额: {account.available}；"
            f"未实现盈亏: {account.unrealised_pnl}",
        ]
        if not positions:
            lines.append("持仓: 无")
        for p in positions:
            lines.append(
                f"持仓 {p.contract}: size={p.size}，入场价 {p.entry_price}，"
                f"标记价 {p.mark_price}，杠杆 {p.leverage}x，保证金 {p.margin}，"
                f"浮盈 {p.unrealised_pnl}，止损 {p.stop_loss_price or '未设置'}，"
                f"止盈 {p.take_profit_price or '未设置'}"
            )
        return "\n".join(lines)

    def _market_section(self) -> str:
        tickers = {t.contract: t for t in self._gateway.get_tickers()}
        lines = ["## 行情"]
        for contract in self._watchlist:
            lines.append(self._ticker_line(contract, tickers.get(contract)))
            candles = self._candles.get_recent(contract, self._interval, self._candle_n)
            lines.append(summarize_candles(contract, self._interval, candles))
        return "\n".join(lines)

    def _ticker_line(self, contract: str, ticker) -> str:
        if ticker is not None:
            return (
                f"{contract}: 标记价 {ticker.mark_price}，资金费率 {ticker.funding_rate}，"
                f"24h涨跌 {ticker.change_percentage}%，24h高/低 "
                f"{ticker.high_24h}/{ticker.low_24h}"
            )
        try:  # ticker 缺失时回退合约元数据（仍含标记价/资金费率）
            meta = self._gateway.get_contract(contract)
            return f"{contract}: 标记价 {meta.mark_price}，资金费率 {meta.funding_rate}（取自合约元数据）"
        except GatewayError:
            return f"{contract}: 无行情数据"

    def _alerts_section(self) -> str:
        """未触发价格预警线（内存唯一存储，重启即失效——如实暴露给 LLM，供其决定重设/取消）。

        条数超 alerts_n 时按 id 升序截断（旧的优先展示），标题保留总数、尾部标注未显示
        条数，避免预警线异常累积时上下文无界膨胀。
        """
        triggers = sorted(self._triggers.list(), key=lambda t: t.id)
        lines = [f"## 价格预警线（内存·重启即失效，{len(triggers)}/{MAX_ALERTS} 条）"]
        if not triggers:
            lines.append("（无）")
        for t in triggers[: self._alerts_n]:
            direction = "above" if t.direction == ">=" else "below"
            lines.append(f"- {t.contract} {direction} {t.price}（设置于 {_fmt_ts(t.created_at)}）")
        if len(triggers) > self._alerts_n:
            lines.append(f"- …另有 {len(triggers) - self._alerts_n} 条未显示")
        return "\n".join(lines)

    async def _notes_section(self) -> str:
        notes = await self._repo.recent_notes(self._notes_n)
        lines = [f"## 近期笔记（近 {self._notes_n} 条）"]
        if not notes:
            lines.append("（无）")
        for n in notes:
            lines.append(f"- [{_fmt_ts(n.created_at)}] {n.content}")
        return "\n".join(lines)

    async def _plans_section(self) -> str:
        """当前交易计划（全局唯一一份）：每轮必看并核对，更新时间供 LLM 判断新旧。

        计划原文逐行加引用前缀定界：自由文本每轮重复注入，不加护栏则可伪装成
        其他系统 section（跨轮自我强化注入面）。
        """
        plan = await self._repo.plans.get_plan()
        if plan is None:
            return "## 交易计划（全局唯一一份，用 update_trade_plan 全文覆盖更新）\n（无）"
        body = "\n".join("> " + line for line in plan.content.splitlines())
        return (
            f"## 交易计划（更新于 {_fmt_ts(plan.updated_at)}；执行/作废后用 "
            "clear_trade_plan 清空，修订用 update_trade_plan 全文覆盖）\n"
            f"以下引用块为你上次保存的计划原文：\n{body}"
        )

    async def _trades_section(self) -> str:
        trades = (await self._repo.trades_between(0.0, time.time()))[-self._trades_n :]
        lines = [f"## 近期成交（近 {self._trades_n} 笔）"]
        if not trades:
            lines.append("（无）")
        for t in trades:
            lines.append(
                f"- [{_fmt_ts(t.created_at)}] {t.contract} size={t.size} "
                f"价格 {t.price}，手续费 {t.fee}，已实现盈亏 {t.pnl}"
            )
        return "\n".join(lines)


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
