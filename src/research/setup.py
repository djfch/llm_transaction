"""研报子系统装配：四类数据源 + 聚合器 + 提示词 + agent 组件束。

密钥只从 .env 读取（JIN10_MCP_TOKEN / BLOCKBEATS_API_KEY / FRED_API_KEY）；
key 缺失的源不装配（对应工具返回"未装配"哨兵），不阻塞其余源。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from src.audit.trail import AuditTrail
from src.config import ROOT, Settings
from src.memory.repo import Repo
from src.research.agent import ResearchAgent
from src.research.providers.base import ResearchDataProvider
from src.research.providers.blockbeats import BlockbeatsSource
from src.research.providers.fred import FredSource
from src.research.providers.jin10 import Jin10Source
from src.research.providers.polymarket import PolymarketSource
from src.research.prompts import ResearchPromptLoader


@dataclass
class ResearchComponents:
    """研报子系统组件束：provider（LLM）/ 数据聚合器 / agent。"""

    agent: ResearchAgent
    data_provider: ResearchDataProvider


def build_research(
    settings: Settings,
    repo: Repo,
    audit: AuditTrail,
    provider: object | None,
) -> ResearchComponents:
    """装配研报子系统。provider 为 LLM provider（可为 None：LLM 未配置时研报直接失败）。"""
    cfg = settings.research

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

    data_provider = ResearchDataProvider(
        jin10=jin10,
        blockbeats=blockbeats,
        fred=fred_src,
        polymarket=PolymarketSource(base_url=cfg.polymarket_base_url),
    )

    agent = ResearchAgent(
        settings=settings,
        provider=provider,  # type: ignore[arg-type]
        repo=repo,
        audit=audit,
        prompt_loader=ResearchPromptLoader(ROOT / "research_prompt.md"),
        data_provider=data_provider,
        max_turns=cfg.max_turns,
        timeout_seconds=cfg.timeout_seconds,
    )
    return ResearchComponents(agent=agent, data_provider=data_provider)
