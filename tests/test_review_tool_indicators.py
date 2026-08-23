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
        """初始化测试替身并保存后续调用所需的预设数据。

        参数：
            candles: list[Candle]，预设 K 线序列

        返回：
            None，初始化当前测试替身，无返回值
        """
        self._candles = candles

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        """返回测试替身中预设的最近 K 线。

        参数：
            contract: str，目标合约标识
            interval: str，K 线周期
            n: int，需要读取或生成的记录数量

        返回：
            list[Candle]，预置 K 线序列末尾最多 n 根记录
        """
        return self._candles[-n:]


class _OiStub:
    """OI 缓存 stub（鸭子类型对齐 OpenInterestCache.get）。"""

    def __init__(self, value: Decimal | None) -> None:
        """初始化测试替身并保存后续调用所需的预设数据。

        参数：
            value: Decimal | None，待设置或预置的值

        返回：
            None，初始化当前测试替身，无返回值
        """
        self._value = value

    def get(self, contract: str) -> Decimal | None:
        """返回测试替身中指定合约的预设值。

        参数：
            contract: str，目标合约标识

        返回：
            Decimal | None，当前替身保存的持仓量值
        """
        return self._value


def _candles(n: int) -> list[Candle]:
    """n 根 1h 阳线（收盘 100+i 递增），保证各指标有确定非 None 值。

    参数：
        n: int，需要读取或生成的记录数量

    返回：
        list[Candle]，时间递增且收盘价逐根上涨的 n 根模拟 K 线
    """
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
    """提供连接临时数据库的仓储夹具。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        AsyncIterator[Repo]，提供临时数据库仓储并在测试结束后关闭连接
    """
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
async def deps(tmp_path, repo):
    """完整接线 deps：指标服务（60 根 K 线 + OI=12345）+ 短名单 store + watchlist。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        repo: Repo，连接测试数据库的仓储实例

    返回：
        ReviewToolDeps，指标服务与配置仓储均已接线的复盘工具依赖
    """
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
    """未接线指标依赖的 deps（indicator_service / indicator_config_store 均为 None）。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        repo: Repo，连接测试数据库的仓储实例

    返回：
        ReviewToolDeps，指标服务与配置仓储均未接线的降级依赖
    """
    store = StrategyStore(tmp_path / "system_prompt.md", repo)
    return ReviewToolDeps(repo=repo, store=store, mode="paper")


@pytest.fixture
async def registry(deps):
    """组装已注册测试工具的注册表夹具。

    参数：
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        ToolRegistry，绑定完整复盘依赖的工具注册表
    """
    return ReviewToolRegistry(deps)


# ---------- get_indicators ----------


async def test_get_indicators_panel(registry):
    """验证复盘指标面板工具返回完整指标文本。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_indicators", {"contract": "BTC_USDT"})
    assert "BTC_USDT 指标面板（1h）" in text  # 缺省周期与执行 agent 惯例一致
    assert "截至" in text  # 有 K 线时给面板时刻
    assert "- ema9 | EMA9(指数均线)：" in text
    assert "- macd | MACD(异同均线)：dif=" in text  # 多字段指标逐字段列出
    assert "- oi | 持仓量：12345" in text  # OI 来自 OI 缓存
    assert "无数据" not in text  # 60 根 K 线覆盖全部 min_candles


async def test_get_indicators_no_data_fallback(deps):
    """K 线不足 min_candles 的指标与缺失 OI 均显示 无数据。

    参数：
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    deps.indicator_service = IndicatorService(_CandleStub(_candles(10)), _OiStub(None))
    text = await ReviewToolRegistry(deps).execute("get_indicators", {"contract": "BTC_USDT"})
    assert "- ema50 | EMA50(指数均线)：无数据" in text  # ema50 需 50 根
    assert "- oi | 持仓量：无数据" in text
    assert "- ema9 | EMA9(指数均线)：无数据" not in text  # ema9 只需 9 根，有值


