"""研报子系统装配：四类数据源 + 聚合器 + 提示词 + agent/调度器组件束。

密钥只从 .env 读取（JIN10_MCP_TOKEN / BLOCKBEATS_API_KEY / FRED_API_KEY）；
key 缺失的源不装配（对应工具返回"未装配"哨兵），不阻塞其余源。
bootstrap 只做编排；研报提示词写回调（research_prompt_save/research_prompt_rollback，
供 server 端点注入）集中在这里（形状与 src/review/setup.py 的策略写回调一致）。
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
from src.research.prompt_store import ResearchPromptStore, ResearchPromptValidationError
from src.research.providers.base import ResearchDataProvider
from src.research.providers.blockbeats import BlockbeatsSource
from src.research.providers.fred import FredSource
from src.research.providers.jin10 import Jin10Source
from src.research.providers.polymarket import PolymarketSource
from src.research.prompts import ResearchPromptLoader
from src.research.scheduler import ResearchScheduler


@dataclass
class ResearchComponents:
    """研报子系统组件束：agent / 数据聚合器 / 定时调度器 / 提示词版本存储。"""

    agent: ResearchAgent
    data_provider: ResearchDataProvider
    scheduler: ResearchScheduler
    # 研报提示词版本存储（issue #113）：bootstrap 创建并完成播种/对账后注入，
    # 供 server 端版本查询与人工保存/回滚；测试直建组件束时可为 None
    prompt_store: ResearchPromptStore | None = None

    def _require_prompt_store(self) -> ResearchPromptStore:
        """取研报提示词版本存储；未装配（None）时抛 RuntimeError。

        参数：无

        返回：
            ResearchPromptStore：已装配的研报提示词版本存储

        异常：
            RuntimeError：prompt_store 为 None（组件束未经 bootstrap 完整装配）时抛出
        """
        if self.prompt_store is None:
            raise RuntimeError("研报提示词版本存储未装配")
        return self.prompt_store

    async def research_prompt_save(self, content: str) -> dict:
        """前端手动保存研报提示词：走 ResearchPromptStore（校验 + 版本落库 + 原子生效）。

        特殊语义：仅"与当前提示词无差异"一条校验失败时视为幂等成功（重复保存不产新版本）；
        其余校验失败原样上抛 ResearchPromptValidationError，由路由映 422。

        参数：
            content: str，前端提交的完整研报提示词文本

        返回：
            dict，保存状态及新版本编号；幂等保存时版本编号为 None
            （{"saved", "version"}，与策略保存同一返回键结构）

        异常：
            RuntimeError：研报提示词版本存储未装配时抛出
            ResearchPromptValidationError：内容存在无差异以外的校验错误时抛出
        """
        store = self._require_prompt_store()
        try:
            version = await store.revise_applied(content, reason="前端手动保存")
        except ResearchPromptValidationError as exc:
            if exc.no_diff_only:  # 唯一原因是"无差异"：结构化判定，不做文案子串匹配
                return {"saved": True, "version": None}
            raise
        return {"saved": True, "version": version.id}

    async def research_prompt_rollback(self, version_id: int) -> dict:
        """把研报提示词回滚到指定历史版本，并记录一条新的回滚版本。

        参数：
            version_id: int，作为回滚来源的历史版本编号

        返回：
            dict，来源版本编号与回滚后新版本编号
            （{"rolled_back_to", "version"}，与策略回滚同一返回键结构）

        异常：
            RuntimeError：研报提示词版本存储未装配时抛出
            ResearchPromptValidationError：目标版本不存在时抛出（路由映 404）
        """
        store = self._require_prompt_store()
        version = await store.rollback(version_id)
        return {"rolled_back_to": version_id, "version": version.id}


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
    prompt_store: ResearchPromptStore | None = None,
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
        prompt_store: ResearchPromptStore | None，研报提示词版本存储（bootstrap 已播种/对账，
            issue #113）；None 时组件束仅不携带该引用，不影响研报运行

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
        prompt_store=prompt_store,  # R6-4：prompt 正文/md5/版本归因经其锁内快照同刻取齐
    )
    scheduler = ResearchScheduler(settings, agent, repo)
    return ResearchComponents(
        agent=agent, data_provider=data_provider, scheduler=scheduler, prompt_store=prompt_store
    )
