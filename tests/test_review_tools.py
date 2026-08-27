"""src/review 工具层测试：16 个工具经 ReviewToolRegistry.execute 逐一验证。

覆盖：正常返回文本、参数非法转错误文本、未知工具、截断行为、limit 钳制、
calc 计算、submit 成功置 created_version_id、submit 校验拒绝返回原因文本。
"""

import time
from decimal import Decimal

import pytest

from src.memory import Database, Repo
from src.review.tool_handlers import ReviewToolDeps
from src.review.tools import ReviewToolRegistry
from src.review.strategy import StrategyStore

_INIT = "初始策略书：" + "稳健交易，控制回撤。" * 10


@pytest.fixture
async def repo(tmp_path):
    """提供指向临时数据库文件的 Repo 仓储夹具。

    参数：
        tmp_path: Path，pytest 临时目录夹具，测试数据库文件落在其中

    返回：
        AsyncIterator[Repo]，yield 已打开临时数据库的仓储对象，测试结束后关闭连接
    """
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
async def deps(tmp_path, repo):
    """组装测试所需的工具或服务依赖。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        repo: Repo，连接测试数据库的仓储实例

    返回：
        ReviewToolDeps，绑定临时仓储与 paper 模式的复盘工具依赖
    """
    prompt = tmp_path / "system_prompt.md"
    prompt.write_text(_INIT, encoding="utf-8")
    store = StrategyStore(prompt, repo)
    await store.seed_if_empty()  # v1
    return ReviewToolDeps(repo=repo, store=store, mode="paper")


@pytest.fixture
async def registry(deps):
    """组装已注册测试工具的注册表夹具。

    参数：
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        ToolRegistry，绑定复盘工具依赖的注册表
    """
    await _seed_rounds(deps.repo)
    return ReviewToolRegistry(deps)


async def _seed_rounds(repo: Repo) -> None:
    """两轮决策 + 审计轮（含 error 与上下文快照）+ 工具调用链 + 三笔成交。

    参数：
        repo: Repo，连接测试数据库的仓储实例

    返回：
        None，执行上述模拟操作或副作用，无返回值
    """
    await repo.save_decision(
        round_id="round-aaa",
        mode="paper",
        strategy_md5="md5-a",
        wake_source="timer",
        context_summary="看多 BTC，开仓 1 张",
        llm_raw="原始输出" * 200,
    )
    await repo.save_decision(
        round_id="round-bbb", mode="paper", strategy_md5="md5-b", wake_source="price_alert"
    )
    await repo.start_audit_round("round-aaa", "paper", context_snapshot="上下文快照" * 100)
    await repo.start_audit_round("round-bbb", "paper")
    await repo.finish_audit_round("round-bbb", llm_raw="{}", error="LLMError: 超时")
    await repo.save_audit_tool_call(
        "round-aaa",
        1,
        "get_market_data",
        args_json='{"contract":"BTC_USDT"}',
        result_json='{"text":"' + "行" * 600 + '"}',
        duration_ms=12,
    )
    await repo.save_audit_tool_call(
        "round-aaa",
        2,
        "place_order",
        args_json="{}",
        risk_verdict="allow",
        result_json='{"text":"ok"}',
        duration_ms=5,
    )
    await repo.save_trade(
        "round-aaa",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("10"),
        source="llm_close",
        created_at=1000.0,
    )
    await repo.save_trade(
        "round-aaa",
        "paper",
        "ETH_USDT",
        Decimal(-1),
        Decimal("3000"),
        Decimal("1"),
        Decimal("-4"),
        source="user_close",
        created_at=1500.0,
    )
    await repo.save_trade(
        "round-bbb",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("51000"),
        Decimal("1"),
        Decimal("99"),
        source="llm_open",
        created_at=1600.0,
    )


# ---------- get_review_stats ----------


async def test_get_review_stats(registry):
    """验证复盘统计工具返回完整汇总。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_review_stats", {"start_ts": 0, "end_ts": 2000})
    assert "平仓笔数：2" in text  # llm_open 不计入样本
    assert "总盈亏：6" in text
    assert "胜率：0.5000（1/2）" in text
    assert "盈亏比：2.5000" in text


async def test_get_review_stats_missing_ts(registry):
    """验证缺少时间戳的统计参数返回错误。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_review_stats", {"end_ts": 2000})
    assert "参数错误" in text and "start_ts" in text


