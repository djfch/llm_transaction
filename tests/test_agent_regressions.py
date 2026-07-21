"""第二波对抗性审查回归（loop 侧）：成交落库、审计双轨合一、失败轮审计痕迹、风控锁持久化。

覆盖缺陷：P0-#1（trades 表恒空）、P2-#14（AuditTrail 零调用）、
P2-#16（context.build 失败无审计）、P1-#9（风控锁只写内存）。
"""

from __future__ import annotations

import json
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

import yaml

from src.agent import DecisionLoop, LLMError, LLMResponse, PromptLoader, ToolCall
from src.agent.loop import default_daily_stats
from src.config import AuditConfig, LLMConfig, PaperConfig, Settings
from src.config_io import read_settings_raw, write_settings
from src.gateway.base import Contract
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.paper.engine import PaperGateway
from src.risk.engine import RiskEngine


class SeqProvider:
    """预置响应序列的 mock provider；元素为异常则抛出。"""

    def __init__(self, responses: list) -> None:
        self._responses = deque(responses)

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        if not self._responses:
            return LLMResponse(text="（无更多预置响应）", raw="{}")
        item = self._responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        return {"role": "tool", "tool_call_id": call.call_id, "content": result}


def _resp(text: str, calls: list[ToolCall], raw: str = "{}") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=calls,
        raw=raw,
        assistant_message={"role": "assistant", "content": text or "（调用工具）"},
    )


def _contract(name: str, quanto: str, mark: str) -> Contract:
    return Contract(
        name=name,
        quanto_multiplier=Decimal(quanto),
        order_size_min=Decimal(1),
        order_size_max=Decimal("1000000"),
        order_price_round=Decimal("0.1"),
        enable_decimal=False,
        mark_price=Decimal(mark),
        funding_rate=Decimal("0.0001"),
        funding_interval=28800,
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        status="trading",
        in_delisting=False,
    )


async def _make_loop(
    tmp_path,
    provider: SeqProvider,
    *,
    gateway=None,
    drain_fills=None,
    persist_kill_switch=None,
    max_failures: int = 3,
) -> SimpleNamespace:
    """组装最小决策循环；审计快照目录隔离到 tmp_path。新依赖参数按名透传。"""
    db = Database()
    await db.open(tmp_path / "agent.db")
    repo = Repo(db)
    gateway = gateway or MockGateway(
        contracts={"BTC_USDT": _contract("BTC_USDT", "0.001", "60000")}
    )
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("# 策略书\n稳健交易，控制回撤。", encoding="utf-8")
    settings = Settings(
        llm=LLMConfig(max_consecutive_failures=max_failures),
        audit=AuditConfig(dir=str(tmp_path / "audit")),
    )
    extra = {}
    if drain_fills is not None:
        extra["drain_fills"] = drain_fills
    if persist_kill_switch is not None:
        extra["persist_kill_switch"] = persist_kill_switch
    loop = DecisionLoop(
        settings=settings,
        watchlist=["BTC_USDT"],
        provider=provider,
        gateway=gateway,
        risk_engine=RiskEngine(),
        repo=repo,
        candles=CandleCache(gateway, ManualPriceSource()),
        triggers=TriggerManager(lambda t, p: None),
        prompt_loader=PromptLoader(prompt_path),
        **extra,
    )
    return SimpleNamespace(db=db, repo=repo, gateway=gateway, loop=loop, settings=settings)


# ---------- P0-#1 paper 全链路：成交经 drain_fills 落 trades 表 ----------


async def test_paper_fills_persisted_to_trades(tmp_path):
    gateway = PaperGateway(PaperConfig(initial_equity=Decimal("10000")))
    gateway.upsert_contract(_contract("BTC_USDT", "0.001", "60000"))
    gateway.on_price("BTC_USDT", Decimal("60000"))
    provider = SeqProvider(
        [
            _resp(
                "开仓后立即平仓",
                [
                    ToolCall(
                        "place_order",
                        {
                            "contract": "BTC_USDT",
                            "size": 1,
                            "leverage": 2,
                            "stop_loss_price": 58000,
                        },
                        "c1",
                    ),
                    ToolCall("place_order", {"contract": "BTC_USDT", "close": True}, "c2"),
                ],
            ),
            _resp("完成", []),
        ]
    )
    env = await _make_loop(tmp_path, provider, gateway=gateway, drain_fills=gateway.drain_fills)
    try:
        result = await env.loop.run_once("timer")
        assert result.ok
        trades = await env.repo.list_trades()
        assert len(trades) == 2  # 开/平各一笔；drain 落库，不发生双计
        assert all(t.round_id == result.round_id and t.mode == "paper" for t in trades)
        stats = await env.repo.daily_stats("paper", 0.0)
        assert stats.realized_pnl != 0  # 滑点使平仓产生非零已实现盈亏，rule_daily_loss 不再失明
        assert stats.realized_pnl == sum((t.pnl for t in trades), Decimal(0))
    finally:
        await env.db.close()


