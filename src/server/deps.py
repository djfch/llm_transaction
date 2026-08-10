"""server 层依赖注入容器：运行时依赖全部由构造参数注入。

server 不 import agent/scheduler/market 的具体实现；gateway/repo/audit_trail、
运行时状态、kill_switch 回调、ws 事件源都由主程序（Step 13）接线，测试用 fake 注入。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from src.audit.trail import AuditTrail
from src.config import ROOT, Settings
from src.gateway.base import Gateway
from src.memory.repo import Repo


class IndicatorBundle(Protocol):
    """指标子系统回调束：路由经此触达指标计算与短名单版本管理（实现见 market/indicators_setup）。

    结构化类型：server 不 import market 指标实现，仅按方法签名取用。
    - panel(contract, interval)：全指标面板（shortlist 已由装配层合并当前值）；
    - series(contract, interval, keys, limit)：逐根序列，keys=None 用当前短名单，
      未知 key 抛 ValueError（路由映 422）；
    - config_get()：{"shortlist", "available"}（available 取自指标注册表）；
    - config_revise(shortlist, reason)：人工修订（created_by='human'），
      校验失败抛 IndicatorConfigValidationError（路由映 422）；
    - config_rollback(version_id)：回滚，版本不存在抛 IndicatorConfigValidationError（路由映 404）。
    """

    oi_task: asyncio.Task | None  # OI 后台刷新任务句柄（主程序 shutdown 取消）

    def panel(self, contract: str, interval: str) -> dict:
        """读取指定合约某周期的全指标面板，并附上当前生效的指标短名单。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，K 线周期（如 4h、1d）

        返回：
            dict：全指标面板（shortlist 已由装配层合并当前值）
        """
        ...

    def series(self, contract: str, interval: str, keys: list[str] | None, limit: int) -> dict:
        """读取指定合约某周期下选定指标的逐根 K 线序列。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，K 线周期（如 4h、1d）
            keys: list[str] | None，指标键列表；省略（None）时使用当前短名单
            limit: int，返回的最近 K 线根数上限

        返回：
            dict：按指标键组织的逐根序列数据
        """
        ...

    def config_get(self) -> dict:
        """读取当前指标短名单与注册表全集。

        参数：无

        返回：
            dict：{"shortlist", "available"}，available 取自指标注册表，
            供前端指标选择器展示
        """
        ...

    async def config_revise(self, shortlist: list[str], reason: str) -> dict:
        """人工修订指标短名单并落为新版本（created_by='human'）。

        参数：
            shortlist: list[str]，新的指标短名单键列表
            reason: str，修订原因说明

        返回：
            dict：{"ok": True, "version_id"}，version_id 为新版本号
        """
        ...

    async def config_rollback(self, version_id: int) -> dict:
        """把指标短名单回滚到指定历史版本（记 created_by='rollback' 新版本）。

        参数：
            version_id: int，目标历史版本号

        返回：
            dict：{"rolled_back_to", "version_id"}，分别为回滚目标版本号与新版本号
        """
        ...


@dataclass
class ServerDeps:
    """create_app 的全部运行时依赖与文件路径。

    gateway 为 None 时账户/持仓端点返回 503；各 path 默认指向项目根目录下的文件，
    测试中用 tmp_path 覆盖以隔离真实配置。

    runtime_settings / runtime_watchlist 是与 agent 循环共享的运行时对象：
    配置端点写回文件后把可变字段原地写入它们，使变更下轮决策即生效
    （须与 DecisionLoop/WakeupScheduler 持有的是同一实例，由主程序接线保证）；
    未接线（None）时配置端点把相应变更诚实标注 needs_restart。
    """

    repo: Repo
    audit_trail: AuditTrail | None = None
    gateway: Gateway | None = None
    status_provider: Callable[[], dict[str, Any]] | None = None
    on_kill_switch: Callable[[bool], None] | None = None
    # 写操作回调（主程序接线；None 时对应端点 503/409，见 routes_trading）：
    # manual_close 与 LLM 平仓同一风控路径；paper_reset 仅 paper 模式注入；
    # agent_start/agent_stop 启停决策调度器（运行态经 status_provider 读取）
    manual_close: Callable[[str], Awaitable[dict]] | None = None
    # 手动撤单回调：必须走 gateway，并由 agent 层同步本地订单状态。
    manual_cancel_order: Callable[[str, str], Awaitable[dict]] | None = None
    paper_reset: Callable[[Decimal], None] | None = None
    agent_start: Callable[[], Awaitable[None]] | None = None
    agent_stop: Callable[[], Awaitable[None]] | None = None
    # LLM 热重建回调（主程序接线）：改 key/模型后重建 provider 并热替换；
    # None 时相关端点诚实回报 "agent 未接线"。契约 {"llm_configured": bool, "error": str}
    llm_reconfigure: Callable[[], Awaitable[dict]] | None = None
    # 未触发价格预警线提供者（主程序接线，返回 TriggerManager.list() 的快照）：
    # server 不 import market 具体实现，路由按 duck-typing 读 id/contract/direction/price/created_at；
    # None 时 /api/alerts 诚实 503
    alerts_provider: Callable[[], list[Any]] | None = None
    # 复盘/策略版本写回调（主程序接线；None 时对应端点诚实 503）：
    # review_run 手动触发一次复盘，可选 period_start/period_end 指定补跑区间
    # （409 进行中/503 未配置/422 区间非法由路由按返回的 error_code 映射）；
    # strategy_save 经 StrategyStore 落版本（校验失败抛 StrategyValidationError，路由映 422）；
    # strategy_rollback 回滚到指定版本（版本不存在抛 StrategyValidationError，路由映 404）
    review_run: Callable[..., Awaitable[dict]] | None = None
    strategy_save: Callable[[str], Awaitable[dict]] | None = None
    strategy_rollback: Callable[[int], Awaitable[dict]] | None = None
    # 手动触发研报（主程序接线；None 时端点 503）：
    # 可选 report_type/hours 指定类型与回看窗口（409 进行中/503 LLM 未配置由路由按 error_code 映射）
    research_run: Callable[..., Awaitable[dict]] | None = None
    # 指标子系统回调束（主程序装配注入；None 时 /api/indicators* 与 /api/indicator_config
    # 写端点诚实 503；版本族读端点经 repo.indicator_config 直取，同策略版本先例）
    indicators: IndicatorBundle | None = None
    event_queue: asyncio.Queue[dict[str, Any]] | None = None
    runtime_settings: Settings | None = None
    runtime_watchlist: list[str] | None = None
    config_path: Path = field(default_factory=lambda: ROOT / "config.yaml")
    watchlist_path: Path = field(default_factory=lambda: ROOT / "watchlist.yaml")
    prompt_path: Path = field(default_factory=lambda: ROOT / "system_prompt.md")
    env_path: Path = field(default_factory=lambda: ROOT / ".env")
    web_dist: Path = field(default_factory=lambda: ROOT / "web" / "dist")

    def runtime_status(self) -> dict[str, Any]:
        """读取运行时状态（uptime 等）；未注入时返回空 dict。

        参数：无

        返回：
            dict[str, Any]，读取运行时状态（uptime 等）；未注入时返回空 dict
        """
        if self.status_provider is None:
            return {}
        return self.status_provider()

    def notify_kill_switch(self, enabled: bool) -> None:
        """kill_switch 写回配置后回调主程序；未注入时静默跳过。

        参数：
            enabled: bool，熔断开关是否启用

        返回：
            None，kill_switch 写回配置后回调主程序；未注入时静默跳过
        """
        if self.on_kill_switch is not None:
            self.on_kill_switch(enabled)
