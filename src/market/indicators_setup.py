"""指标子系统装配：OpenInterestCache/IndicatorService/IndicatorConfigStore 创建、播种与接线。

bootstrap 只做编排；指标组件创建与 server 端点回调束（panel/series/config_get/
config_revise/config_rollback，经 ServerDeps.indicators 注入）集中在这里，
镜像 src/review/setup.py 的装配模式（组件束 + 写回调方法）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.config import ROOT
from src.memory.repo import Repo
from src.review.indicator_config import IndicatorConfigStore

from .indicator_service import REGISTRY, CandleCacheLike, IndicatorService
from .oi import OiGateway, OpenInterestCache

OI_REFRESH_SECONDS = 60  # OI 后台刷新周期（秒）


@dataclass
class IndicatorComponents:
    """指标子系统组件束：service/store/oi_cache + server 回调方法 + OI 后台任务句柄。

    ServerDeps.indicators 回调束的实现面（结构化 Protocol 见 server/deps.py）；
    oi_task 由 setup_indicators 创建，主程序 shutdown 取消（同其他后台 task 模式）。
    """

    service: IndicatorService
    store: IndicatorConfigStore
    oi_cache: OpenInterestCache
    oi_task: asyncio.Task | None = None

    def panel(self, contract: str, interval: str) -> dict:
        """全指标面板 + 当前短名单（路由契约：full_panel 基础上补 shortlist）。

        参数：
            contract: str，合约标识
            interval: str，K 线周期

        返回：
            dict，全指标面板 + 当前短名单（路由契约：full_panel 基础上补 shortlist）
        """
        panel = self.service.full_panel(contract, interval)
        panel["shortlist"] = list(self.store.load_current().shortlist)
        return panel

    def series(self, contract: str, interval: str, keys: list[str] | None, limit: int) -> dict:
        """逐根序列；keys=None 用当前短名单（未知 key 由 service 抛 ValueError，路由映 422）。

        参数：
            contract: str，合约标识
            interval: str，K 线周期
            keys: list[str] | None，需要查询的指标键列表
            limit: int，返回记录数量上限

        返回：
            dict，逐根序列；keys=None 用当前短名单（未知 key 由 service 抛 ValueError，路由映 422）
        """
        effective = keys or list(self.store.load_current().shortlist)
        return self.service.series(contract, interval, effective, limit)

    def shortlist_keys(self) -> list[str]:
        """当前短名单键列表：每次调用重读配置（复盘修订后下一轮决策上下文即生效）。

        参数：无

        返回：
            list[str]，当前短名单键列表：每次调用重读配置（复盘修订后下一轮决策上下文即生效）
        """
        return list(self.store.load_current().shortlist)

    def config_get(self) -> dict:
        """当前短名单 + 注册表全集（available 供前端指标选择器展示）。

        参数：无

        返回：
            dict，当前短名单 + 注册表全集（available 供前端指标选择器展示）
        """
        return {
            "shortlist": list(self.store.load_current().shortlist),
            "available": [
                {"key": key, "label": d.label, "kind": d.kind, "fields": list(d.fields)}
                for key, d in REGISTRY.items()
            ],
        }

    async def config_revise(self, shortlist: list[str], reason: str) -> dict:
        """人工修订短名单（created_by='human'）；校验失败上抛，路由映 422。

        参数：
            shortlist: list[str]，新的指标短名单
            reason: str，指标短名单修订原因

        返回：
            dict，人工修订短名单（created_by='human'）；校验失败上抛，路由映 422
        """
        version = await self.store.revise(shortlist, created_by="human", reason=reason)
        await self.store.apply_version(version.id)  # 人工调整即时生效（草稿仅限复盘链路）
        return {"ok": True, "version_id": version.id}

    async def config_rollback(self, version_id: int) -> dict:
        """回滚到历史版本（记 created_by='rollback' 新版本）；不存在上抛，路由映 404。

        参数：
            version_id: int，策略版本编号

        返回：
            dict，回滚到历史版本（记 created_by='rollback' 新版本）；不存在上抛，路由映 404
        """
        version = await self.store.rollback(version_id)
        return {"rolled_back_to": version_id, "version_id": version.id}

    async def oi_refresh_loop(self) -> None:
        """OI 后台刷新：启动即拉一轮，随后每 60s 一轮；由 shutdown 取消而结束。

        参数：无

        返回：
            None，OI 后台刷新：启动即拉一轮，随后每 60s 一轮；由 shutdown 取消而结束
        """
        while True:
            await self.oi_cache.refresh_once()
            await asyncio.sleep(OI_REFRESH_SECONDS)


async def setup_indicators(
    repo: Repo,
    gateway: OiGateway,
    candle_cache: CandleCacheLike,
    watchlist: list[str],
    event_queue: asyncio.Queue,
    *,
    config_path: Path | None = None,
    notify_event: Callable[[dict], None] | None = None,
) -> IndicatorComponents:
    """装配指标子系统：配置库播种 v1，创建 OI 后台刷新任务，返回组件束。

    路径默认取 ROOT 下运行时文件（测试经 config_path 隔离）；watchlist 为共享引用
    （原地更新自选即生效）。配置变更（revise/rollback/人工 PUT 同走 store）即广播
    indicator_config_updated：notify_event 缺省时取 event_queue.put_nowait。

    参数：
        repo: Repo，数据仓储
        gateway: OiGateway，交易网关
        candle_cache: CandleCacheLike，K 线缓存实例
        watchlist: list[str]，关注合约配置
        event_queue: asyncio.Queue，指标配置更新事件队列
        config_path: Path | None，可选配置文件路径
        notify_event: Callable[[dict], None] | None，可选事件通知回调

    返回：
        IndicatorComponents，装配指标子系统：配置库播种 v1，创建 OI 后台刷新任务，返回组件束。  路径默认取 ROOT 下运行时文件（测试经 config_path 隔离）；watchlist 为共享引用 （原地更新自选即生效）。配置变更（revise/rollback/人工 PUT 同走 store）即广播 indicator_config_updated：notify_event 缺省时取 event_queue.put_nowait

    """
    oi_cache = OpenInterestCache(gateway, watchlist)
    service = IndicatorService(candle_cache, oi_cache)
    notify = notify_event or event_queue.put_nowait
    store = IndicatorConfigStore(
        config_path or ROOT / "indicator_config.yaml",
        repo,
        valid_keys=frozenset(REGISTRY),
        on_change=lambda: notify({"type": "indicator_config_updated"}),
    )
    await store.seed_if_empty()
    components = IndicatorComponents(service=service, store=store, oi_cache=oi_cache)
    components.oi_task = asyncio.create_task(components.oi_refresh_loop())
    return components