# ---------- list_decision_rounds ----------


async def test_list_decision_rounds(registry):
    """验证决策轮次工具按预期列出记录。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute(
        "list_decision_rounds", {"start_ts": 0, "end_ts": time.time() + 10}
    )
    assert "round=round-aaa（round-aa）" in text  # 全文 + 前 8 位
    assert "round-bbb" in text
    assert "看多 BTC" in text  # 一行摘要
    assert "LLMError: 超时" in text  # error 来自审计轮
    assert "策略md5=md5-a" in text


async def test_list_decision_rounds_limit_and_clamp(registry):
    """验证决策轮次数量限制会被安全收敛。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    one = await registry.execute(
        "list_decision_rounds", {"start_ts": 0, "end_ts": time.time() + 10, "limit": 1}
    )
    assert "round-bbb" in one and "round-aaa" not in one  # 最新在前，limit 生效
    clamped = await registry.execute(
        "list_decision_rounds", {"start_ts": 0, "end_ts": time.time() + 10, "limit": 999}
    )
    assert "round-aaa" in clamped  # 钳到 100，不报错
    bad = await registry.execute(
        "list_decision_rounds", {"start_ts": 0, "end_ts": 1, "limit": "abc"}
    )
    assert "参数错误" in bad


# ---------- get_decision_detail ----------


async def test_get_decision_detail(registry):
    """验证决策详情工具返回指定轮次。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_decision_detail", {"round_id": "round-aaa"})
    assert "原始输出" in text and "看多 BTC" in text
    truncated = await registry.execute(
        "get_decision_detail", {"round_id": "round-aaa", "max_chars": 10}
    )
    assert "已截断" in truncated
    clamped = await registry.execute(
        "get_decision_detail", {"round_id": "round-aaa", "max_chars": 0}
    )
    assert "已截断" in clamped  # 钳到 1


async def test_get_decision_detail_missing(registry):
    """验证决策详情工具处理不存在轮次。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_decision_detail", {"round_id": "nope"})
    assert "未找到" in text


# ---------- get_tool_call_chain ----------


async def test_get_tool_call_chain(registry):
    """验证工具调用链按执行顺序返回。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_tool_call_chain", {"round_id": "round-aaa"})
    assert text.index("#1 get_market_data") < text.index("#2 place_order")  # 按 seq 排序
    assert "耗时=12ms" in text
    assert "风控=allow" in text
    assert "已截断，原文共" in text  # 600 字结果截到 500


async def test_get_tool_call_chain_empty(registry):
    """验证没有工具调用时返回空调用链。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_tool_call_chain", {"round_id": "round-bbb"})
    assert "无工具调用记录" in text


# ---------- calc ----------


async def test_calc_tool(registry):
    """calc 已注册且可算；参数缺失转错误文本；工具总数 18。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    assert await registry.execute("calc", {"expression": "2*(3-1)^2"}) == "8"
    assert "参数错误" in await registry.execute("calc", {})
    assert len(registry.specs) == 18


# ---------- list_trades ----------


async def test_list_trades(registry):
    """验证复盘工具列出成交记录。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("list_trades", {"start_ts": 0, "end_ts": 2000})
    assert "BTC_USDT" in text and "ETH_USDT" in text
    assert "来源=llm_close" in text and "来源=user_close" in text
    assert "round=round-aa" in text  # round_id 前 8 位


