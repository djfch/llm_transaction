"""src/review 工具层测试：8 个工具经 ReviewToolRegistry.execute 逐一验证。

覆盖：正常返回文本、参数非法转错误文本、未知工具、截断行为、limit 钳制、
submit 成功置 created_version_id、submit 校验拒绝返回原因文本。
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
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
async def deps(tmp_path, repo):
    prompt = tmp_path / "system_prompt.md"
    prompt.write_text(_INIT, encoding="utf-8")
    store = StrategyStore(prompt, repo)
    await store.seed_if_empty()  # v1
    return ReviewToolDeps(repo=repo, store=store, mode="paper")


@pytest.fixture
async def registry(deps):
    await _seed_rounds(deps.repo)
    return ReviewToolRegistry(deps)


async def _seed_rounds(repo: Repo) -> None:
    """两轮决策 + 审计轮（含 error 与上下文快照）+ 工具调用链 + 三笔成交。"""
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
    text = await registry.execute("get_review_stats", {"start_ts": 0, "end_ts": 2000})
    assert "平仓笔数：2" in text  # llm_open 不计入样本
    assert "总盈亏：6" in text
    assert "胜率：0.5000（1/2）" in text
    assert "盈亏比：2.5000" in text


async def test_get_review_stats_missing_ts(registry):
    text = await registry.execute("get_review_stats", {"end_ts": 2000})
    assert "参数错误" in text and "start_ts" in text


# ---------- list_decision_rounds ----------


async def test_list_decision_rounds(registry):
    text = await registry.execute(
        "list_decision_rounds", {"start_ts": 0, "end_ts": time.time() + 10}
    )
    assert "round=round-aaa（round-aa）" in text  # 全文 + 前 8 位
    assert "round-bbb" in text
    assert "看多 BTC" in text  # 一行摘要
    assert "LLMError: 超时" in text  # error 来自审计轮
    assert "策略md5=md5-a" in text


async def test_list_decision_rounds_limit_and_clamp(registry):
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
    text = await registry.execute("get_decision_detail", {"round_id": "nope"})
    assert "未找到" in text


# ---------- get_tool_call_chain ----------


async def test_get_tool_call_chain(registry):
    text = await registry.execute("get_tool_call_chain", {"round_id": "round-aaa"})
    assert text.index("#1 get_market_data") < text.index("#2 place_order")  # 按 seq 排序
    assert "耗时=12ms" in text
    assert "风控=allow" in text
    assert "已截断，原文共" in text  # 600 字结果截到 500


async def test_get_tool_call_chain_empty(registry):
    text = await registry.execute("get_tool_call_chain", {"round_id": "round-bbb"})
    assert "无工具调用记录" in text


# ---------- list_trades ----------


async def test_list_trades(registry):
    text = await registry.execute("list_trades", {"start_ts": 0, "end_ts": 2000})
    assert "BTC_USDT" in text and "ETH_USDT" in text
    assert "来源=llm_close" in text and "来源=user_close" in text
    assert "round=round-aa" in text  # round_id 前 8 位


async def test_list_trades_filter_and_limit(registry):
    by_source = await registry.execute(
        "list_trades", {"start_ts": 0, "end_ts": 2000, "source": "llm_close"}
    )
    assert "ETH_USDT" not in by_source
    limited = await registry.execute("list_trades", {"start_ts": 0, "end_ts": 2000, "limit": 1})
    assert limited.count("\n- ") == 1


async def test_list_decision_rounds_and_trades_mode_isolation(registry, deps):
    """取数口径对齐 deps.mode（paper）：混合 mode 数据下工具只返回当前模式的轮次/成交。"""
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
    text = await registry.execute("get_round_context", {"round_id": "round-aaa"})
    assert "上下文快照" in text
    truncated = await registry.execute(
        "get_round_context", {"round_id": "round-aaa", "max_chars": 10}
    )
    assert "已截断" in truncated


async def test_get_round_context_missing(registry):
    text = await registry.execute("get_round_context", {"round_id": "nope"})
    assert "未找到" in text


# ---------- get_strategy_versions ----------


async def test_get_strategy_versions_list_and_current(registry):
    text = await registry.execute("get_strategy_versions", {})
    assert "v1" in text and "human" in text and "初始版本" in text
    assert "当前策略全文" in text and "稳健交易" in text


async def test_get_strategy_versions_single(registry, deps):
    v1 = (await deps.store.list_versions())[0]
    text = await registry.execute("get_strategy_versions", {"version_id": v1.id})
    assert "全文" in text and "稳健交易" in text
    missing = await registry.execute("get_strategy_versions", {"version_id": 999})
    assert "不存在" in missing
    bad = await registry.execute("get_strategy_versions", {"version_id": "x"})
    assert "参数错误" in bad


# ---------- submit_strategy_revision ----------


async def test_submit_strategy_revision_success(registry, deps):
    new = "新策略书：" + "顺势加仓，严格止损。" * 10
    text = await registry.execute(
        "submit_strategy_revision", {"new_prompt_md": new, "reason": "收紧止损"}
    )
    assert deps.created_version_id is not None
    assert f"v{deps.created_version_id}" in text
    assert deps.store.current() == new  # 策略书文件已更新


async def test_submit_strategy_revision_rejected(registry, deps):
    text = await registry.execute(
        "submit_strategy_revision", {"new_prompt_md": "太短", "reason": "r"}
    )
    assert "校验拒绝" in text and "100" in text
    assert deps.created_version_id is None  # 拒绝不置位


# ---------- 注册表统一错误 ----------


async def test_unknown_tool(registry):
    text = await registry.execute("nope", {})
    assert "未知工具" in text and "get_review_stats" in text


async def test_args_not_dict(registry):
    text = await registry.execute("get_review_stats", "not-a-dict")
    assert text == "参数错误：工具参数必须是对象"
