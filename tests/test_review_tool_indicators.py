"""src/review 指标工具测试：3 个工具经 ReviewToolRegistry.execute 验证。

覆盖：面板渲染（默认周期/无数据 降级/watchlist 校验）、当前配置文本、submit 成功
（版本落库/created_by='review_agent'/deps 记录版本 id）、submit 校验失败中文文案
（未知键/>8 个/空 reason/无差异/非数组）、依赖 None 时「指标功能未配置」降级、
ReviewAgent 构造期注入的指标依赖进入每轮 deps、build_review 透传 notify_event 事件链路。
红线自证：本文件不 import src/agent/*（与 src/review 包约束一致），provider 用
SimpleNamespace 鸭子类型 stub。
"""

from collections import deque
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.audit.trail import AuditTrail
from src.config import DEFAULT_INDICATOR_SHORTLIST, AuditConfig, Settings
from src.gateway.base import Candle
from src.market.indicator_service import REGISTRY, IndicatorService
from src.memory import Database, Repo
from src.review.agent import ReviewAgent
from src.review.indicator_config import IndicatorConfigStore
from src.review.prompts import ReviewPromptLoader
from src.review.setup import build_review
from src.review.strategy import StrategyStore
from src.review.tool_handlers import ReviewToolDeps
from src.review.tools import ReviewToolRegistry

_WATCHLIST = ("BTC_USDT", "ETH_USDT")


class _CandleStub:
    """K 线缓存 stub：按合约返回预置序列（鸭子类型对齐 CandleCache.get_recent）。"""

    def __init__(self, candles: list[Candle]) -> None:
        self._candles = candles

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        return self._candles[-n:]


class _OiStub:
    """OI 缓存 stub（鸭子类型对齐 OpenInterestCache.get）。"""

    def __init__(self, value: Decimal | None) -> None:
        self._value = value

    def get(self, contract: str) -> Decimal | None:
        return self._value


def _candles(n: int) -> list[Candle]:
    """n 根 1h 阳线（收盘 100+i 递增），保证各指标有确定非 None 值。"""
    return [
        Candle(
            t=1_700_000_000 + i * 3600,
            o=Decimal(99 + i),
            h=Decimal(101 + i),
            l=Decimal(98 + i),
            c=Decimal(100 + i),
            v=Decimal(10),
        )
        for i in range(n)
    ]


@pytest.fixture
async def repo(tmp_path):
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
async def deps(tmp_path, repo):
    """完整接线 deps：指标服务（60 根 K 线 + OI=12345）+ 短名单 store + watchlist。"""
    store = StrategyStore(tmp_path / "system_prompt.md", repo)
    indicator_store = IndicatorConfigStore(
        tmp_path / "indicator_config.yaml", repo, valid_keys=frozenset(REGISTRY)
    )
    service = IndicatorService(_CandleStub(_candles(60)), _OiStub(Decimal("12345")))
    return ReviewToolDeps(
        repo=repo,
        store=store,
        mode="paper",
        indicator_service=service,
        indicator_config_store=indicator_store,
        watchlist=_WATCHLIST,
    )


@pytest.fixture
async def bare_deps(tmp_path, repo):
    """未接线指标依赖的 deps（indicator_service / indicator_config_store 均为 None）。"""
    store = StrategyStore(tmp_path / "system_prompt.md", repo)
    return ReviewToolDeps(repo=repo, store=store, mode="paper")


@pytest.fixture
async def registry(deps):
    return ReviewToolRegistry(deps)


# ---------- get_indicators ----------


async def test_get_indicators_panel(registry):
    text = await registry.execute("get_indicators", {"contract": "BTC_USDT"})
    assert "BTC_USDT 指标面板（1h）" in text  # 缺省周期与执行 agent 惯例一致
    assert "截至" in text  # 有 K 线时给面板时刻
    assert "- ema9 | EMA9(指数均线)：" in text
    assert "- macd | MACD(异同均线)：dif=" in text  # 多字段指标逐字段列出
    assert "- oi | 持仓量：12345" in text  # OI 来自 OI 缓存
    assert "无数据" not in text  # 60 根 K 线覆盖全部 min_candles


async def test_get_indicators_no_data_fallback(deps):
    """K 线不足 min_candles 的指标与缺失 OI 均显示 无数据。"""
    deps.indicator_service = IndicatorService(_CandleStub(_candles(10)), _OiStub(None))
    text = await ReviewToolRegistry(deps).execute("get_indicators", {"contract": "BTC_USDT"})
    assert "- ema50 | EMA50(指数均线)：无数据" in text  # ema50 需 50 根
    assert "- oi | 持仓量：无数据" in text
    assert "- ema9 | EMA9(指数均线)：无数据" not in text  # ema9 只需 9 根，有值


