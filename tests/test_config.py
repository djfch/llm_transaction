"""配置加载与写回校验的测试。"""

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.config import (
    AgentBinding,
    AgentsConfig,
    CredentialConfig,
    LLMConfig,
    ResearchConfig,
    RiskConfig,
    ReviewConfig,
    SchedulerConfig,
    Settings,
    load_settings,
    load_watchlist,
)
from src.config_io import ConfigError, write_settings, write_watchlist


def test_load_default_settings():
    """校验项目默认配置文件加载后关键默认值符合预期。

    参数：无

    返回：
        None，断言默认配置为 paper 模式、最大杠杆 5 倍、usdt 结算
    """
    settings = load_settings()
    assert settings.mode == "paper"
    assert settings.risk.max_leverage == 5
    assert settings.risk.max_position_stop_risk_pct == 0.01
    assert settings.gate.settle == "usdt"


def test_old_risk_config_without_stop_risk_uses_one_percent_default():
    """校验旧配置缺少整仓止损字段时自动采用 1% 默认值。

    参数：无

    返回：
        None，断言无需迁移即可完成模型校验
    """
    risk = RiskConfig.model_validate(
        {
            "max_position_pct": 0.3,
            "max_total_position_pct": 0.8,
            "max_leverage": 5,
            "daily_loss_limit": 0.1,
            "max_orders_per_day": 20,
            "max_deviation": 0.02,
            "kill_switch": False,
        }
    )
    assert risk.max_position_stop_risk_pct == 0.01


def test_load_watchlist():
    """校验默认白名单文件加载后包含 BTC_USDT 合约。

    参数：无

    返回：
        None，断言 load_watchlist 读出的合约列表包含 BTC_USDT
    """
    watchlist = load_watchlist()
    assert "BTC_USDT" in watchlist.contracts