async def test_list_trades_filter_and_limit(registry):
    """验证成交工具支持筛选与数量限制。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    by_source = await registry.execute(
        "list_trades", {"start_ts": 0, "end_ts": 2000, "source": "llm_close"}
    )
    assert "ETH_USDT" not in by_source
    limited = await registry.execute("list_trades", {"start_ts": 0, "end_ts": 2000, "limit": 1})
    assert limited.count("\n- ") == 1


async def test_list_decision_rounds_and_trades_mode_isolation(registry, deps):
    """取数口径对齐 deps.mode（paper）：混合 mode 数据下工具只返回当前模式的轮次/成交。

    参数：
        registry: ToolRegistry，待调用的工具注册表
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    await deps.repo.save_decision(round_id="round-testnet", mode="testnet", strategy_md5="md5-a")
    await deps.repo.save_trade(
        "round-testnet",
        "testnet",
        "BTC_USDT",
        Decimal(1),
        Decimal("60000"),
        Decimal("1"),
        Decimal("5"),
        source="llm_close",
        created_at=1200.0,
    )
    rounds_text = await registry.execute(
        "list_decision_rounds", {"start_ts": 0, "end_ts": time.time() + 10}
    )
    assert "round-testnet" not in rounds_text  # 其他模式轮次不出现
    assert "round-aaa" in rounds_text  # 当前模式轮次不受影响
    trades_text = await registry.execute("list_trades", {"start_ts": 0, "end_ts": 2000})
    assert "60000" not in trades_text  # 其他模式成交不出现
    assert "50000" in trades_text  # 当前模式成交不受影响


# ---------- get_round_context ----------


async def test_get_round_context(registry):
    """验证轮次上下文工具返回输入快照。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_round_context", {"round_id": "round-aaa"})
    assert "上下文快照" in text
    truncated = await registry.execute(
        "get_round_context", {"round_id": "round-aaa", "max_chars": 10}
    )
    assert "已截断" in truncated


async def test_get_round_context_missing(registry):
    """验证轮次上下文工具处理不存在轮次。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_round_context", {"round_id": "nope"})
    assert "未找到" in text


# ---------- get_strategy_versions ----------


async def test_get_strategy_versions_list_and_current(registry):
    """验证策略版本列表标记当前版本。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_strategy_versions", {})
    assert "v1" in text and "human" in text and "初始版本" in text
    assert "当前策略全文" in text and "稳健交易" in text


async def test_get_strategy_versions_single(registry, deps):
    """验证策略版本工具返回指定单个版本。

    参数：
        registry: ToolRegistry，待调用的工具注册表
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    v1 = (await deps.store.list_versions())[0]
    text = await registry.execute("get_strategy_versions", {"version_id": v1.id})
    assert "全文" in text and "稳健交易" in text
    missing = await registry.execute("get_strategy_versions", {"version_id": 999})
    assert "不存在" in missing
    bad = await registry.execute("get_strategy_versions", {"version_id": "x"})
    assert "参数错误" in bad


# ---------- submit_strategy_revision ----------


async def test_submit_strategy_revision_success(registry, deps):
    """验证合法策略修订成功生成新版本。

    参数：
        registry: ToolRegistry，待调用的工具注册表
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    new = "新策略书：" + "顺势加仓，严格止损。" * 10
    text = await registry.execute(
        "submit_strategy_revision", {"new_prompt_md": new, "reason": "收紧止损"}
    )
    assert deps.created_version_id is not None
    assert f"v{deps.created_version_id}" in text
    # 草稿语义（issue #62/#73）：工具调用不动文件，报告成功后由 agent 统一生效
    assert deps.store.current() != new
    await deps.store.apply_version(deps.created_version_id)
    assert deps.store.current() == new


async def test_submit_strategy_revision_rejected(registry, deps):
    """验证不满足约束的策略修订被拒绝。

    参数：
        registry: ToolRegistry，待调用的工具注册表
        deps: 依赖对象，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute(
        "submit_strategy_revision", {"new_prompt_md": "太短", "reason": "r"}
    )
    assert "校验拒绝" in text and "100" in text
    assert deps.created_version_id is None  # 拒绝不置位


# ---------- 注册表统一错误 ----------


async def test_unknown_tool(registry):
    """验证注册表拒绝未知工具名。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("nope", {})
    assert "未知工具" in text and "get_review_stats" in text


async def test_args_not_dict(registry):
    """验证注册表拒绝非字典工具参数。

    参数：
        registry: ToolRegistry，待调用的工具注册表

    返回：
        None，通过断言验证上述行为，无返回值
    """
    text = await registry.execute("get_review_stats", "not-a-dict")
    assert text == "参数错误：工具参数必须是对象"
