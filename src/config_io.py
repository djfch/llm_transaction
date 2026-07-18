"""配置的读写与校验：供 server 层的前端配置编辑接口复用。

写回前先用 pydantic 模型整体校验，非法值抛 ConfigError（由 server 层转成 422）。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .config import ROOT, Settings, Watchlist


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
        yaml.safe_dump(_yaml_safe(settings.model_dump()), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return settings


def read_watchlist_raw(path: Path | None = None) -> dict:
    return read_raw(path or ROOT / "watchlist.yaml")


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