async def test_get_indicators_interval_param(registry):
    """验证指标面板工具透传指定 K 线周期。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_indicators", {"contract": "ETH_USDT", "interval": "4h"})
    assert "ETH_USDT 指标面板（4h）" in text
    bad = await registry.execute("get_indicators", {"contract": "BTC_USDT", "interval": "3h"})
    assert "参数错误" in bad and "interval" in bad


async def test_get_indicators_watchlist_guard(registry):
    """验证指标工具拒绝查询关注列表外合约。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_indicators", {"contract": "DOGE_USDT"})
    assert "不在 watchlist" in text and "BTC_USDT" in text  # 给出可选集合
    missing = await registry.execute("get_indicators", {})
    assert "参数错误" in missing and "contract" in missing


# ---------- get_indicator_config ----------


async def test_get_indicator_config(registry):
    """验证指标配置工具返回当前短名单与参数。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_indicator_config", {})
    assert "当前指标短名单" in text
    for key in DEFAULT_INDICATOR_SHORTLIST:
        assert key in text  # 文件未创建时回落默认基线
    for key in REGISTRY:
        assert key in text  # 可选全集菜单覆盖全部注册键
    assert "主图叠加" in text and "副图" in text and "单值" in text  # kind 中文释义


# ---------- submit_indicator_config ----------


async def test_submit_indicator_config_success(registry, deps, repo):
    """验证合法指标配置修订成功落库并生效。

    参数：
        registry: ToolRegistry，待调用的工具注册表
        deps: 依赖对象，测试应用或工具的依赖集合
        repo: Repo，连接测试数据库的仓储实例

    返回：
        None，通过断言验证上述行为，无返回值
    """
    new_list = ["ema20", "rsi14", "macd", "boll"]
    reason = "round-aaa 的 BTC_USDT 亏损源于 ema50 全程无信号，换 boll 捕捉波动"
    text = await registry.execute(
        "submit_indicator_config", {"shortlist": new_list, "reason": reason}
    )
    assert "校验通过" in text
    assert deps.indicator_config_version_id is not None  # 成果记进 deps（同策略修订模式）
    assert f"v{deps.indicator_config_version_id}" in text
    # 草稿语义（issue #62/#73）：显式生效后文件才更新
    await deps.indicator_config_store.apply_version(deps.indicator_config_version_id)
    assert "boll" in text  # 生效短名单回显
    assert deps.indicator_config_store.load_current().shortlist == new_list  # 文件已生效
    versions = await repo.indicator_config.list_versions()
    assert len(versions) == 1
    assert versions[0].id == deps.indicator_config_version_id
    assert versions[0].created_by == "review_agent"
    assert versions[0].reason == reason
    assert versions[0].report_id is None  # 报告关联由轮末装配回填（同策略修订取法）


async def test_submit_indicator_config_dedup(registry, deps):
    """重复键去重保序后生效（提交 3 个含重复键，生效 2 个）。

    参数：
        registry: ToolRegistry，待调用的工具注册表
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute(
        "submit_indicator_config",
        {"shortlist": ["rsi14", "ema20", "rsi14"], "reason": "去重验证"},
    )
    assert "校验通过" in text and "2 个" in text
    await deps.indicator_config_store.apply_version(deps.indicator_config_version_id)
    assert deps.indicator_config_store.load_current().shortlist == ["rsi14", "ema20"]


async def test_submit_indicator_config_unknown_key(registry, deps):
    """验证指标配置拒绝未知指标键。

    参数：
        registry: ToolRegistry，待调用的工具注册表
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute(
        "submit_indicator_config", {"shortlist": ["ema20", "sma20"], "reason": "尝试未知键"}
    )
    assert "校验拒绝" in text and "未知指标键" in text and "sma20" in text
    assert deps.indicator_config_version_id is None  # 拒绝不置位
    assert not await deps.repo.indicator_config.list_versions()  # 拒绝不落版本


async def test_submit_indicator_config_too_many(registry, deps):
    """验证指标配置拒绝超过上限的短名单。

    参数：
        registry: ToolRegistry，待调用的工具注册表
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    nine = ["ema9", "ema20", "ema50", "macd", "rsi7", "rsi14", "kdj", "roc10", "atr14"]
    text = await registry.execute(
        "submit_indicator_config", {"shortlist": nine, "reason": "堆叠 9 个指标"}
    )
    assert "校验拒绝" in text and "1~8" in text
    assert deps.indicator_config_version_id is None


