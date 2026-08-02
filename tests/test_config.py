"""配置加载与写回校验的测试。"""

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import (
    AgentBinding,
    AgentsConfig,
    CredentialConfig,
    LLMConfig,
    ReviewConfig,
    SchedulerConfig,
    Settings,
    load_settings,
    load_watchlist,
)
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
    """复盘配置默认值：启用、触发时刻 03:00（本地）、间隔 1 天。"""
    cfg = ReviewConfig()
    assert cfg.enabled is True
    assert cfg.daily_time == "03:00"
    assert cfg.interval_days == 1


def test_review_interval_days_valid():
    assert ReviewConfig(interval_days=3).interval_days == 3
    assert ReviewConfig(interval_days=30).interval_days == 30


def test_review_interval_days_invalid():
    """非法 interval_days：0、负数、超上限 30 均拒绝。"""
    for bad in [0, -1, 31]:
        with pytest.raises(ValidationError, match="interval_days"):
            ReviewConfig(interval_days=bad)


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


# ---------- 多 LLM 凭证 + 按 agent 分配 ----------


def _cred(name: str, **kwargs) -> CredentialConfig:
    return CredentialConfig(name=name, provider="anthropic", model="claude-sonnet-4-5", **kwargs)


def test_legacy_config_synthesizes_default_credential():
    """旧平铺配置（无 credentials）自动合成 default 凭证，行为字段与旧平铺一致（零迁移）。"""
    llm = LLMConfig(
        provider="openai_compat", model="m1", max_tokens=1024, openai_base_url="https://x"
    )
    creds = llm.resolve_credentials()
    assert len(creds) == 1
    c = creds[0]
    assert (c.name, c.provider, c.model, c.max_tokens, c.openai_base_url) == (
        "default",
        "openai_compat",
        "m1",
        1024,
        "https://x",
    )
    assert c.api_key_env == "OPENAI_API_KEY"  # openai_compat 推导
    assert LLMConfig(provider="anthropic").resolve_credentials()[0].api_key_env == (
        "ANTHROPIC_API_KEY"  # anthropic 推导
    )


def test_credential_api_key_env_derivation():
    """api_key_env 留空按 LLM_KEY_<name 大写，非字母数字换下划线> 推导（推导值天然合规）。"""
    assert _cred("main").api_key_env == "LLM_KEY_MAIN"
    assert _cred("deepseek-backup 2").api_key_env == "LLM_KEY_DEEPSEEK_BACKUP_2"


def test_credential_explicit_api_key_env_whitelist():
    """显式 api_key_env 对齐 .env 白名单：两个旧键名与 LLM_KEY_* 接受。"""
    assert _cred("x", api_key_env="ANTHROPIC_API_KEY").api_key_env == "ANTHROPIC_API_KEY"
    assert _cred("x", api_key_env="OPENAI_API_KEY").api_key_env == "OPENAI_API_KEY"
    assert _cred("x", api_key_env="LLM_KEY_CUSTOM").api_key_env == "LLM_KEY_CUSTOM"


def test_credential_explicit_api_key_env_whitelist_rejected():
    """显式 api_key_env 白名单外一律拒绝（PUT 映 422）：

    - MY_KEY / MY_CUSTOM_KEY：能落盘但 set_env_keys 写不进 .env → 死凭证；
    - GATE_API_KEY：会把交易所 key 读出来当 LLM bearer token 用（安全红线）。
    """
    for bad in ("MY_KEY", "MY_CUSTOM_KEY", "GATE_API_KEY", "GATE_API_SECRET", "TELEGRAM_BOT_TOKEN"):
        with pytest.raises(ValidationError, match="api_key_env"):
            _cred("x", api_key_env=bad)


def test_credential_invalid_api_key_env_rejected():
    """api_key_env 非法字符拒绝（必须 ^[A-Z][A-Z0-9_]*$）。"""
    for bad in ("1ABC", "lower", "HAS-DASH", "HAS SPACE", "_LEADING", "LLM_KEY_lower"):
        with pytest.raises(ValidationError, match="api_key_env"):
            _cred("x", api_key_env=bad)


def test_credential_invalid_provider_rejected():
    """provider 仅允许 anthropic / openai_compat / openai_responses，拼错（如 openaai）直接校验拒绝，
    避免被构造层静默当作 openai_compat。"""
    with pytest.raises(ValidationError):
        CredentialConfig(name="x", provider="openaai", model="m1")
    assert CredentialConfig(name="x", provider="openai_compat", model="m1").provider == (
        "openai_compat"
    )
    assert CredentialConfig(name="x", provider="openai_responses", model="m1").provider == (
        "openai_responses"
    )


def test_duplicate_credential_names_rejected():
    """credentials 重名拒绝（Settings 校验经 resolve_credentials 拦截）。"""
    with pytest.raises(ValidationError, match="重名"):
        Settings(llm=LLMConfig(credentials=[_cred("a"), _cred("a")]))


def test_agent_binding_unknown_credential_rejected():
    """agent 引用不存在的凭证 → Settings 校验 ValueError（write_settings 映 422）。"""
    with pytest.raises(ValidationError, match="不存在的凭证"):
        Settings(
            llm=LLMConfig(credentials=[_cred("a")]),
            agents=AgentsConfig(trader=AgentBinding(credential="ghost")),
        )


def test_agent_binding_defaults_point_to_default_credential():
    """缺省 agents 时 trader/reviewer 都指向 default。"""
    settings = Settings()
    assert settings.agents.trader.credential == "default"
    assert settings.agents.reviewer.credential == "default"


def test_multi_credentials_roundtrip_via_write_settings(tmp_path: Path):
    """多凭证 + 分配写回 config.yaml 后重读保持一致（含 422 前置校验路径）。"""
    cfg = tmp_path / "config.yaml"
    data = {
        "llm": {
            "credentials": [
                {"name": "main", "provider": "anthropic", "model": "m1"},
                {"name": "backup", "provider": "openai_compat", "model": "m2"},
            ]
        },
        "agents": {"trader": {"credential": "main"}, "reviewer": {"credential": "backup"}},
    }
    saved = write_settings(data, cfg)
    creds = saved.llm.resolve_credentials()
    assert [c.name for c in creds] == ["main", "backup"]
    assert creds[0].api_key_env == "LLM_KEY_MAIN" and creds[1].api_key_env == "LLM_KEY_BACKUP"
    reloaded = load_settings(cfg)
    assert reloaded.agents.trader.credential == "main"
    assert reloaded.agents.reviewer.credential == "backup"
    with pytest.raises(ConfigError):  # 引用不存在凭证 → 落不了盘
        write_settings({**data, "agents": {"reviewer": {"credential": "ghost"}}}, cfg)
