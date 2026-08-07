"""配置的读写与校验：供 server 层的前端配置编辑接口复用。

写回前先用 pydantic 模型整体校验，非法值抛 ConfigError（由 server 层转成 422）。
set_env_keys 负责 .env 密钥落盘：只写指定 key，永不返回/记录 value。
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .config import ENV_KEY_PREFIX, ENV_KEY_WHITELIST, ROOT, IndicatorConfig, Settings, Watchlist


class ConfigError(ValueError):
    """配置校验失败，携带字段级错误信息。"""


def _yaml_safe(obj: Any) -> Any:
    """递归把 Decimal 转成 float，使 yaml.safe_dump 可用。

    配置文件是人读写的标量数值（如 initial_equity），float 表示足够；
    重读时 pydantic 再从字符串形式还原为精确 Decimal。
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _yaml_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_yaml_safe(v) for v in obj]
    return obj


def read_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def read_settings_raw(config_path: Path | None = None) -> dict:
    return read_raw(config_path or ROOT / "config.yaml")


def write_settings(data: dict, config_path: Path | None = None) -> Settings:
    """校验并写回 config.yaml，返回校验后的 Settings。"""
    try:
        settings = Settings(**data)
        settings.validate_mode()
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    path = config_path or ROOT / "config.yaml"
    path.write_text(
        # 全量落盘（含默认字段）：GET /api/config 直接读文件，缺字段会破坏前端契约
        yaml.safe_dump(_yaml_safe(settings.model_dump()), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return settings


def read_watchlist_raw(path: Path | None = None) -> dict:
    return read_raw(path or ROOT / "watchlist.yaml")


# 经 API 写 .env 的键名白名单：仅 LLM key（防经配置接口篡改 GATE_API_KEY 等交易所密钥）。
# 常量定义在 config.py，与 CredentialConfig.api_key_env 校验共用同一份（防漂移）。
def _check_env_key_allowed(key: str) -> None:
    if key not in ENV_KEY_WHITELIST and not key.startswith(ENV_KEY_PREFIX):
        raise ConfigError(f".env 写入拒绝白名单外键名：{key}")


def set_env_keys(mapping: dict[str, str], env_path: Path) -> list[str]:
    """把 mapping 中的 key 写入 .env：已存在则替换该行，缺失则文件末尾追加。

    只写 mapping 里的 key，其他行与注释（含 # KEY= 形式）原样保留；空值跳过不写
    （文件不存在且无可写值时不创建）。写入成功的 key 同步进 os.environ。
    键名必须在白名单内（ANTHROPIC_API_KEY / OPENAI_API_KEY / LLM_KEY_*），其余抛 ConfigError。
    密钥铁规：返回值只含写入的 key 名，永不返回/记录 value。
    """
    for key, value in mapping.items():
        # 防换行注入：控制字符可在 .env 注入任意新行（防御纵深，与 SecretsBody 校验双层）
        if any(c in key + value for c in ("\r", "\n", "\0")):
            raise ConfigError(f".env 写入拒绝控制字符（\\r/\\n/\\0）：{key}")
        _check_env_key_allowed(key)
    pending = {k: v for k, v in mapping.items() if v}  # 空值跳过
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out: list[str] = []
    written: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = ""
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
        if key and key in pending:
            out.append(f"{key}={pending.pop(key)}")
            written.append(key)
        else:
            out.append(line)
    for key, value in pending.items():  # 文件中不存在的 key 末尾追加
        out.append(f"{key}={value}")
        written.append(key)
    if out:
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    for key in written:
        os.environ[key] = mapping[key]  # 同步当前进程环境变量
    return written


def write_watchlist(data: dict, path: Path | None = None) -> Watchlist:
    try:
        watchlist = Watchlist(**data)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    if not watchlist.contracts:
        raise ConfigError("watchlist.contracts 不能为空")
    target = path or ROOT / "watchlist.yaml"
    target.write_text(
        yaml.safe_dump(watchlist.model_dump(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return watchlist


def write_indicator_config(path: Path, cfg: IndicatorConfig | dict) -> IndicatorConfig:
    """校验并写回 indicator_config.yaml（内容为 shortlist 键列表），返回校验后的模型。

    接受已校验的 IndicatorConfig 或待校验 dict；形状校验（去重/长度/字符集）走模型本身。
    """
    try:
        config = cfg if isinstance(cfg, IndicatorConfig) else IndicatorConfig(**cfg)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    path.write_text(
        yaml.safe_dump(config.model_dump(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return config