def test_load_watchlist_empty_rejected(tmp_path: Path):
    """校验合约列表为空的白名单文件加载时被拒绝。

    参数：
        tmp_path: Path，pytest 临时目录夹具，写入 contracts 为空的 watchlist.yaml

    返回：
        None，断言 load_watchlist 抛出 ValidationError（至少包含一个合约）
    """
    path = tmp_path / "watchlist.yaml"
    path.write_text("settle: usdt\ncontracts: []\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="至少包含一个合约"):
        load_watchlist(path)


def test_load_watchlist_duplicate_contracts_rejected(tmp_path: Path):
    """校验合约重复出现的白名单文件加载时被拒绝。

    参数：
        tmp_path: Path，pytest 临时目录夹具，写入含两条 BTC_USDT 的 watchlist.yaml

    返回：
        None，断言 load_watchlist 抛出 ValidationError（合约不能重复）
    """
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        "settle: usdt\ncontracts:\n  - BTC_USDT\n  - BTC_USDT\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="不能重复"):
        load_watchlist(path)


def test_invalid_mode_rejected(tmp_path: Path):
    """校验 mode 取值非法（如 mars）时配置加载被拒绝。

    参数：
        tmp_path: Path，pytest 临时目录夹具，写入 mode 为 mars 的 config.yaml

    返回：
        None，断言 load_settings 抛出 ValueError（非法 mode）
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mode: mars", encoding="utf-8")
    with pytest.raises(ValueError, match="非法 mode"):
        load_settings(cfg)


def test_write_settings_roundtrip(tmp_path: Path):
    """校验配置写回 yaml 后重读保持一致的往返行为。

    参数：
        tmp_path: Path，pytest 临时目录夹具，写回的 config.yaml 落在其中

    返回：
        None，断言 write_settings 返回值 mode 为 testnet，重读后 max_leverage 为 3
    """
    cfg = tmp_path / "config.yaml"
    saved = write_settings({"mode": "testnet", "risk": {"max_leverage": 3}}, cfg)
    assert saved.mode == "testnet"
    reloaded = load_settings(cfg)
    assert reloaded.risk.max_leverage == 3


def test_write_settings_invalid_risk(tmp_path: Path):
    """校验写回非法风控参数（max_leverage 为 0）被拒绝。

    参数：
        tmp_path: Path，pytest 临时目录夹具，目标配置文件路径落在其中

    返回：
        None，断言 write_settings 抛出 ConfigError
    """
    with pytest.raises(ConfigError):
        write_settings({"risk": {"max_leverage": 0}}, tmp_path / "c.yaml")


def test_write_watchlist_empty_rejected(tmp_path: Path):
    """校验写回空合约列表的白名单被拒绝。

    参数：
        tmp_path: Path，pytest 临时目录夹具，目标白名单文件路径落在其中

    返回：
        None，断言 write_watchlist 抛出 ConfigError（不能为空）
    """
    with pytest.raises(ConfigError, match="不能为空"):
        write_watchlist({"contracts": []}, tmp_path / "w.yaml")


def test_write_watchlist_duplicate_contracts_rejected(tmp_path: Path):
    """校验写回合约重复的白名单被拒绝。

    参数：
        tmp_path: Path，pytest 临时目录夹具，目标白名单文件路径落在其中

    返回：
        None，断言 write_watchlist 抛出 ConfigError（不能重复）
    """
    with pytest.raises(ConfigError, match="不能重复"):
        write_watchlist(
            {"contracts": ["BTC_USDT", "BTC_USDT"]},
            tmp_path / "w.yaml",
        )


# ---------- SchedulerConfig 取值关系校验 ----------


def test_scheduler_min_gt_max_rejected():
    """min_wake_minutes 不得大于 max_wake_minutes。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    with pytest.raises(ValidationError, match="min_wake_minutes"):
        SchedulerConfig(min_wake_minutes=60, max_wake_minutes=30)


def test_scheduler_default_outside_range_rejected():
    """default_wake_minutes 必须落在 [min, max] 之间。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    with pytest.raises(ValidationError, match="default_wake_minutes"):
        SchedulerConfig(default_wake_minutes=1000)  # 超过默认上限 720
    with pytest.raises(ValidationError, match="default_wake_minutes"):
        SchedulerConfig(default_wake_minutes=1, min_wake_minutes=5)  # 低于下限


def test_scheduler_max_over_720_rejected():
    """max_wake_minutes 上限为 720（12 小时）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    with pytest.raises(ValidationError):
        SchedulerConfig(max_wake_minutes=721)


def test_scheduler_valid_combo_accepted():
    """校验合法的唤醒间隔组合（default 落在 [min, max] 内）被接受。

    参数：无

    返回：
        None，断言 SchedulerConfig 构造成功且 max_wake_minutes 为 120
    """
    cfg = SchedulerConfig(default_wake_minutes=30, min_wake_minutes=10, max_wake_minutes=120)
    assert cfg.max_wake_minutes == 120


# ---------- ReviewConfig.daily_time 校验 ----------


def test_review_config_defaults():
    """复盘配置默认值：启用、触发时刻 03:00（本地）、间隔 1 天。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    cfg = ReviewConfig()
    assert cfg.enabled is True
    assert cfg.daily_time == "03:00"
    assert cfg.interval_days == 1


def test_review_interval_days_valid():
    """校验合法 interval_days（含上限边界 30）被接受。

    参数：无

    返回：
        None，断言 interval_days 为 3 与 30 时 ReviewConfig 构造成功且取值不变
    """
    assert ReviewConfig(interval_days=3).interval_days == 3
    assert ReviewConfig(interval_days=30).interval_days == 30


def test_review_interval_days_invalid():
    """非法 interval_days：0、负数、超上限 30 均拒绝。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    for bad in [0, -1, 31]:
        with pytest.raises(ValidationError, match="interval_days"):
            ReviewConfig(interval_days=bad)


def test_review_daily_time_valid():
    """校验合法 daily_time（含边界 0:00 与 23:59）被接受。

    参数：无

    返回：
        None，断言 daily_time 为 23:59 与 0:00 时 ReviewConfig 构造成功且取值不变
    """
    assert ReviewConfig(daily_time="23:59").daily_time == "23:59"
    assert ReviewConfig(daily_time="0:00").daily_time == "0:00"


def test_review_daily_time_invalid():
    """非法 daily_time：越界时刻、非 HH:MM 结构均拒绝。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    for bad in ["24:00", "12:60", "-1:30", "3点", "03:00:00", "", "ab:cd"]:
        with pytest.raises(ValidationError, match="daily_time"):
            ReviewConfig(daily_time=bad)


# ---------- ResearchConfig 定时调度与方向闸门字段 ----------


def test_research_config_defaults():
    """研报配置默认值：总开关关闭，三市场预设开启，方向闸门保持开启。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    cfg = ResearchConfig()
    assert cfg.enabled is False
    assert [(item.id, item.kind, item.enabled) for item in cfg.schedules] == [
        ("asia_open", "market_open", True),
        ("europe_open", "market_open", True),
        ("us_open", "market_open", True),
    ]
    assert cfg.gate_enabled is True
    assert cfg.gate_max_age_hours == 13


def test_research_legacy_schedule_migrates_without_changing_switches():
    """旧三时间字段加载为新预设，同时保留总开关与方向闸门配置。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    cfg = ResearchConfig(
        enabled=True,
        time_asia="08:30",
        time_europe="14:30",
        time_us="21:00",
        us_dst_adjust=False,
        gate_enabled=False,
    )
    assert cfg.enabled is True
    assert [item.id for item in cfg.schedules] == ["asia_open", "europe_open", "us_open"]
    assert cfg.gate_enabled is False


def test_research_legacy_schedule_writeback_is_idempotent(tmp_path: Path):
    """旧字段首次写回后只保留新列表，重复读写不再改变调度结构。

    参数：
        tmp_path: Path，隔离的配置文件目录

    返回：
        None：校验迁移落盘结构与二次写回一致
    """
    path = tmp_path / "config.yaml"
    legacy = {
        "research": {
            "enabled": True,
            "time_asia": "08:30",
            "time_europe": "14:30",
            "time_us": "21:00",
            "us_dst_adjust": False,
            "gate_enabled": False,
        }
    }
    first = write_settings(legacy, path)
    first_raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    second = write_settings(first_raw, path)
    second_raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert "time_asia" not in first_raw["research"]
    assert first_raw["research"]["enabled"] is True
    assert first_raw["research"]["gate_enabled"] is False
    assert first_raw["research"]["schedules"] == second_raw["research"]["schedules"]
    assert first.research == second.research


def test_research_custom_schedule_valid():
    """合法自定义时间接受 UTC+8 时刻与四种日期规则。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    base = [item.model_dump() for item in ResearchConfig().schedules]
    for calendar in ("daily", "XTKS", "XLON", "XNYS"):
        cfg = ResearchConfig(
            schedules=[
                *base,
                {
                    "id": f"00000000-0000-4000-8000-00000000000{len(calendar)}",
                    "kind": "fixed_time",
                    "enabled": True,
                    "time": "12:30",
                    "calendar": calendar,
                },
            ]
        )
        assert cfg.schedules[-1].calendar == calendar


def test_research_custom_schedule_rejects_invalid_or_conflicting_times():
    """非法时刻、重复启用时刻及与市场预设可能时刻冲突时拒绝保存。

    参数：无

    返回：
        None：通过断言校验自定义调度冲突门禁
    """
    base = [item.model_dump() for item in ResearchConfig().schedules]

    def custom(suffix: str, time_value: str) -> dict:
        """构造自定义调度测试字典。

        参数：
            suffix: str，UUID 最后一位
            time_value: str，UTC+8 触发时刻

        返回：
            dict：可传给 ResearchConfig 的自定义调度项
        """
        return {
            "id": f"00000000-0000-4000-8000-00000000000{suffix}",
            "kind": "fixed_time",
            "enabled": True,
            "time": time_value,
            "calendar": "daily",
        }

    for bad in ("25:00", "8:60", "0830", "ab:cd"):
        with pytest.raises(ValidationError):
            ResearchConfig(schedules=[*base, custom("1", bad)])
    with pytest.raises(ValidationError, match="重复"):
        ResearchConfig(schedules=[*base, custom("1", "12:30"), custom("2", "12:30")])
    with pytest.raises(ValidationError, match="冲突"):
        ResearchConfig(schedules=[*base, custom("1", "21:00")])
    duplicate_lower = custom("a", "12:30")
    duplicate_upper = {**custom("b", "13:30"), "id": duplicate_lower["id"].upper()}
    with pytest.raises(ValidationError, match="id 不能重复"):
        ResearchConfig(schedules=[*base, duplicate_lower, duplicate_upper])


def test_research_gate_max_age_hours_bounds():
    """方向闸门有效期：1-48 小时接受，0 与 49 拒绝。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert ResearchConfig(gate_max_age_hours=1).gate_max_age_hours == 1
    assert ResearchConfig(gate_max_age_hours=48).gate_max_age_hours == 48
    for bad in [0, 49]:
        with pytest.raises(ValidationError, match="gate_max_age_hours"):
            ResearchConfig(gate_max_age_hours=bad)


# ---------- PaperConfig.initial_equity 为 Decimal ----------


def test_paper_initial_equity_decimal_from_yaml(tmp_path: Path):
    """yaml 写 10000.5，读出为精确的 Decimal（金额不走 float）。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paper:\n  initial_equity: 10000.5\n", encoding="utf-8")
    settings = load_settings(cfg)
    assert isinstance(settings.paper.initial_equity, Decimal)
    assert settings.paper.initial_equity == Decimal("10000.5")


def test_write_settings_decimal_yaml_roundtrip(tmp_path: Path):
    """Decimal 字段写回 yaml 不报错，且重读后精度不变。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    cfg = tmp_path / "config.yaml"
    write_settings({"paper": {"initial_equity": 10000.5}}, cfg)
    reloaded = load_settings(cfg)
    assert reloaded.paper.initial_equity == Decimal("10000.5")


# ---------- 多 LLM 凭证 + 按 agent 分配 ----------


def _cred(name: str, **kwargs) -> CredentialConfig:
    """构造一条 anthropic 测试凭证，额外字段透传给 CredentialConfig。

    参数：
        name: str，凭证名
        **kwargs: 透传给 CredentialConfig 的额外字段（如 api_key_env）

    返回：
        CredentialConfig：provider 为 anthropic、模型为 claude-sonnet-4-5 的凭证
    """
    return CredentialConfig(name=name, provider="anthropic", model="claude-sonnet-4-5", **kwargs)


def test_legacy_config_synthesizes_default_credential():
    """旧平铺配置（无 credentials）自动合成 default 凭证，行为字段与旧平铺一致（零迁移）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
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
    """api_key_env 留空按 LLM_KEY_<name 大写，非字母数字换下划线> 推导（推导值天然合规）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert _cred("main").api_key_env == "LLM_KEY_MAIN"
    assert _cred("deepseek-backup 2").api_key_env == "LLM_KEY_DEEPSEEK_BACKUP_2"


def test_credential_explicit_api_key_env_whitelist():
    """显式 api_key_env 对齐 .env 白名单：两个旧键名与 LLM_KEY_* 接受。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert _cred("x", api_key_env="ANTHROPIC_API_KEY").api_key_env == "ANTHROPIC_API_KEY"
    assert _cred("x", api_key_env="OPENAI_API_KEY").api_key_env == "OPENAI_API_KEY"
    assert _cred("x", api_key_env="LLM_KEY_CUSTOM").api_key_env == "LLM_KEY_CUSTOM"


def test_credential_explicit_api_key_env_whitelist_rejected():
    """显式 api_key_env 白名单外一律拒绝（PUT 映 422）：

    - MY_KEY / MY_CUSTOM_KEY：能落盘但 set_env_keys 写不进 .env → 死凭证；
    - GATE_API_KEY：会把交易所 key 读出来当 LLM bearer token 用（安全红线）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    for bad in ("MY_KEY", "MY_CUSTOM_KEY", "GATE_API_KEY", "GATE_API_SECRET", "TELEGRAM_BOT_TOKEN"):
        with pytest.raises(ValidationError, match="api_key_env"):
            _cred("x", api_key_env=bad)


def test_credential_invalid_api_key_env_rejected():
    """api_key_env 非法字符拒绝（必须 ^[A-Z][A-Z0-9_]*$）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    for bad in ("1ABC", "lower", "HAS-DASH", "HAS SPACE", "_LEADING", "LLM_KEY_lower"):
        with pytest.raises(ValidationError, match="api_key_env"):
            _cred("x", api_key_env=bad)


def test_credential_invalid_provider_rejected():
    """provider 仅允许 anthropic / openai_compat / openai_responses，拼错（如 openaai）直接校验拒绝，
    避免被构造层静默当作 openai_compat。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    with pytest.raises(ValidationError):
        CredentialConfig(name="x", provider="openaai", model="m1")
    assert CredentialConfig(name="x", provider="openai_compat", model="m1").provider == (
        "openai_compat"
    )
    assert CredentialConfig(name="x", provider="openai_responses", model="m1").provider == (
        "openai_responses"
    )


def test_duplicate_credential_names_rejected():
    """credentials 重名拒绝（Settings 校验经 resolve_credentials 拦截）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    with pytest.raises(ValidationError, match="重名"):
        Settings(llm=LLMConfig(credentials=[_cred("a"), _cred("a")]))


def test_agent_binding_unknown_credential_rejected():
    """agent 引用不存在的凭证 → Settings 校验 ValueError（write_settings 映 422）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    with pytest.raises(ValidationError, match="不存在的凭证"):
        Settings(
            llm=LLMConfig(credentials=[_cred("a")]),
            agents=AgentsConfig(trader=AgentBinding(credential="ghost")),
        )


def test_researcher_binding_unknown_credential_is_allowed():
    """研报 Agent 引用缺失凭证时允许加载并由运行时降级。

    参数：无

    返回：
        None，通过断言冻结可选研报功能的兼容规则
    """
    settings = Settings(
        agents=AgentsConfig(researcher=AgentBinding(credential="ghost")),
    )

    assert settings.agents.researcher.credential == "ghost"


def test_agent_binding_defaults_point_to_default_credential():
    """缺省 agents 时三个 Agent 都指向 default。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    settings = Settings()
    assert settings.agents.trader.credential == "default"
    assert settings.agents.reviewer.credential == "default"
    assert settings.agents.researcher.credential == "default"


def test_multi_credentials_roundtrip_via_write_settings(tmp_path: Path):
    """多凭证 + 分配写回 config.yaml 后重读保持一致（含 422 前置校验路径）。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    cfg = tmp_path / "config.yaml"
    data = {
        "llm": {
            "credentials": [
                {"name": "main", "provider": "anthropic", "model": "m1"},
                {"name": "backup", "provider": "openai_compat", "model": "m2"},
            ]
        },
        "agents": {
            "trader": {"credential": "main"},
            "reviewer": {"credential": "backup"},
            "researcher": {"credential": "backup"},
        },
    }
    saved = write_settings(data, cfg)
    creds = saved.llm.resolve_credentials()
    assert [c.name for c in creds] == ["main", "backup"]
    assert creds[0].api_key_env == "LLM_KEY_MAIN" and creds[1].api_key_env == "LLM_KEY_BACKUP"
    reloaded = load_settings(cfg)
    assert reloaded.agents.trader.credential == "main"
    assert reloaded.agents.reviewer.credential == "backup"
    assert reloaded.agents.researcher.credential == "backup"
    with pytest.raises(ConfigError):  # 引用不存在凭证 → 落不了盘
        write_settings({**data, "agents": {"reviewer": {"credential": "ghost"}}}, cfg)
