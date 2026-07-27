"""配置加载与写回校验的测试。"""

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import ReviewConfig, SchedulerConfig, load_settings, load_watchlist
from src.config_io import ConfigError, write_settings, write_watchlist


def test_load_default_settings():
    settings = load_settings()
    assert settings.mode == "paper"
    assert settings.risk.max_leverage == 5
    assert settings.gate.settle == "usdt"


def test_load_watchlist():
    watchlist = load_watchlist()
    assert "BTC_USDT" in watchlist.contracts


def test_invalid_mode_rejected(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mode: mars", encoding="utf-8")
    with pytest.raises(ValueError, match="非法 mode"):
        load_settings(cfg)


def test_write_settings_roundtrip(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    saved = write_settings({"mode": "testnet", "risk": {"max_leverage": 3}}, cfg)
    assert saved.mode == "testnet"
    reloaded = load_settings(cfg)
    assert reloaded.risk.max_leverage == 3


def test_write_settings_invalid_risk(tmp_path: Path):
    with pytest.raises(ConfigError):
        write_settings({"risk": {"max_leverage": 0}}, tmp_path / "c.yaml")


def test_write_watchlist_empty_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="不能为空"):
        write_watchlist({"contracts": []}, tmp_path / "w.yaml")


# ---------- SchedulerConfig 取值关系校验 ----------


def test_scheduler_min_gt_max_rejected():
    """min_wake_minutes 不得大于 max_wake_minutes。"""
    with pytest.raises(ValidationError, match="min_wake_minutes"):
        SchedulerConfig(min_wake_minutes=60, max_wake_minutes=30)


def test_scheduler_default_outside_range_rejected():
    """default_wake_minutes 必须落在 [min, max] 之间。"""
    with pytest.raises(ValidationError, match="default_wake_minutes"):
        SchedulerConfig(default_wake_minutes=1000)  # 超过默认上限 720
    with pytest.raises(ValidationError, match="default_wake_minutes"):
        SchedulerConfig(default_wake_minutes=1, min_wake_minutes=5)  # 低于下限


def test_scheduler_max_over_720_rejected():
    """max_wake_minutes 上限为 720（12 小时）。"""
    with pytest.raises(ValidationError):
        SchedulerConfig(max_wake_minutes=721)


def test_scheduler_valid_combo_accepted():
    cfg = SchedulerConfig(default_wake_minutes=30, min_wake_minutes=10, max_wake_minutes=120)
    assert cfg.max_wake_minutes == 120


# ---------- ReviewConfig.daily_time 校验 ----------


def test_review_config_defaults():
    """复盘配置默认值：启用、每日 03:00（本地时间）。"""
    cfg = ReviewConfig()
    assert cfg.enabled is True
    assert cfg.daily_time == "03:00"


def test_review_daily_time_valid():
    assert ReviewConfig(daily_time="23:59").daily_time == "23:59"
    assert ReviewConfig(daily_time="0:00").daily_time == "0:00"


def test_review_daily_time_invalid():
    """非法 daily_time：越界时刻、非 HH:MM 结构均拒绝。"""
    for bad in ["24:00", "12:60", "-1:30", "3点", "03:00:00", "", "ab:cd"]:
        with pytest.raises(ValidationError, match="daily_time"):
            ReviewConfig(daily_time=bad)


# ---------- PaperConfig.initial_equity 为 Decimal ----------


def test_paper_initial_equity_decimal_from_yaml(tmp_path: Path):
    """yaml 写 10000.5，读出为精确的 Decimal（金额不走 float）。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paper:\n  initial_equity: 10000.5\n", encoding="utf-8")
    settings = load_settings(cfg)
    assert isinstance(settings.paper.initial_equity, Decimal)
    assert settings.paper.initial_equity == Decimal("10000.5")


def test_write_settings_decimal_yaml_roundtrip(tmp_path: Path):
    """Decimal 字段写回 yaml 不报错，且重读后精度不变。"""
    cfg = tmp_path / "config.yaml"
    write_settings({"paper": {"initial_equity": 10000.5}}, cfg)
    reloaded = load_settings(cfg)
    assert reloaded.paper.initial_equity == Decimal("10000.5")