# ---------- P0-#1 default_daily_stats 走 repo 公共方法（按 mode 过滤） ----------


async def test_default_daily_stats_via_repo(tmp_path):
    db = Database()
    await db.open(tmp_path / "stats.db")
    repo = Repo(db)
    try:
        await repo.save_trade(
            round_id="r",
            mode="paper",
            contract="BTC_USDT",
            size=Decimal(1),
            price=Decimal("60000"),
            fee=Decimal("0.03"),
            pnl=Decimal("-5"),
        )
        await repo.save_trade(
            round_id="r",
            mode="live",
            contract="BTC_USDT",
            size=Decimal(1),
            price=Decimal("60000"),
            fee=Decimal(0),
            pnl=Decimal("100"),
        )  # 其他 mode 不计入
        await repo.save_order(
            order_id="o1", round_id="r", mode="paper", contract="BTC_USDT", side_size=Decimal(1)
        )
        stats = await default_daily_stats(repo, "paper")
        assert stats.realized_pnl == Decimal("-5")
        assert stats.orders_today == 1
    finally:
        await db.close()


# ---------- P2-#14 审计双轨合一：一轮决策生成 JSON 全文快照 ----------


async def test_round_writes_audit_json_snapshot(tmp_path):
    provider = SeqProvider(
        [
            _resp(
                "账户上下文已注入",
                [ToolCall("get_market_data", {"contract": "BTC_USDT"}, "c1")],
                '{"turn":1}',
            ),
            _resp("观望", [], '{"turn":2}'),
        ]
    )
    env = await _make_loop(tmp_path, provider)
    try:
        result = await env.loop.run_once("timer")
        assert result.ok
        path = tmp_path / "audit" / f"round_{result.round_id}.json"
        assert path.exists()  # AuditTrail 快照真实生成
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "稳健交易" in data["round"]["prompt_snapshot"]
        assert data["round"]["context_snapshot"]  # 上下文构建后回填
        assert data["round"]["llm_raw"] == '{"turn":1}\n{"turn":2}'  # 各轮原始输出拼接
        assert [c["tool"] for c in data["tool_calls"]] == ["get_market_data"]
        assert "risk_verdict" in data["tool_calls"][0]
    finally:
        await env.db.close()


# ---------- P2-#16 context.build 失败也留审计痕迹 ----------


async def test_context_build_failure_leaves_audit_trace(tmp_path):
    class BrokenGateway(MockGateway):
        def get_account(self):  # context.build 的首个网关调用即失败
            raise RuntimeError("market down")

    gateway = BrokenGateway(contracts={"BTC_USDT": _contract("BTC_USDT", "0.001", "60000")})
    env = await _make_loop(tmp_path, SeqProvider([_resp("不应到达", [])]), gateway=gateway)
    try:
        result = await env.loop.run_once("timer")
        assert not result.ok and "market down" in result.error
        row = await env.repo.get_audit_round(result.round_id)
        assert row is not None  # 失败轮落审计行
        assert "market down" in row.error and row.ended_at is not None
    finally:
        await env.db.close()


# ---------- P1-#9 风控锁写回 config.yaml（经注入回调，tmp_path 隔离） ----------


async def test_lock_persists_kill_switch_to_config_yaml(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mode: paper\nrisk:\n  kill_switch: false\n", encoding="utf-8")

    def persist_kill_switch(enabled: bool) -> None:
        raw = read_settings_raw(cfg)
        raw.setdefault("risk", {})["kill_switch"] = enabled
        write_settings(raw, cfg)

    env = await _make_loop(
        tmp_path,
        SeqProvider([LLMError("boom")]),
        persist_kill_switch=persist_kill_switch,
        max_failures=1,
    )
    try:
        result = await env.loop.run_once("timer")
        assert not result.ok and env.loop.risk_locked
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert data["risk"]["kill_switch"] is True  # 重启后锁仍生效
    finally:
        await env.db.close()