async def test_submit_indicator_config_no_diff(registry, deps):
    """验证无实际变化的指标配置不会生成新版本。

    参数：
        registry: ToolRegistry，待调用的工具注册表
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute(
        "submit_indicator_config",
        {"shortlist": list(DEFAULT_INDICATOR_SHORTLIST), "reason": "与当前相同"},
    )
    assert "校验拒绝" in text and "无差异" in text
    assert deps.indicator_config_version_id is None


async def test_submit_indicator_config_bad_args(registry, deps):
    """验证指标配置工具拒绝错误参数形状。

    参数：
        registry: ToolRegistry，待调用的工具注册表
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """验证指标依赖未接线时工具明确降级。

    参数：
        bare_deps: ReviewToolDeps，未接线指标服务的复盘工具依赖

    返回：
        None，通过断言验证上述行为，无返回值
    """
    registry = ReviewToolRegistry(bare_deps)
    text = await registry.execute("get_indicators", {"contract": "BTC_USDT"})
    assert "指标功能未配置" in text
    assert "指标功能未配置" in await registry.execute("get_indicator_config", {})
    submit = await registry.execute(
        "submit_indicator_config", {"shortlist": ["ema20"], "reason": "r"}
    )
    assert "指标功能未配置" in submit


async def test_indicator_tools_registered(registry):
    """验证指标相关工具均已注册并可调用。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
        """初始化测试替身并保存后续调用所需的预设数据。

        参数：
            script: list，按调用顺序消费的模拟响应脚本

        返回：
            None，初始化当前测试替身，无返回值
        """
        self._script = deque(script)

    async def chat(self, system: str, messages: list[dict], tools: list[dict]):
        """按测试脚本顺序返回模拟的 LLM 响应或异常。

        参数：
            system: str，传给 LLM 的系统提示词
            messages: list[dict]，传给 LLM 的消息历史
            tools: list[dict]，传给 LLM 的工具定义

        返回：
            SimpleNamespace，脚本队首的模拟 LLM 响应
        """
        return self._script.popleft()

    def tool_result_message(self, call, result: str) -> dict:
        """把工具调用结果封装为模拟提供商消息。

        参数：
            call: ToolCall，待封装的工具调用
            result: str，工具执行结果文本

        返回：
            dict，包含 role=tool 与结果内容的模拟工具消息
        """
        return {"role": "tool", "content": result}


def _resp(text: str = "", calls: tuple = ()) -> SimpleNamespace:
    """构造带文本和工具调用的模拟 LLM 响应。

    参数：
        text: str，响应或待校验文本
        calls: tuple，模拟工具调用序列

    返回：
        SimpleNamespace，包含文本、原始响应、空助手消息与工具调用列表的模拟响应
    """
    return SimpleNamespace(text=text, raw="{}", assistant_message=None, tool_calls=list(calls))


async def test_review_agent_wires_indicator_deps(tmp_path, repo, deps):
    """构造期注入的指标依赖进入每轮 deps：LLM 调 get_indicator_config 返回配置文本。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        repo: Repo，连接测试数据库的仓储实例
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """build_review 逐项接通指标依赖：装配出的 agent 轮内 deps 拿到实例（工具非降级）。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        repo: Repo，连接测试数据库的仓储实例
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """build_review 透传 notify_event：装配出的 agent 轮始/轮末事件经装配层广播（透传不断）。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        repo: Repo，连接测试数据库的仓储实例

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    assert events[1]["data"] == {
        "round_id": result["round_id"],
        "ok": True,
        "applied": True,
    }
