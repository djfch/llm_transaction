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
    """按 .env 密钥装配四类数据源；key 缺失的源不装配（None），不阻塞其余源。"""
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
    """装配研报子系统。provider 为 LLM provider（可为 None：LLM 未配置时研报直接失败）。

    notify_event 为 WS 事件广播回调（轮始/轮末），None 则不广播（测试/未接线场景）。
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