async def test_get_indicators_interval_param(registry):
    text = await registry.execute("get_indicators", {"contract": "ETH_USDT", "interval": "4h"})
    assert "ETH_USDT 指标面板（4h）" in text
    bad = await registry.execute("get_indicators", {"contract": "BTC_USDT", "interval": "3h"})
    assert "参数错误" in bad and "interval" in bad


async def test_get_indicators_watchlist_guard(registry):
    text = await registry.execute("get_indicators", {"contract": "DOGE_USDT"})
    assert "不在 watchlist" in text and "BTC_USDT" in text  # 给出可选集合
    missing = await registry.execute("get_indicators", {})
    assert "参数错误" in missing and "contract" in missing


# ---------- get_indicator_config ----------


async def test_get_indicator_config(registry):
    text = await registry.execute("get_indicator_config", {})
    assert "当前指标短名单" in text
    for key in DEFAULT_INDICATOR_SHORTLIST:
        assert key in text  # 文件未创建时回落默认基线
    for key in REGISTRY:
        assert key in text  # 可选全集菜单覆盖全部注册键
    assert "主图叠加" in text and "副图" in text and "单值" in text  # kind 中文释义


# ---------- submit_indicator_config ----------


async def test_submit_indicator_config_success(registry, deps, repo):
    new_list = ["ema20", "rsi14", "macd", "boll"]
    reason = "round-aaa 的 BTC_USDT 亏损源于 ema50 全程无信号，换 boll 捕捉波动"
    text = await registry.execute(
        "submit_indicator_config", {"shortlist": new_list, "reason": reason}
    )
    assert "校验通过" in text
    assert deps.indicator_config_version_id is not None  # 成果记进 deps（同策略修订模式）
    assert f"v{deps.indicator_config_version_id}" in text
    assert "boll" in text  # 生效短名单回显
    assert deps.indicator_config_store.load_current().shortlist == new_list  # 文件已生效
    versions = await repo.indicator_config.list_versions()
    assert len(versions) == 1
    assert versions[0].id == deps.indicator_config_version_id
    assert versions[0].created_by == "review_agent"
    assert versions[0].reason == reason
    assert versions[0].report_id is None  # 报告关联由轮末装配回填（同策略修订取法）


async def test_submit_indicator_config_dedup(registry, deps):
    """重复键去重保序后生效（提交 3 个含重复键，生效 2 个）。"""
    text = await registry.execute(
        "submit_indicator_config",
        {"shortlist": ["rsi14", "ema20", "rsi14"], "reason": "去重验证"},
    )
    assert "校验通过" in text and "2 个" in text
    assert deps.indicator_config_store.load_current().shortlist == ["rsi14", "ema20"]


async def test_submit_indicator_config_unknown_key(registry, deps):
    text = await registry.execute(
        "submit_indicator_config", {"shortlist": ["ema20", "sma20"], "reason": "尝试未知键"}
    )
    assert "校验拒绝" in text and "未知指标键" in text and "sma20" in text
    assert deps.indicator_config_version_id is None  # 拒绝不置位
    assert not await deps.repo.indicator_config.list_versions()  # 拒绝不落版本


async def test_submit_indicator_config_too_many(registry, deps):
    nine = ["ema9", "ema20", "ema50", "macd", "rsi7", "rsi14", "kdj", "roc10", "atr14"]
    text = await registry.execute(
        "submit_indicator_config", {"shortlist": nine, "reason": "堆叠 9 个指标"}
    )
    assert "校验拒绝" in text and "1~8" in text
    assert deps.indicator_config_version_id is None


async def test_submit_indicator_config_no_diff(registry, deps):
    text = await registry.execute(
        "submit_indicator_config",
        {"shortlist": list(DEFAULT_INDICATOR_SHORTLIST), "reason": "与当前相同"},
    )
    assert "校验拒绝" in text and "无差异" in text
    assert deps.indicator_config_version_id is None


async def test_submit_indicator_config_bad_args(registry, deps):
    empty_reason = await registry.execute(
        "submit_indicator_config", {"shortlist": ["ema20"], "reason": "  "}
    )
    assert "参数错误" in empty_reason and "reason" in empty_reason
    not_list = await registry.execute(
        "submit_indicator_config", {"shortlist": "ema20", "reason": "r"}
    )
    assert "参数错误" in not_list and "shortlist" in not_list
    assert deps.indicator_config_version_id is None


# ---------- 降级与注册 ----------


