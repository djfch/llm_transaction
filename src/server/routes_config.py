"""配置编辑端点：config.yaml / system_prompt.md / watchlist / secrets 状态 / kill_switch。

交易所与 LLM 的 API key 永不进入响应（secrets/status 只返回是否已配置的布尔值）。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from src.config import Settings, load_settings
from src.config_io import (
    read_settings_raw,
    read_watchlist_raw,
    write_settings,
    write_watchlist,
)
from src.server.deps import ServerDeps

# 变更后可原地写回运行时共享实例、下轮决策即生效的字段。生效依据（共享同一实例逐轮读取）：
# - risk.*：ToolDeps.risk_config 即 settings.risk，风控规则每次检查时读字段
# - llm.max_consecutive_failures：决策循环每轮失败处理时读取
# - scheduler.*：WakeupScheduler 每次钳制/重新武装定时器时读取
# - paper.slippage：PaperGateway 每次市价撮合时读取
# 其余字段（mode/gate/llm.model 等）在 gateway/provider/server 构造期绑定，须重启生效。
_RUNTIME_KEYS = frozenset(
    {
        "risk.max_position_pct",
        "risk.max_total_position_pct",
        "risk.max_leverage",
        "risk.daily_loss_limit",
        "risk.max_orders_per_day",
        "risk.max_deviation",
        "risk.kill_switch",
        "llm.max_consecutive_failures",
        "scheduler.default_wake_minutes",
        "scheduler.min_wake_minutes",
        "scheduler.max_wake_minutes",
        "paper.slippage",
    }
)


class KillSwitchBody(BaseModel):
    enabled: bool


def _changed_keys(old: BaseModel, new: BaseModel, prefix: str = "") -> list[str]:
    """递归比较两个同型模型，返回有差异的叶子字段点分路径（按模型字段声明序）。"""
    keys: list[str] = []
    for name in type(new).model_fields:
        vo, vn = getattr(old, name), getattr(new, name)
        dotted = f"{prefix}{name}"
        if isinstance(vo, BaseModel) and isinstance(vn, BaseModel):
            keys.extend(_changed_keys(vo, vn, f"{dotted}."))
        elif vo != vn:
            keys.append(dotted)
    return keys


def _write_back(runtime: Settings, source: Settings, keys: list[str]) -> None:
    """把指定点分叶子字段从 source 原地写入 runtime（保持同一实例，共享引用不失效）。"""
    for key in keys:
        parts = key.split(".")
        target: Any = runtime
        src: Any = source
        for part in parts[:-1]:
            target = getattr(target, part)
            src = getattr(src, part)
        setattr(target, parts[-1], getattr(src, parts[-1]))


def _config_422(exc: ValueError) -> HTTPException:
    """ConfigError 与 validate_mode 抛出的 ValueError 都转成 422。"""
    return HTTPException(status_code=422, detail=str(exc))


def create_config_router(deps: ServerDeps) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/config")
    async def get_config() -> dict[str, Any]:
        return read_settings_raw(deps.config_path)

    @router.put("/config")
    async def put_config(body: dict[str, Any]) -> dict[str, Any]:
        try:
            old = load_settings(deps.config_path)
        except ValueError:
            old = Settings()  # 旧文件非法时按默认值做变更对比（PUT 体整体校验覆盖）
        try:
            new = write_settings(body, deps.config_path)
        except ValueError as exc:  # ConfigError 及 validate_mode 的 ValueError
            raise _config_422(exc) from exc
        # 可变字段原地写回运行时共享实例（下轮决策即生效）；写不回的诚实标 needs_restart
        changed = _changed_keys(old, new)
        runtime = deps.runtime_settings
        applied = [k for k in changed if k in _RUNTIME_KEYS and runtime is not None]
        if applied:
            _write_back(runtime, new, applied)
        return {"saved": True, "needs_restart": [k for k in changed if k not in applied]}

    @router.get("/strategy", response_class=PlainTextResponse)
    async def get_strategy() -> str:
        """策略书按纯文本（text/plain）返回，与前端约定一致。"""
        if not deps.prompt_path.exists():
            return ""
        return deps.prompt_path.read_text(encoding="utf-8")

    @router.put("/strategy", response_class=PlainTextResponse)
    async def put_strategy(request: Request) -> str:
        body = (await request.body()).decode("utf-8")
        deps.prompt_path.write_text(body, encoding="utf-8")
        return body

    @router.get("/watchlist")
    async def get_watchlist() -> dict[str, Any]:
        return read_watchlist_raw(deps.watchlist_path)

    @router.put("/watchlist")
    async def put_watchlist(body: dict[str, Any]) -> dict[str, bool]:
        try:
            watchlist = write_watchlist(body, deps.watchlist_path)
        except ValueError as exc:
            raise _config_422(exc) from exc
        if deps.runtime_watchlist is not None:
            deps.runtime_watchlist[:] = watchlist.contracts  # 同一 list 原地更新，下轮生效
        return {"saved": True}

    @router.get("/secrets/status")
    async def secrets_status() -> dict[str, bool]:
        # 只查环境变量是否存在，永不返回明文
        env = os.environ
        return {
            "gate_key": bool(env.get("GATE_API_KEY")) and bool(env.get("GATE_API_SECRET")),
            "llm_key": bool(env.get("ANTHROPIC_API_KEY") or env.get("OPENAI_API_KEY")),
            "telegram": bool(env.get("TELEGRAM_BOT_TOKEN")) and bool(env.get("TELEGRAM_CHAT_ID")),
        }

    @router.post("/kill_switch")
    async def post_kill_switch(body: KillSwitchBody) -> dict[str, bool]:
        raw = read_settings_raw(deps.config_path)
        raw.setdefault("risk", {})["kill_switch"] = body.enabled
        try:
            write_settings(raw, deps.config_path)
        except ValueError as exc:
            raise _config_422(exc) from exc
        deps.notify_kill_switch(body.enabled)
        return {"kill_switch": body.enabled}

    return router
