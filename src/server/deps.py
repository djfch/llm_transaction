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
from typing import Any

from src.audit.trail import AuditTrail
from src.config import ROOT, Settings
from src.gateway.base import Gateway
from src.memory.repo import Repo


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
    # 复盘/策略版本写回调（主程序接线；None 时对应端点诚实 503）：
    # review_run 手动触发一次复盘，可选 period_start/period_end 指定补跑区间
    # （409 进行中/503 未配置/422 区间非法由路由按返回的 error_code 映射）；
    # strategy_save 经 StrategyStore 落版本（校验失败抛 StrategyValidationError，路由映 422）；
    # strategy_rollback 回滚到指定版本（版本不存在抛 StrategyValidationError，路由映 404）
    review_run: Callable[..., Awaitable[dict]] | None = None
    strategy_save: Callable[[str], Awaitable[dict]] | None = None
    strategy_rollback: Callable[[int], Awaitable[dict]] | None = None
    event_queue: asyncio.Queue[dict[str, Any]] | None = None
    runtime_settings: Settings | None = None
    runtime_watchlist: list[str] | None = None
    config_path: Path = field(default_factory=lambda: ROOT / "config.yaml")
    watchlist_path: Path = field(default_factory=lambda: ROOT / "watchlist.yaml")
    prompt_path: Path = field(default_factory=lambda: ROOT / "system_prompt.md")
    env_path: Path = field(default_factory=lambda: ROOT / ".env")
    web_dist: Path = field(default_factory=lambda: ROOT / "web" / "dist")

    def runtime_status(self) -> dict[str, Any]:
        """读取运行时状态（uptime 等）；未注入时返回空 dict。"""
        if self.status_provider is None:
            return {}
        return self.status_provider()

    def notify_kill_switch(self, enabled: bool) -> None:
        """kill_switch 写回配置后回调主程序；未注入时静默跳过。"""
        if self.on_kill_switch is not None:
            self.on_kill_switch(enabled)
