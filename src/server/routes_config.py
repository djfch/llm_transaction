"""配置编辑端点：config.yaml / system_prompt.md / watchlist / secrets / kill_switch。

密钥铁规：交易所 key 无任何前端写入端点；LLM key 只经 POST /api/secrets 写入 .env，
任何 API 响应永不包含密钥明文（secrets/status 只返回是否已配置的布尔值）。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, field_validator

from src.config import Settings, load_settings
from src.config_io import (
    ConfigError,
    read_settings_raw,
    read_watchlist_raw,
    set_env_keys,
    write_settings,
    write_watchlist,
)
from src.server.deps import ServerDeps

# 变更后可原地写回运行时共享实例、下轮决策即生效的字段。生效依据（共享同一实例逐轮读取）：
# - risk.*：ToolDeps.risk_config 即 settings.risk，风控规则每次检查时读字段
# - llm.max_consecutive_failures：决策循环每轮失败处理时读取
# - scheduler.*：WakeupScheduler 每次钳制/重新武装定时器时读取
# - paper.slippage：PaperGateway 每次市价撮合时读取
# - llm.provider/model/max_tokens/openai_base_url：写回后经 llm_reconfigure 重建 provider
#   并热替换（set_provider），下轮决策即生效；重建失败保留旧 provider 且诚实回报 llm_error
# 其余字段（mode/gate 等）在 gateway/server 构造期绑定，须重启生效。
_RUNTIME_KEYS = frozenset(
    {
        "risk.max_position_pct",
        "risk.max_total_position_pct",
        "risk.max_leverage",
        "risk.daily_loss_limit",
        "risk.max_orders_per_day",
        "risk.max_deviation",
        "risk.kill_switch",
        "llm.provider",
        "llm.model",
        "llm.max_tokens",
        "llm.openai_base_url",
        "llm.max_consecutive_failures",
        "scheduler.default_wake_minutes",
        "scheduler.min_wake_minutes",
        "scheduler.max_wake_minutes",
        "paper.slippage",
    }
)

# 变更后须触发 LLM 热重建的热键（_RUNTIME_KEYS 子集）
_LLM_HOT_KEYS = frozenset({"llm.provider", "llm.model", "llm.max_tokens", "llm.openai_base_url"})


class KillSwitchBody(BaseModel):
    enabled: bool


class SecretsBody(BaseModel):
    """POST /api/secrets 请求体：只接受 LLM key（交易所 key 无写入端点）；空串表示不修改。"""

    anthropic_api_key: str = ""
    openai_api_key: str = ""

    @field_validator("anthropic_api_key", "openai_api_key")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        """拒绝换行/回车/NUL 等控制字符（防 .env 换行注入，见 set_env_keys 同名防护）。"""
        if any(c in value for c in ("\r", "\n", "\0")):
            raise ValueError("密钥值不允许包含换行/回车/NUL 字符")
        return value


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


def _merge_body(raw: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """把 PUT 体合并到现有配置原文上（按段浅合并）。

    前端只提交它管理的字段子集（mode/llm/risk/scheduler/notify），直接整体写回会把
    未提交的段（gate/paper/server/audit/log）重置为默认值——合并保证未提及的键原样保留。
    """
    merged = dict(raw)
    for key, value in body.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


async def _run_llm_reconfigure(deps: ServerDeps) -> dict[str, Any]:
    """调用主程序注入的 LLM 热重建回调；未接线时诚实标注 agent 未接线。"""
    if deps.llm_reconfigure is None:
        return {"llm_configured": False, "error": "agent 未接线"}
    return await deps.llm_reconfigure()


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
            new = write_settings(
                _merge_body(read_settings_raw(deps.config_path), body), deps.config_path
            )
        except ValueError as exc:  # ConfigError 及 validate_mode 的 ValueError
            raise _config_422(exc) from exc
        # 可变字段原地写回运行时共享实例（下轮决策即生效）；写不回的诚实标 needs_restart
        changed = _changed_keys(old, new)
        runtime = deps.runtime_settings
        applied = [k for k in changed if k in _RUNTIME_KEYS and runtime is not None]
        if applied:
            _write_back(runtime, new, applied)
        result: dict[str, Any] = {
            "saved": True,
            "needs_restart": [k for k in changed if k not in applied],
        }
        if any(k in _LLM_HOT_KEYS for k in applied):
            # llm 热键已写回：热重建 provider（失败不 422，配置合法已落盘）
            reconfigure = await _run_llm_reconfigure(deps)
            result["llm_configured"] = reconfigure["llm_configured"]
            result["llm_error"] = reconfigure["error"]
        return result

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

    @router.post("/secrets")
    async def post_secrets(body: SecretsBody) -> dict[str, Any]:
        """写入 LLM key 到 .env 并热重建 provider；响应永不回显明文。

        契约逐字：{"saved": true, "llm_configured": bool, "error": str}。
        空串字段不修改（set_env_keys 空值跳过）；交易所 key 无写入端点（密钥铁规）。
        """
        try:
            set_env_keys(
                {
                    "ANTHROPIC_API_KEY": body.anthropic_api_key,
                    "OPENAI_API_KEY": body.openai_api_key,
                },
                deps.env_path,
            )
        except ConfigError as exc:  # 控制字符注入防护（双层之一，SecretsBody 已先拦）
            raise _config_422(exc) from exc
        reconfigure = await _run_llm_reconfigure(deps)
        return {"saved": True, **reconfigure}

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
