"""研报子系统装配：四类数据源 + 聚合器 + 提示词 + agent/调度器组件束。

密钥只从 .env 读取（JIN10_MCP_TOKEN / BLOCKBEATS_API_KEY / FRED_API_KEY）；
key 缺失的源不装配（对应工具返回"未装配"哨兵），不阻塞其余源。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from src.audit.trail import AuditTrail
from src.config import ROOT, ResearchConfig, Settings
from src.memory.repo import Repo
from src.research.agent import ResearchAgent
from src.research.market_data import ResearchMarketDataService
from src.research.providers.base import ResearchDataProvider
from src.research.providers.blockbeats import BlockbeatsSource
from src.research.providers.fred import FredSource
from src.research.providers.jin10 import Jin10Source
from src.research.providers.polymarket import PolymarketSource
from src.research.prompts import ResearchPromptLoader
from src.research.scheduler import ResearchScheduler


@dataclass
class ResearchComponents:
    """研报子系统组件束：agent / 数据聚合器 / 定时调度器。"""

    agent: ResearchAgent
    data_provider: ResearchDataProvider
    scheduler: ResearchScheduler


def _build_data_provider(cfg: ResearchConfig) -> ResearchDataProvider:
    """按环境变量中的密钥装配可用研报数据源，缺少密钥的来源保持未配置。

    参数：
        cfg: ResearchConfig，研报外部数据源的地址与启动命令配置

    返回：
        ResearchDataProvider，包含当前可用来源的数据聚合器
    """
    jin10 = None
    token = os.environ.get("JIN10_MCP_TOKEN", "")
    if token:
        jin10 = Jin10Source(url=cfg.jin10_mcp_url, token=token)

    blockbeats = None
    if os.environ.get("BLOCKBEATS_API_KEY", ""):
        blockbeats = BlockbeatsSource(cmd=cfg.blockbeats_mcp_cmd)

    fred_src = None
    if os.environ.get("FRED_API_KEY", ""):
        fred_src = FredSource(base_url=cfg.fred_base_url)

    return ResearchDataProvider(
        jin10=jin10,
        blockbeats=blockbeats,
        fred=fred_src,
        polymarket=PolymarketSource(base_url=cfg.polymarket_base_url),
    )


def build_research(
    settings: Settings,
    repo: Repo,
    audit: AuditTrail,
    provider: object | None,
    notify_event: Callable[[dict], None] | None = None,
    candle_cache: object | None = None,
    gateway: object | None = None,
    watchlist: list[str] | None = None,
) -> ResearchComponents:
    """装配研报代理、数据聚合器和调度器，并建立可选市场数据与事件通道。

    参数：
        settings: Settings，研报运行参数与外部来源配置
        repo: Repo，共享持久化仓库
        audit: AuditTrail，研报轮次与工具调用审计入口
        provider: object | None，LLM 提供器；未配置时研报运行会返回失败
        notify_event: Callable[[dict], None] | None，轮次事件广播回调
        candle_cache: object | None，逐标的 K 线缓存
        gateway: object | None，逐标的行情与合约查询网关
        watchlist: list[str] | None，本轮允许研究的合约白名单

    返回：
        ResearchComponents，已接线的研报代理、数据聚合器与调度器
    """
    cfg = settings.research
    data_provider = _build_data_provider(cfg)
    market_data = None
    if candle_cache is not None and gateway is not None:
        market_data = ResearchMarketDataService(candle_cache, gateway)  # type: ignore[arg-type]
    agent = ResearchAgent(
        settings=settings,
        provider=provider,  # type: ignore[arg-type]
        repo=repo,
        audit=audit,
        prompt_loader=ResearchPromptLoader(ROOT / "research_prompt.md"),
        data_provider=data_provider,
        notify_event=notify_event,
        max_turns=cfg.max_turns,
        market_data=market_data,
        watchlist=watchlist or [],
        timeout_seconds=cfg.timeout_seconds,
    )
    scheduler = ResearchScheduler(settings, agent, repo)
    return ResearchComponents(agent=agent, data_provider=data_provider, scheduler=scheduler)