async def test_indicator_tools_not_configured(bare_deps):
    registry = ReviewToolRegistry(bare_deps)
    text = await registry.execute("get_indicators", {"contract": "BTC_USDT"})
    assert "指标功能未配置" in text
    assert "指标功能未配置" in await registry.execute("get_indicator_config", {})
    submit = await registry.execute(
        "submit_indicator_config", {"shortlist": ["ema20"], "reason": "r"}
    )
    assert "指标功能未配置" in submit


async def test_indicator_tools_registered(registry):
    specs = {s.name: s for s in registry.specs}
    assert {"get_indicators", "get_indicator_config", "submit_indicator_config"} <= specs.keys()
    submit = specs["submit_indicator_config"]
    assert "ema20" in submit.description  # 可选键菜单动态生成进 schema 描述
    assert submit.parameters["required"] == ["shortlist", "reason"]
    interval = specs["get_indicators"].parameters["properties"]["interval"]
    assert "1h" in interval["enum"] and "3h" not in interval["enum"]


# ---------- ReviewAgent 装配接线 ----------


class _StubProvider:
    """按脚本回放响应的 stub（鸭子类型对齐复盘 provider 协议，不 import src/agent）。"""

    def __init__(self, script: list) -> None:
        self._script = deque(script)

    async def chat(self, system: str, messages: list[dict], tools: list[dict]):
        return self._script.popleft()

    def tool_result_message(self, call, result: str) -> dict:
        return {"role": "tool", "content": result}


def _resp(text: str = "", calls: tuple = ()) -> SimpleNamespace:
    return SimpleNamespace(text=text, raw="{}", assistant_message=None, tool_calls=list(calls))


async def test_review_agent_wires_indicator_deps(tmp_path, repo, deps):
    """构造期注入的指标依赖进入每轮 deps：LLM 调 get_indicator_config 返回配置文本。"""
    review_prompt = tmp_path / "review_prompt.md"
    review_prompt.write_text("# 复盘", encoding="utf-8")
    provider = _StubProvider(
        [
            _resp(calls=(SimpleNamespace(name="get_indicator_config", args={}, call_id="c1"),)),
            _resp(text="复盘报告"),
        ]
    )
    agent = ReviewAgent(
        settings=Settings(),
        provider=provider,
        repo=repo,
        audit=AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit"))),
        store=deps.store,
        prompt_loader=ReviewPromptLoader(review_prompt),
        indicator_service=deps.indicator_service,
        indicator_config_store=deps.indicator_config_store,
        watchlist=deps.watchlist,
    )
    result = await agent.run(1000.0, 2000.0)
    assert result["ok"] is True
    calls = await repo.list_audit_tool_calls(result["round_id"])
    assert [c.tool for c in calls] == ["get_indicator_config"]
    assert "当前指标短名单" in calls[0].result_json


async def test_build_review_wires_indicator_params(tmp_path, repo, deps):
    """build_review 逐项接通指标依赖：装配出的 agent 轮内 deps 拿到实例（工具非降级）。"""
    review_prompt = tmp_path / "review_prompt.md"
    review_prompt.write_text("# 复盘", encoding="utf-8")
    provider = _StubProvider(
        [
            _resp(calls=(SimpleNamespace(name="get_indicator_config", args={}, call_id="c1"),)),
            _resp(text="复盘报告"),
        ]
    )
    components = await build_review(
        Settings(),
        repo,
        AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit"))),
        provider,
        strategy_path=tmp_path / "strategy.md",
        review_prompt_path=review_prompt,
        indicator_service=deps.indicator_service,
        indicator_config_store=deps.indicator_config_store,
        watchlist=deps.watchlist,
    )
    result = await components.agent.run(1000.0, 2000.0)
    assert result["ok"] is True
    calls = await repo.list_audit_tool_calls(result["round_id"])
    assert "当前指标短名单" in calls[0].result_json  # deps 拿到 store 实例，非「指标功能未配置」


async def test_build_review_passes_notify_event_to_agent(tmp_path, repo):
    """build_review 透传 notify_event：装配出的 agent 轮始/轮末事件经装配层广播（透传不断）。"""
    review_prompt = tmp_path / "review_prompt.md"
    review_prompt.write_text("# 复盘", encoding="utf-8")
    events: list[dict] = []
    components = await build_review(
        Settings(),
        repo,
        AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit"))),
        _StubProvider([_resp(text="复盘报告")]),
        strategy_path=tmp_path / "strategy.md",
        review_prompt_path=review_prompt,
        notify_event=events.append,  # 装配层透传入口：断则收集为空
    )
    result = await components.agent.run(1000.0, 2000.0)
    assert result["ok"] is True
    assert [e["type"] for e in events] == ["review_round_start", "review_round"]
    assert events[0]["data"] == {"round_id": result["round_id"]}
    assert events[1]["data"] == {"round_id": result["round_id"], "ok": True}
