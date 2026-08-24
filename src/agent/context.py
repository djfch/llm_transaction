"""决策上下文组装：账户/持仓/白名单行情摘要/价格预警线/研报前瞻/交易计划/近期笔记/近期成交。

产出的 AgentContext.text 同时用作发给 LLM 的 user 消息与审计的上下文快照；
summary 为一行摘要，落 decisions.context_summary。
共享辅助（compute_equity / position_snapshots / summarize_candles）供工具层复用。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.audit.logger import get_logger
from src.config import DEFAULT_INDICATOR_SHORTLIST, ResearchConfig
from src.gateway.async_io import PRIORITY_NORMAL, read_positions_with_tpsl, run_gateway_io
from src.gateway.base import Account, Candle, Contract, Gateway, GatewayError, Position, Ticker
from src.market.candles import CandleCache, stale_text
from src.market.indicator_service import IndicatorService
from src.market.triggers import MAX_ALERTS, TriggerManager
from src.memory.models import ResearchAssetView
from src.memory.repo import Repo
from src.risk.models import PositionSnapshot

logger = get_logger(__name__)


def compute_equity(account: Account, positions: list[Position]) -> Decimal:
    """账户权益估值 = 可用余额 + Σ持仓保证金 + 账户级未实现盈亏（与 paper 记账口径一致）。

    参数：
        account: Account，账户快照
        positions: list[Position]，当前持仓列表
    返回：
        Decimal，账户权益估值 = 可用余额 + Σ持仓保证金 + 账户级未实现盈亏（与 paper 记账口径一致）
    """
    margin = sum((p.margin for p in positions), Decimal(0))
    return account.available + margin + account.unrealised_pnl


async def position_snapshots(
    gateway: Gateway,
    positions: list[Position],
    *,
    priority: int = PRIORITY_NORMAL,
    metadata: dict[str, Contract] | None = None,
) -> list[PositionSnapshot]:
    """持仓 → 风控快照（quanto_multiplier 取合约元数据；标记价缺失时回退元数据）。

    逐合约元数据各自经统一卸载层独立调度（不打包成单个复合任务）：HIGH 人工
    平仓可在子请求之间插队，不被 N 次 get_contract 串行读取长期阻断（PR #84
    评审 P1，与 list_positions 裸读同一原则）。

    参数：
        gateway: Gateway，交易所网关
        positions: list[Position]，当前持仓列表
        priority: int，卸载优先级；手动安全操作传 PRIORITY_HIGH
        metadata: dict[str, Contract] | None，调用方已读取的合约规格，可避免重复查询
    返回：
        list[PositionSnapshot]，持仓 → 风控快照（quanto_multiplier 取合约元数据；标记价缺失时回退元数据）
    """
    snaps = []
    for p in positions:
        meta = (metadata or {}).get(p.contract)
        if meta is None:
            meta = await run_gateway_io(gateway.get_contract, p.contract, priority=priority)
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
    """K 线序列压缩为一行摘要：首 open → 末 close、区间高低、简单变化率。

    参数：
        contract: str，合约标识
        interval: str，K 线或持仓量周期
        candles: list[Candle]，按时间排序的 K 线序列
    返回：
        str，K 线序列压缩为一行摘要：首 open → 末 close、区间高低、简单变化率
    """
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
        indicator_service: IndicatorService | None = None,
        indicator_shortlist: Callable[[], list[str]] | None = None,
        research_config: ResearchConfig | None = None,
    ) -> None:
        """注入网关、存储与行情等依赖，并固化本轮上下文的截断与指标配置。

        参数：
            gateway: Gateway，交易所网关，用于读取账户、持仓与行情数据
            repo: Repo，存储仓库，用于读取研报结论、交易计划、近期笔记与成交
            candles: CandleCache，K 线缓存，用于按白名单取最近若干根 K 线
            triggers: TriggerManager，价格预警线管理器，提供未触发预警列表
            watchlist: list[str]，白名单合约列表，行情与研报段落按它逐个组装
            interval: str，K 线周期；省略时默认为 "1h"
            candle_n: int，每合约展示的最近 K 线根数；省略时默认为 24
            alerts_n: int，最多展示的预警线条数；省略时默认为 20
            notes_n: int，最多展示的近期笔记条数；省略时默认为 10
            trades_n: int，最多展示的近期成交笔数；省略时默认为 20
            indicator_service: IndicatorService | None，指标服务；
                省略时默认为 None（行情段落不含指标行）
            indicator_shortlist: Callable[[], list[str]] | None，指标短名单来源；
                省略时回退内置基线 DEFAULT_INDICATOR_SHORTLIST
            research_config: ResearchConfig | None，研报配置（含方向闸门开关）；
                省略时默认为 None（视为闸门关闭）

        返回：
            None，初始化实例依赖与配置并写入实例属性
        """
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
        self._indicator_service = indicator_service
        # 短名单来源由装配层注入（读 indicator_config.yaml）；缺省回退内置基线
        self._indicator_shortlist = indicator_shortlist or (
            lambda: list(DEFAULT_INDICATOR_SHORTLIST)
        )
        self._research_config = research_config

    async def build(self, wake_source: str) -> AgentContext:
        """组装一轮决策的完整上下文文本与一行摘要。

        汇总账户/持仓、白名单行情、价格预警线、研报前瞻、交易计划、
        近期笔记与近期成交各段落；某个研报段落为空时自动省略。

        参数：
            wake_source: str，本轮唤醒来源（如定时、预警触发），写入头部与摘要

        返回：
            AgentContext：text 为完整上下文（user 消息与审计快照），
            summary 为一行摘要（落 decisions.context_summary）
        """
        account = await run_gateway_io(self._gateway.get_account)
        positions = await read_positions_with_tpsl(self._gateway)  # 展示路径：逐合约补全 TPSL
        tickers, metas = await self._read_market_data()
        equity = compute_equity(account, positions)
        sections = [
            self._header(wake_source),
            self._account_section(account, positions, equity),
            self._market_section(tickers, metas),
            self._alerts_section(),
            await self._research_section(),
            await self._plans_section(),
            await self._notes_section(),
            await self._trades_section(),
        ]
        summary = f"权益 {equity}，持仓 {len(positions)} 个，唤醒源 {wake_source}"
        text = "\n\n".join(s for s in sections if s is not None)
        return AgentContext(text=text, summary=summary)

    def _header(self, wake_source: str) -> str:
        """生成上下文头部：标题、本轮唤醒来源与当前本地时间。

        参数：
            wake_source: str，本轮唤醒来源（如定时、预警触发）

        返回：
            str：上下文头部文本（Markdown 标题段）
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"# 交易决策上下文\n本轮唤醒来源：{wake_source}\n当前时间：{now}"

    def _account_section(self, account: Account, positions: list[Position], equity: Decimal) -> str:
        """生成账户段落：权益估值、可用余额、未实现盈亏与逐持仓明细。

        参数：
            account: Account，合约账户（可用余额、未实现盈亏）
            positions: list[Position]，当前持仓列表；为空时标注"持仓: 无"
            equity: Decimal，账户权益估值（由 compute_equity 预先算好）

        返回：
            str：账户段落文本（含每个持仓的方向张数、入场价、标记价、
            杠杆、保证金、浮盈与止盈止损设置）
        """
        lines = [
            "## 账户",
            f"权益(估值): {equity}；可用余额: {account.available}；"
            f"未实现盈亏: {account.unrealised_pnl}",
        ]
        if not positions:
            lines.append("持仓: 无")
        for p in positions:
            lev_text = (
                f"{p.cross_leverage_limit}x（全仓）"
                if p.margin_mode == "cross" and p.cross_leverage_limit is not None
                else f"{p.leverage}x"
            )
            lines.append(
                f"持仓 {p.contract}: size={p.size}，入场价 {p.entry_price}，"
                f"标记价 {p.mark_price}，杠杆 {lev_text}，保证金 {p.margin}，"
                f"浮盈 {p.unrealised_pnl}，止损 {p.stop_loss_price or '未设置'}，"
                f"止盈 {p.take_profit_price or '未设置'}"
            )
        return "\n".join(lines)

    async def _read_market_data(self) -> tuple[dict[str, Ticker], dict[str, Contract]]:
        """读取行情段落的全部网关数据：ticker 全集 + ticker 缺失合约的元数据回退。

        每次网关调用单独经统一卸载层 run_gateway_io 提交，由网关方法自身的内联
        标记决定执行线程（paper 无真实行情 provider 时 get_tickers/get_contract
        内联、与撮合同线程；真实网关或 paper 接 REST provider 时卸载到 executor）。
        不得把整段作为单个同步函数卸载：owner 是 ContextBuilder 而非网关，会绕过
        paper 的线程亲和判定（PR #84 评审 P1）。get_tickers 抛错原样上抛，仅单合约
        元数据回退读取失败时降级为 None（该合约行情行最终显示"无行情数据"）。

        参数：无

        返回：
            tuple[dict[str, Ticker], dict[str, Contract]]：（合约 -> ticker）与
            （ticker 缺失合约 -> 元数据）两个字典；元数据读取失败的合约不在后者中
        """
        tickers = {t.contract: t for t in await run_gateway_io(self._gateway.get_tickers)}
        metas: dict[str, Contract] = {}
        for contract in self._watchlist:
            if contract in tickers:
                continue
            try:  # ticker 缺失时回退合约元数据（仍含标记价/资金费率）
                metas[contract] = await run_gateway_io(self._gateway.get_contract, contract)
            except GatewayError:
                continue
        return tickers, metas

    def _market_section(self, tickers: dict[str, Ticker], metas: dict[str, Contract]) -> str:
        """生成行情段落：按白名单逐合约给出 ticker 行、K 线摘要行与指标行。

        参数：
            tickers: dict[str, Ticker]，_read_market_data 预读的（合约 -> ticker）
            metas: dict[str, Contract]，ticker 缺失合约的元数据回退（缺失时该合约
                行情行降级为"无行情数据"）

        返回：
            str：行情段落文本（每合约依次为 ticker 摘要、K 线摘要，
            指标服务接入时追加指标短名单行）
        """
        lines = ["## 行情"]
        for contract in self._watchlist:
            lines.append(self._ticker_line(contract, tickers.get(contract), metas.get(contract)))
            candles = self._candles.get_recent(contract, self._interval, self._candle_n)
            stale = stale_text(candles, self._interval)
            if stale is not None:
                # 停更即报错：不把旧 K 线喂给 LLM，防其基于过时行情幻觉决策（issue #74）
                lines.append(f"{contract} {self._interval} K线数据不可用：{stale}")
            else:
                lines.append(summarize_candles(contract, self._interval, candles))
            indicator_line = self._indicator_line(contract)
            if indicator_line is not None:
                lines.append(indicator_line)
        return "\n".join(lines)

    def _indicator_line(self, contract: str) -> str | None:
        """短名单指标单行（每合约第三行，紧随 K 线摘要行）。

        服务未接入返回 None（整行省略，不留痕迹）；服务未就绪/数据异常降级为
        提示文本（同 _ticker_line 风格），不拖垮其余 section。

        参数：
            contract: str，合约标识
        返回：
            str | None，短名单指标单行（每合约第三行，紧随 K 线摘要行）
        """
        if self._indicator_service is None:
            return None
        try:
            keys = self._indicator_shortlist()
            return self._indicator_service.shortlist_line(contract, self._interval, keys)
        except Exception as e:
            logger.warning("指标短名单行生成失败（%s）：%s", contract, e)
            return f"{contract} 指标({self._interval}): 暂不可用"

    def _ticker_line(self, contract: str, ticker: Ticker | None, meta: Contract | None) -> str:
        """生成单合约 ticker 摘要行；ticker 缺失时降级回退元数据，不让行情行断档。

        参数：
            contract: str，合约名（如 BTC_USDT）
            ticker: Ticker | None，合约 ticker 摘要（_read_market_data 预读）
            meta: Contract | None，ticker 缺失时的元数据回退（含标记价与资金费率）

        返回：
            str：单行摘要（标记价、资金费率、24h 涨跌与高低）；
            ticker 与元数据都不可用时返回"无行情数据"提示行
        """
        if ticker is not None:
            return (
                f"{contract}: 标记价 {ticker.mark_price}，资金费率 {ticker.funding_rate}，"
                f"24h涨跌 {ticker.change_percentage}%，24h高/低 "
                f"{ticker.high_24h}/{ticker.low_24h}"
            )
        if meta is not None:
            return f"{contract}: 标记价 {meta.mark_price}，资金费率 {meta.funding_rate}（取自合约元数据）"
        return f"{contract}: 无行情数据"

    def _alerts_section(self) -> str:
        """生成当前未触发价格预警线，供模型决定保留或取消。

        条数超 alerts_n 时按 id 升序截断（旧的优先展示），标题保留总数、尾部标注未显示
        条数，避免预警线异常累积时上下文无界膨胀。

        参数：无
        返回：
            str：当前未触发预警线及超出展示上限的数量
        """
        triggers = sorted(self._triggers.list(), key=lambda t: t.id)
        lines = [f"## 价格预警线（{len(triggers)}/{MAX_ALERTS} 条）"]
        if not triggers:
            lines.append("（无）")
        for t in triggers[: self._alerts_n]:
            direction = "above" if t.direction == ">=" else "below"
            lines.append(f"- {t.contract} {direction} {t.price}（设置于 {_fmt_ts(t.created_at)}）")
        if len(triggers) > self._alerts_n:
            lines.append(f"- …另有 {len(triggers) - self._alerts_n} 条未显示")
        return "\n".join(lines)

    async def _research_section(self) -> str | None:
        """按白名单逐合约注入当前研报结论。

        参数：无
        返回：
            str | None，按白名单逐合约注入当前研报结论
        """
        try:
            views = [
                view
                for contract in self._watchlist
                if (view := await self._repo.research.latest_asset_view(contract)) is not None
            ]
        except Exception as e:
            logger.exception("研报前瞻段生成失败：%s", e)
            return "## 研报前瞻（宏观与消息面）\n暂不可用"
        if not views:
            return None
        lines = ["## 研报前瞻（宏观与消息面）"]
        for view in views:
            lines.extend(self._research_view_lines(view))
        return "\n".join(lines)

    def _research_view_lines(self, view: ResearchAssetView) -> list[str]:
        """把单合约研报结论渲染为多行文本（方向/置信度/结构等要素 + 正文摘要）。

        参数：
            view: ResearchAssetView，单合约研报结论及当时市场输入快照；
                narrative 超 500 字时截断并加省略号

        返回：
            list[str]：该合约研报段落的各行文本；方向硬闸门生效时
            末行追加风控提示
        """
        narrative = view.narrative[:500] + ("…" if len(view.narrative) > 500 else "")
        lines = [
            f"### {view.contract}",
            f"方向：{view.direction} · 置信度：{view.confidence} · 周期：{view.horizon}",
            f"结构：{view.market_regime} · 依据：{view.basis_type} · "
            f"技术确认：{view.technical_confirmation} · 数据：{view.data_status}",
            f"创建时间：{_fmt_ts(view.created_at)}",
        ]
        if narrative:
            lines.append(f"正文摘要：{narrative}")
        if self._gate_active(view):
            lines.append("⚠ 高置信结论有效期内：反向开仓已被风控硬约束")
        return lines

    def _gate_active(self, view: ResearchAssetView) -> bool:
        """与下单硬闸门保持同一口径。

        参数：
            view: ResearchAssetView，逐标的研报结论
        返回：
            bool，与下单硬闸门保持同一口径
        """
        cfg = self._research_config
        if cfg is None or not cfg.gate_enabled:
            return False
        eligible = (
            view.confidence == "高"
            and view.direction in ("偏多", "偏空")
            and view.basis_type in ("事件驱动", "宏观驱动", "混合")
            and view.data_status != "不可用"
            and view.technical_confirmation not in ("冲突", "不可用")
        )
        return eligible and time.time() - view.created_at <= cfg.gate_max_age_hours * 3600

    async def _notes_section(self) -> str:
        """生成近期笔记段落：取最近 notes_n 条笔记，附创建时间逐条列出。

        参数：无

        返回：
            str：近期笔记段落文本；无笔记时标注"（无）"
        """
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

        参数：无
        返回：
            str，当前交易计划（全局唯一一份）：每轮必看并核对，更新时间供 LLM 判断新旧
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
        """生成近期成交段落：取最近 trades_n 笔成交，逐笔列出合约、张数、价格与盈亏。

        参数：无

        返回：
            str：近期成交段落文本；无成交时标注"（无）"
        """
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
    """把 Unix 时间戳格式化为 "月-日 时:分" 短文本，供上下文各行引用时间。

    参数：
        ts: float，Unix 时间戳（秒，本地时区解释）

    返回：
        str：形如 "08-10 15:30" 的格式化时间串
    """
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
