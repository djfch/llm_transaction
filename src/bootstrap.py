"""应用组装：把配置、网关、行情、风控、Agent、调度、通知、监控服务接成一个整体。

main.py 只负责解析入口与生命周期；组件创建全部在这里，便于冒烟/测试复用。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import uvicorn

from src.agent.fill_persist import FillPersister
from src.agent.fill_sync import ExchangeFillSync
from src.agent.fill_sync_setup import build_trade_sync
from src.agent.loop import DecisionLoop
from src.agent.prompts import PromptLoader
from src.agent.providers.base import LLMError, LLMProvider
from src.agent.providers.factory import build_provider, create_provider, resolve_agent_credential
from src.agent.providers.mock import MockProvider
from src.agent.ticker_fanout import make_on_ticker
from src.audit.logger import get_logger
from src.gateway.async_io import run_gateway_io
from src.lag_monitor import monitor_event_loop_lag
from src.audit.trail import AuditTrail
from src.config import ROOT, CredentialConfig, Settings, Watchlist
from src.config_io import read_settings_raw, write_settings
from src.gateway.base import Candle, Contract, Gateway
from src.gateway.gate_rest import GateRestGateway
from src.market.candles import CandleCache, ManualPriceSource, PriceSource
from src.market.feed import MarketFeed
from src.market.indicators_setup import IndicatorComponents, setup_indicators
from src.market.intervals import GATE_CANDLE_INTERVALS
from src.market.private_feed import PrivateTradeFeed
from src.market.triggers import TriggerManager
from src.memory.db import Database
from src.memory.repo import Repo
from src.notify.telegram import build_notifier
from src.paper.engine import PaperGateway
from src.paper.funding_patrol import funding_loop
from src.market.candles import stale_watchdog
from src.paper.setup import build_paper_gateway
from src.review.research_outcome import RecentWindowCandleSource
from src.review.setup import ReviewComponents, build_review
from src.research.prompt_store import ResearchPromptStore
from src.research.setup import ResearchComponents, build_research
from src.risk.engine import RiskEngine
from src.scheduler.wakeup import WakeupScheduler
from src.server.app import create_app
from src.server.deps import ServerDeps

logger = get_logger(__name__)

# 订阅+回补的 K 线周期：Gate 全周期（单一数据源在 src/market/intervals.py），
# LLM get_market_data 可查任意周期；WS 逐根滚动、启动时 REST 回补均遍历此列表
CANDLE_INTERVALS: list[str] = list(GATE_CANDLE_INTERVALS)


@dataclass
class AppContext:
    """组装完成的应用组件集合，供运行/冒烟/关闭使用。"""

    settings: Settings
    db: Database
    repo: Repo
    gateway: Gateway
    source: PriceSource
    scheduler: WakeupScheduler
    loop: DecisionLoop
    review: ReviewComponents  # 复盘子系统组件束（store/agent/scheduler + 策略写回调）
    research: ResearchComponents  # 研报子系统组件束（agent + 数据聚合器）
    event_queue: asyncio.Queue
    candles: CandleCache
    triggers: TriggerManager
    watchlist: list[str]  # 与 DecisionLoop/ServerDeps 共享同一 list（前端改名单原地生效）
    active_trader_credential: CredentialConfig | None = None
    started_at: float = field(default_factory=time.time)
    server: uvicorn.Server | None = None
    server_deps: ServerDeps | None = None
    # 交易所成交回报对账（testnet/live 且非 mock 行情时装配；paper/mock 为 None）
    trade_feed: PrivateTradeFeed | None = None
    trade_sync: ExchangeFillSync | None = None


def _default_contract(name: str, mark: Decimal) -> Contract:
    """mock 行情下的默认合约元数据（费率/步长取常见档位，仅供模拟撮合）。

    参数：
        name: str，工具名或参数名
        mark: Decimal，模拟标记价格

    返回：
        Contract，mock 行情下的默认合约元数据（费率/步长取常见档位，仅供模拟撮合）
    """
    return Contract(
        name=name,
        quanto_multiplier=Decimal("0.001"),
        mark_price=mark,
        order_size_min=Decimal("1"),
        order_size_max=Decimal("1000000"),
        order_price_round=Decimal("0.1"),
        enable_decimal=False,
        funding_rate=Decimal("0.0001"),
        funding_interval=28800,
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        status="trading",
        in_delisting=False,
    )


def _build_gateway(
    settings: Settings,
    watchlist: Watchlist,
    mock_market: bool,
    candle_provider: Callable[..., list[Candle]] | None,
) -> Gateway:
    """按运行模式创建网关：paper→模拟撮合；testnet/live→真实 REST。
    paper 模式 candle_provider 显式注入优先，否则用公共 REST 网关（无签名，合约/K线/持仓量同源委托）。

    参数：
        settings: Settings，应用配置
        watchlist: Watchlist，关注合约配置
        mock_market: bool，是否使用模拟行情
        candle_provider: Callable[..., list[Candle]] | None，可选的历史 K 线提供函数

    返回：
        Gateway，按运行模式创建网关：paper→模拟撮合；testnet/live→真实 REST。 paper 模式 candle_provider 显式注入优先，否则用公共 REST 网关（无签名，合约/K线/持仓量同源委托）

    """
    if settings.mode != "paper":
        return GateRestGateway(
            settings.gate,
            testnet=settings.mode == "testnet",
            api_key=os.environ.get("GATE_API_KEY", ""),
            api_secret=os.environ.get("GATE_API_SECRET", ""),
        )
    public = None if mock_market else GateRestGateway(settings.gate, testnet=False)
    if mock_market:
        contracts = [_default_contract(name, Decimal("60000")) for name in watchlist.contracts]
    else:
        contracts = [public.get_contract(name) for name in watchlist.contracts]
    return build_paper_gateway(settings.paper, contracts, candle_provider, public)


def _make_llm_reconfigure(ctx: AppContext, mock_llm: bool) -> Callable[[], Awaitable[dict]]:
    """生成 LLM 热重建回调：按各 agent 当前绑定凭证逐个重建 provider 并热替换。

    单 agent 重建失败（LLMError）保留其旧 provider 并在 error 中点名（多个错误用
    中文分号连接）；mock 模式直接回报已配置。响应契约：{"llm_configured": bool, "error": str}。

    参数：
        ctx: AppContext，应用运行上下文
        mock_llm: bool，是否使用模拟 LLM

    返回：
        Callable[[], Awaitable[dict]]，生成 LLM 热重建回调：按各 agent 当前绑定凭证逐个重建 provider 并热替换。  单 agent 重建失败（LLMError）保留其旧 provider 并在 error 中点名（多个错误用 中文分号连接）；mock 模式直接回报已配置。响应契约：{"llm_configured": bool, "error": str}

    """

    async def reconfigure() -> dict:
        """热重建全部 agent 的 LLM provider 并汇报结果。

        mock 模式直接回报已配置；单个 agent 重建失败保留其旧 provider 并记入 error。

        参数：无

        返回：
            dict：{"llm_configured": 交易 agent 是否已配置可用,
            "error": 各 agent 失败原因（中文分号连接，空串表示全部成功）}
        """
        if mock_llm or os.environ.get("LLM_MOCK") == "1":
            ctx.active_trader_credential = resolve_agent_credential(
                ctx.settings, ctx.settings.agents.trader.credential
            ).model_copy(deep=True)
            return {"llm_configured": True, "error": ""}
        targets = (
            ("trader", ctx.settings.agents.trader.credential, ctx.loop),
            ("reviewer", ctx.settings.agents.reviewer.credential, ctx.review.agent),
            ("researcher", ctx.settings.agents.researcher.credential, ctx.research.agent),
        )
        errors: list[str] = []
        for agent_name, cred_name, target in targets:
            try:
                cred = resolve_agent_credential(ctx.settings, cred_name)
                target.set_provider(create_provider(cred))
                if agent_name == "trader":
                    ctx.active_trader_credential = cred.model_copy(deep=True)
                logger.info(
                    "LLM provider 已热重建（%s → %s / %s）", agent_name, cred.provider, cred.model
                )
            except LLMError as exc:
                logger.warning(
                    "LLM provider 热重建失败（%s，保留旧 provider）：%s", agent_name, exc
                )
                errors.append(f"{agent_name}: {exc}")
        return {"llm_configured": ctx.loop.llm_configured, "error": "；".join(errors)}

    return reconfigure


def _build_source(settings: Settings, watchlist: Watchlist, mock_market: bool) -> PriceSource:
    """行情源：mock 用 ManualPriceSource（测试/冒烟手动推送）；否则 Gate WS 订阅。

    参数：
        settings: Settings，应用配置
        watchlist: Watchlist，关注合约配置
        mock_market: bool，是否使用模拟行情

    返回：
        PriceSource，行情源：mock 用 ManualPriceSource（测试/冒烟手动推送）；否则 Gate WS 订阅
    """
    if mock_market:
        return ManualPriceSource()
    return MarketFeed(
        watchlist.contracts,
        CANDLE_INTERVALS,
        settle=settings.gate.settle,
        testnet=settings.mode == "testnet",
        ws_host=settings.gate.testnet_ws_host,
    )


def _backfill_candles(candles: CandleCache, contracts: list[str], *, skip: bool) -> None:
    """启动时 REST 回补历史 K 线。单个周期失败由 backfill 内部隔离记 warning；
    本层 try 仅兜底非周期性异常（WS 仍会逐根积累），不阻断启动。

    参数：
        candles: CandleCache，按时间升序的 K 线序列
        contracts: list[str]，需要回补或订阅的合约列表
        skip: bool，是否跳过历史 K 线回补

    返回：
        None，启动时 REST 回补历史 K 线。单个周期失败由 backfill 内部隔离记 warning； 本层 try 仅兜底非周期性异常（WS 仍会逐根积累），不阻断启动

    """
    if skip:  # mock 行情且无注入 provider：不做真实 REST 回补（可无网运行）
        return
    try:
        candles.backfill(contracts, CANDLE_INTERVALS)
    except Exception:
        logger.warning("K 线历史回补失败，WS 将逐根积累", exc_info=True)


def _build_loop(
    settings: Settings,
    watchlist: Watchlist,
    *,
    provider: LLMProvider | None,
    gateway: Gateway,
    repo: Repo,
    candles: CandleCache,
    triggers: TriggerManager,
    scheduler: WakeupScheduler,
    cfg_path: Path,
    audit: AuditTrail,
    notify_event: Callable[[dict], None] | None = None,
    fill_persister: FillPersister | None = None,
    indicators: IndicatorComponents | None = None,
) -> DecisionLoop:
    """创建决策循环：paper 网关接 drain_fills；真实网关 None（trades 由 fill_sync 落库）。

    provider 由 build_app 按 trader 绑定凭证构造传入（与复盘 agent 各自独立实例）。

    参数：
        settings: Settings，应用配置
        watchlist: Watchlist，关注合约配置
        provider: LLMProvider | None，LLM 提供商
        gateway: Gateway，交易网关
        repo: Repo，数据仓储
        candles: CandleCache，按时间升序的 K 线序列
        triggers: TriggerManager，价格触发器管理器
        scheduler: WakeupScheduler，唤醒调度器
        cfg_path: Path，运行配置文件路径
        audit: AuditTrail，审计轨迹实例
        notify_event: Callable[[dict], None] | None，可选事件通知回调
        fill_persister: FillPersister | None，可选成交持久化协调器
        indicators: IndicatorComponents | None，指标子系统组件

    返回：
        DecisionLoop，创建决策循环：paper 网关接 drain_fills；真实网关 None（trades 由 fill_sync 落库）。  provider 由 build_app 按 trader 绑定凭证构造传入（与复盘 agent 各自独立实例）

    """
    notifier = build_notifier(settings.notify)

    def persist_kill_switch(enabled: bool) -> None:
        """风控锁写回 config.yaml：读原文 → 改字段 → 校验写回（同步回调）。

        参数：
            enabled: bool，熔断开关是否启用

        返回：
            None，风控锁写回 config.yaml：读原文 → 改字段 → 校验写回（同步回调）
        """
        raw = read_settings_raw(cfg_path)
        raw.setdefault("risk", {})["kill_switch"] = enabled
        write_settings(raw, cfg_path)

    return DecisionLoop(
        settings=settings,
        watchlist=watchlist.contracts,
        gateway=gateway,
        repo=repo,
        provider=provider,
        risk_engine=RiskEngine(),
        candles=candles,
        triggers=triggers,
        prompt_loader=PromptLoader(ROOT / "system_prompt.md"),
        set_next_wake=scheduler.set_next_wake,
        on_alert=notifier.send,
        drain_fills=gateway.drain_fills if isinstance(gateway, PaperGateway) else None,
        persist_kill_switch=persist_kill_switch,
        notify_event=notify_event,  # 工具层变更事件（如 plan_updated）直推 WS 广播队列
        fill_persister=fill_persister,  # 与 on_ticker 即时 drain 共用同一实例（锁互斥）
        audit=audit,  # 与 server 共用同一实例
        indicator_service=None if indicators is None else indicators.service,
        indicator_shortlist=None if indicators is None else indicators.shortlist_keys,
    )


async def build_app(
    settings: Settings,
    watchlist: Watchlist,
    *,
    mock_llm: bool = False,
    mock_market: bool = False,
    db_path: str | Path = "data/agent.db",
    config_path: Path | None = None,
    candle_provider: Callable[..., list[Candle]] | None = None,
) -> AppContext:
    """创建全部组件并接好依赖（尚未启动行情/调度/HTTP）。

    参数：
        settings: Settings，应用配置
        watchlist: Watchlist，关注合约配置
        mock_llm: bool，是否使用模拟 LLM
        mock_market: bool，是否使用模拟行情
        db_path: str | Path，数据库文件路径
        config_path: Path | None，可选配置文件路径
        candle_provider: Callable[..., list[Candle]] | None，可选的历史 K 线提供函数

    返回：
        AppContext，创建全部组件并接好依赖（尚未启动行情/调度/HTTP）
    """
    db = Database()
    await db.open(db_path)
    repo = Repo(db)
    audit = AuditTrail(repo, settings.audit)
    gateway = _build_gateway(settings, watchlist, mock_market, candle_provider)
    source = _build_source(settings, watchlist, mock_market)
    candles = CandleCache(gateway, source)  # 构造时自动注册 on_candle
    await run_gateway_io(
        _backfill_candles,
        candles,
        watchlist.contracts,
        skip=mock_market and candle_provider is None,
    )

    async def on_wake(wake_source: str) -> None:
        """调度器唤醒回调：运行决策循环，再广播本轮结束结果。

        闭包晚绑定 loop：本回调在 loop 创建后才会被调度器调用。

        参数：
            wake_source: str，唤醒来源（定时唤醒/价格触发/手动抢醒等）

        返回：None，round_start 由决策循环在审计落库后发送，此处补发 round 结束事件
        """
        result = await loop.run_once(wake_source)
        await event_queue.put(
            {
                "type": "round",
                "data": {"round_id": result.round_id, "ok": result.ok, "wake_source": wake_source},
            }
        )

    scheduler = WakeupScheduler(settings.scheduler, on_wake)
    # 价格预警线为内存唯一存储：触发即移除并抢醒调度器；进程重启即失效（不重建），
    # LLM 经上下文「价格预警线」段看到空列表后自行决定是否重设
    triggers = TriggerManager(
        lambda t, price: scheduler.wake_now(f"price_trigger:{t.contract}@{price}")
    )
    event_queue: asyncio.Queue = asyncio.Queue()
    fill_persister = FillPersister(repo, settings.mode, event_queue.put_nowait)
    # ticker 广播进 WS 事件流：on_ticker 经 maybe_await 在事件循环线程同步调用，put_nowait 安全
    source.set_handlers(
        on_ticker=make_on_ticker(
            gateway,
            triggers,
            lambda msg: event_queue.put_nowait(msg),
            fill_persister=fill_persister,
        )
    )
    # 每个 agent 按其绑定凭证各自构造 provider（同一凭证也各自建实例：互不阻塞、可独立热重建）；
    # mock 模式共享同一 MockProvider 实例
    if mock_llm or os.environ.get("LLM_MOCK") == "1":
        trader_provider = reviewer_provider = MockProvider()
        # 研报专用 Mock（独立实例）：交易 Mock 输出交易工具与非 JSON 文本，研报链路不可用
        from src.research.mock_provider import ResearchMockProvider

        researcher_provider = ResearchMockProvider()
    else:
        trader_provider = build_provider(settings, mock_llm, settings.agents.trader.credential)
        reviewer_provider = build_provider(settings, mock_llm, settings.agents.reviewer.credential)
        researcher_provider = build_provider(
            settings, mock_llm, settings.agents.researcher.credential
        )
    indicators = await setup_indicators(repo, gateway, candles, watchlist.contracts, event_queue)
    loop = _build_loop(
        settings,
        watchlist,
        provider=trader_provider,
        gateway=gateway,
        repo=repo,
        candles=candles,
        triggers=triggers,
        scheduler=scheduler,
        audit=audit,
        cfg_path=config_path or ROOT / "config.yaml",
        notify_event=event_queue.put_nowait,
        fill_persister=fill_persister,
        indicators=indicators,
    )
    # 研报提示词版本存储（issue #113）：启动播种 v1 + 对账（孤儿草稿废弃、文件以库为准），
    # 复盘 agent 经它做草稿修订；变更即广播 research_prompt_updated
    research_prompt_store = ResearchPromptStore(
        ROOT / "research_prompt.md",
        repo,
        on_change=lambda: event_queue.put_nowait({"type": "research_prompt_updated"}),
    )
    await research_prompt_store.seed_if_empty()
    await research_prompt_store.reconcile()
    ctx = AppContext(
        settings=settings,
        db=db,
        repo=repo,
        gateway=gateway,
        source=source,
        loop=loop,
        review=await build_review(  # 复盘子系统装配（策略变更即广播 strategy_updated）
            settings,
            repo,
            audit,
            reviewer_provider,
            notify_event=event_queue.put_nowait,
            indicator_service=indicators.service,
            indicator_config_store=indicators.store,
            watchlist=watchlist.contracts,
            # Gate 网关 from/to 区间路径不可用，包一层最近 N 根+窗口过滤适配器（issue #113）
            candle_source=RecentWindowCandleSource(gateway),
            research_prompt_store=research_prompt_store,
        ),
        research=build_research(  # 研报子系统装配（轮始/轮末事件经 WS 广播）
            settings,
            repo,
            audit,
            researcher_provider,
            notify_event=event_queue.put_nowait,
            candle_cache=candles,
            gateway=gateway,
            watchlist=watchlist.contracts,
            prompt_store=research_prompt_store,
        ),
        scheduler=scheduler,
        event_queue=event_queue,
        candles=candles,
        triggers=triggers,
        watchlist=watchlist.contracts,
        active_trader_credential=(
            resolve_agent_credential(settings, settings.agents.trader.credential).model_copy(
                deep=True
            )
            if trader_provider is not None
            else None
        ),
    )
    ctx.server, ctx.server_deps = _build_server(ctx, audit, indicators, mock_llm=mock_llm)
    if not mock_market:  # mock 行情不接私有 WS（测试/冒烟可离线运行）
        pair = build_trade_sync(
            settings, gateway, db, event_queue.put_nowait, build_notifier(settings.notify).send
        )
        if pair is not None:
            ctx.trade_feed, ctx.trade_sync = pair
    return ctx


def _build_server(
    ctx: AppContext, audit: AuditTrail, indicators: IndicatorComponents, *, mock_llm: bool = False
) -> tuple[uvicorn.Server, ServerDeps]:
    """创建监控 HTTP 服务（与 agent 同进程运行）；runtime_* 与决策循环共享同一实例。

    参数：
        ctx: AppContext，应用运行上下文
        audit: AuditTrail，审计轨迹实例
        indicators: IndicatorComponents，指标子系统组件
        mock_llm: bool，是否使用模拟 LLM

    返回：
        tuple[uvicorn.Server, ServerDeps]，同进程运行的 Uvicorn 服务及其服务端依赖
    """
    settings = ctx.settings

    def status_provider() -> dict:
        """汇总运行时状态供监控接口查询。

        参数：无

        返回：
            dict：运行模式、启动时长（秒）、风控锁状态、agent 运行/在轮状态及
            LLM 配置可用状态
        """
        status = {
            "mode": settings.mode,
            "uptime_seconds": int(time.time() - ctx.started_at),
            "kill_switch": settings.risk.kill_switch,
            "agent_running": ctx.scheduler.is_running,
            "in_round": ctx.scheduler.in_round,
            "llm_configured": ctx.loop.llm_configured,
        }
        active = ctx.active_trader_credential
        if active is not None:
            status.update(
                {
                    "llm_credential_name": active.name,
                    "llm_provider": active.provider,
                    "llm_model": active.model,
                    "llm_thinking_effort": active.thinking_effort,
                }
            )
        return status

    def on_kill_switch(enabled: bool) -> None:
        """监控接口的风控锁开关回调。

        参数：
            enabled: bool，是否开启风控锁（开启后禁止新增敞口）

        返回：None，就地修改 settings.risk.kill_switch（仅内存生效，不落盘）
        """
        settings.risk.kill_switch = enabled

    async def manual_close(contract: str) -> dict:
        """手动平仓适配：调用时解析 loop.manual_close（接口冻结，与 LLM 平仓同一风控路径）。

        同步/异步实现均可（isawaitable 消化），server 层统一按异步回调注入。

        参数：
            contract: str，合约标识

        返回：
            dict，决策循环人工平仓返回的结构化结果

        """
        result = ctx.loop.manual_close(contract)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def manual_cancel_order(contract: str, order_id: str) -> dict:
        """手动撤单适配：调用决策循环的撤单接口，兼容同步和异步实现（isawaitable 消化）。

        参数：
            contract: str，合约名（如 BTC_USDT）
            order_id: str，待撤销的订单 ID

        返回：
            dict：撤单结果（透传决策循环 manual_cancel_order 的返回值）
        """
        result = ctx.loop.manual_cancel_order(contract, order_id)
        if inspect.isawaitable(result):
            result = await result
        return result

    def paper_reset(equity: Decimal) -> None:
        """模拟账户重置适配：调用时解析 gateway.reset_account（清空模拟仓位/挂单）。

        重置后上调决策循环的重置代际：旧 Agent 轮已过风控、尚未落单的增仓写
        会在落单前比对代际失效而中止，不得在新账户上重新开仓（issue #81）。

        参数：
            equity: Decimal，重置后的模拟账户权益

        返回：
            None，模拟账户重置适配：调用时解析 gateway.reset_account（清空模拟仓位/挂单）
        """
        ctx.gateway.reset_account(equity)  # type: ignore[attr-defined]
        ctx.loop.notify_paper_reset()

    async def agent_start() -> None:
        """手动启动 agent：启动调度器并立即抢醒第一轮（用户点击"启动"的合理预期是
        马上开始决策，而非干等 default_wake_minutes 后的首个定时唤醒）。

        参数：无

        返回：
            None，手动启动 agent：启动调度器并立即抢醒第一轮（用户点击"启动"的合理预期是 马上开始决策，而非干等 default_wake_minutes 后的首个定时唤醒）

        """
        await ctx.scheduler.start()
        ctx.scheduler.wake_now("manual_start")

    deps = ServerDeps(
        repo=ctx.repo,
        audit_trail=audit,
        gateway=ctx.gateway,
        event_queue=ctx.event_queue,
        status_provider=status_provider,
        on_kill_switch=on_kill_switch,
        manual_close=manual_close,
        manual_cancel_order=manual_cancel_order,
        paper_reset=paper_reset if isinstance(ctx.gateway, PaperGateway) else None,
        agent_start=agent_start,
        agent_stop=ctx.scheduler.stop,
        llm_reconfigure=_make_llm_reconfigure(ctx, mock_llm),
        alerts_provider=lambda: ctx.triggers.list(),
        review_run=ctx.review.scheduler.start_now,
        research_run=ctx.research.scheduler.start_now,
        research_schedule_status=ctx.research.scheduler.status,
        strategy_save=ctx.review.strategy_save,
        strategy_rollback=ctx.review.strategy_rollback,
        # 研报提示词版本写回调（issue #113）：store 由主程序恒装配（上方已播种/对账），
        # 与策略写回调同口径无条件接线
        research_prompt_save=ctx.research.research_prompt_save,
        research_prompt_rollback=ctx.research.research_prompt_rollback,
        runtime_settings=settings,
        runtime_watchlist=ctx.watchlist,
        indicators=indicators,
    )
    app = create_app(deps)
    config = uvicorn.Config(
        app, host=settings.server.host, port=settings.server.port, log_level="warning"
    )
    return uvicorn.Server(config), deps


async def run_app(
    ctx: AppContext,
    *,
    duration: float | None = None,
    price_pusher: Callable[[AppContext], Awaitable[None]] | None = None,
) -> None:
    """启动并运行应用；duration 为 None 时长驻（Ctrl+C 退出），否则到时自动关闭。

    参数：
        ctx: AppContext，应用运行上下文
        duration: float | None，可选运行时长秒数
        price_pusher: Callable[[AppContext], Awaitable[None]] | None，可选模拟价格推送协程

    返回：
        None，启动并运行应用；duration 为 None 时长驻（Ctrl+C 退出），否则到时自动关闭
    """
    assert ctx.server is not None
    await ctx.source.start()
    if ctx.trade_feed is not None and ctx.trade_sync is not None:
        await ctx.trade_feed.start()
        await ctx.trade_sync.catch_up()  # 启动补漏：离线期间成交经 REST 补齐（REST 在子线程）
    if ctx.settings.scheduler.autostart:
        await ctx.scheduler.start()
    else:
        logger.info(
            "agent 决策未自动启动（scheduler.autostart=false）：监控已可用，请在主页点击“启动 agent”"
        )
    server_task = asyncio.create_task(ctx.server.serve())
    pusher_task = asyncio.create_task(price_pusher(ctx)) if price_pusher else None
    funding_task = asyncio.create_task(funding_loop(ctx.gateway))
    # K 线停更看门狗：WS 断联后缓存冻结，超阈值自动 REST 回补（issue #74 第一层自愈）
    watchdog_task = asyncio.create_task(stale_watchdog(ctx.candles))
    # 成交补漏低频安全网（gatews 静默重连绕过 on_reconnected，秒级断线窗口靠它兜底）
    safety_task = (
        asyncio.create_task(ctx.trade_sync.run_safety_net()) if ctx.trade_sync is not None else None
    )
    # 复盘/研报巡检无论 enabled 与否都创建：scheduler 每 tick 现读各自 enabled 配置（热开关）
    review_task = asyncio.create_task(ctx.review.scheduler.run_forever())
    research_task = asyncio.create_task(ctx.research.scheduler.run_forever())
    # 事件循环 lag 哨兵：同步阻塞回归（绕过卸载层）时超阈值记 warning（issue #72 建议 5）
    lag_task = asyncio.create_task(monitor_event_loop_lag())
    logger.info(
        "应用已启动（mode=%s，HTTP=%s:%d）",
        ctx.settings.mode,
        ctx.settings.server.host,
        ctx.settings.server.port,
    )
    try:
        if duration is not None:
            await asyncio.sleep(duration)
        else:
            await asyncio.Event().wait()  # 长驻，Ctrl+C 退出
    finally:
        await shutdown(
            ctx,
            server_task,
            pusher_task,
            funding_task,
            review_task,
            research_task,
            safety_task,
            lag_task,
            watchdog_task,
        )


async def shutdown(
    ctx: AppContext,
    server_task: asyncio.Task,
    pusher_task: asyncio.Task | None,
    funding_task: asyncio.Task,
    review_task: asyncio.Task,
    research_task: asyncio.Task,
    safety_task: asyncio.Task | None = None,
    lag_task: asyncio.Task | None = None,
    watchdog_task: asyncio.Task | None = None,
) -> None:
    """优雅退出：停调度与行情，关 HTTP，收尾数据库。

    参数：
        ctx: AppContext，应用运行上下文
        server_task: asyncio.Task，HTTP 服务任务
        pusher_task: asyncio.Task | None，可选价格推送任务
        funding_task: asyncio.Task，资金费巡检任务
        review_task: asyncio.Task，复盘调度任务
        research_task: asyncio.Task，研报调度任务
        safety_task: asyncio.Task | None，可选安全对账任务
        lag_task: asyncio.Task | None，可选事件循环 lag 监控任务

    返回：
        None，优雅退出：停调度与行情，关 HTTP，收尾数据库
    """
    logger.info("正在关闭应用…")
    await ctx.scheduler.stop()
    await ctx.source.stop()
    if safety_task is not None:  # 先停安全网：防止 shutdown 途中又调度新的回填任务
        safety_task.cancel()
        await asyncio.gather(safety_task, return_exceptions=True)
    if ctx.trade_feed is not None:
        await ctx.trade_feed.stop()
    if ctx.trade_sync is not None:
        await ctx.trade_sync.aclose()  # 取消失漏 pnl 回填任务，须在 db.close 前
    if ctx.server is not None:
        ctx.server.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)
    for task in (
        pusher_task,
        funding_task,
        review_task,
        research_task,
        ctx.server_deps.indicators.oi_task,
        lag_task,
    ):
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    # 手动点火的后台研报/复盘：取消后走 agent 既有取消收尾（落失败报告），须在 db.close 前
    await ctx.review.scheduler.shutdown()
    await ctx.research.scheduler.shutdown()
    await ctx.db.close()
