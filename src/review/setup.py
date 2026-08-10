"""复盘子系统装配：StrategyStore/ReviewAgent/ReviewScheduler 创建 + 策略版本库播种。

bootstrap 只做编排；复盘组件创建与策略书写回调（strategy_save/strategy_rollback，
供 server 端点注入）集中在这里。
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from src.audit.trail import AuditTrail
from src.config import ROOT, Settings
from src.market.indicator_service import IndicatorService
from src.memory.repo import Repo
from src.notify.telegram import build_notifier
from src.review.agent import ReviewAgent
from src.review.indicator_config import IndicatorConfigStore
from src.review.prompts import ReviewPromptLoader
from src.review.scheduler import ReviewScheduler
from src.review.strategy import StrategyStore, StrategyValidationError


@dataclass
class ReviewComponents:
    """复盘子系统组件束：三大组件 + 策略写回调（server 端点经 ServerDeps 注入后两者）。"""

    store: StrategyStore
    agent: ReviewAgent
    scheduler: ReviewScheduler

    async def strategy_save(self, content: str) -> dict:
        """前端手动保存策略书：走 StrategyStore（与复盘改写同一路径，版本落库）。

        特殊语义：仅"与当前策略书无差异"一条校验失败时视为幂等成功（重复保存不产新版本）；
        其余校验失败原样上抛 StrategyValidationError，由路由映 422。

        参数：
            content: str，前端提交的完整策略书文本

        返回：
            dict，保存状态及新版本编号；幂等保存时版本编号为 None

        异常：
            StrategyValidationError: 策略内容存在无差异以外的校验错误时抛出
        """
        try:
            version = await self.store.revise(content, reason="前端手动保存", created_by="human")
        except StrategyValidationError as exc:
            if exc.no_diff_only:  # 唯一原因是"无差异"：结构化判定，不做文案子串匹配
                return {"saved": True, "version": None}
            raise
        return {"saved": True, "version": version.id}

    async def strategy_rollback(self, version_id: int) -> dict:
        """把策略书回滚到指定历史版本，并记录一条新的回滚版本。

        参数：
            version_id: int，作为回滚来源的历史版本编号

        返回：
            dict，来源版本编号与回滚后新版本编号
        """
        version = await self.store.rollback(version_id)
        return {"rolled_back_to": version_id, "version": version.id}


async def build_review(
    settings: Settings,
    repo: Repo,
    audit: AuditTrail,
    provider: Any,  # LLMProvider | None：按 reviewer 绑定凭证构造（与决策循环各自独立实例）
    *,
    strategy_path: Path | None = None,
    review_prompt_path: Path | None = None,
    notify_event: Callable[[dict], None] | None = None,
    indicator_service: IndicatorService | None = None,
    indicator_config_store: IndicatorConfigStore | None = None,
    watchlist: Iterable[str] | None = None,
) -> ReviewComponents:
    """创建复盘子系统组件：策略版本库播种 v1；复盘 agent 复用同一 provider/audit/repo。

    路径默认取 ROOT 下运行时文件（调用期解析，测试可 monkeypatch 本模块 ROOT 隔离）；
    notify_event 接线后，策略书任一路径变更（复盘修订/手动保存/回滚）即广播 strategy_updated，
    复盘轮开始/结束另广播 review_round_start/review_round（透传给 ReviewAgent）。
    指标三件套（service/config_store/watchlist）缺省 None：指标工具降级为中文提示。

    参数：
        settings: Settings，复盘频率、模型与通知等运行配置
        repo: Repo，共享持久化仓库
        audit: AuditTrail，复盘轮次与工具调用审计入口
        provider: Any，复盘专用 LLM 提供器或未配置状态
        strategy_path: Path | None，策略书运行时文件路径
        review_prompt_path: Path | None，复盘提示词运行时文件路径
        notify_event: Callable[[dict], None] | None，策略和轮次事件广播回调
        indicator_service: IndicatorService | None，技术指标查询服务
        indicator_config_store: IndicatorConfigStore | None，指标短名单版本存储
        watchlist: Iterable[str] | None，允许复盘工具查询的合约集合

    返回：
        ReviewComponents，已播种并完成接线的策略存储、复盘代理和调度器
    """
    on_change = None if notify_event is None else lambda: notify_event({"type": "strategy_updated"})
    store = StrategyStore(strategy_path or ROOT / "system_prompt.md", repo, on_change=on_change)
    await store.seed_if_empty()
    agent = ReviewAgent(
        settings=settings,
        provider=provider,
        repo=repo,
        audit=audit,
        store=store,
        prompt_loader=ReviewPromptLoader(review_prompt_path or ROOT / "review_prompt.md"),
        on_alert=build_notifier(settings.notify).send,  # 与决策循环同款通知通道
        notify_event=notify_event,  # 复盘轮始末事件直推 WS 广播队列
        indicator_service=indicator_service,
        indicator_config_store=indicator_config_store,
        watchlist=watchlist,
    )
    scheduler = ReviewScheduler(settings, agent, repo)
    return ReviewComponents(store=store, agent=agent, scheduler=scheduler)
