"""src/agent 决策循环全链路测试：MockProvider（预置响应序列）+ MockGateway + tmp_path SQLite。

覆盖：完整一轮工具调用落审计、风控拒绝被记录且不下单、解析失败不交易、
连续失败触发风控锁并告警、参数校验失败返回错误文本、set_leverage 风控、
PromptLoader mtime 热重载。
"""

import hashlib
import json
import os
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.agent import (
    DecisionLoop,
    LLMError,
    LLMParseError,
    LLMResponse,
    PromptLoader,
    ToolCall,
    ToolSpec,
)
from src.agent.tool_schemas import SCHEMAS
from src.agent.tools import _HANDLERS
from src.config import AuditConfig, LLMConfig, Settings
from src.gateway.base import Candle, Contract, Ticker
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import MAX_ALERTS, TriggerManager
from src.memory import Database, Repo
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats


class MockProvider:
    """预置响应序列的 mock provider：每次 chat 弹出下一个；元素为异常则抛出。"""

    def __init__(self, responses: list) -> None:
        """初始化 mock provider：预置响应存入队列，准备记录调用快照。

        参数：
            responses: list，预置响应序列；元素为 LLMResponse，或用于模拟失败的异常实例

        返回：
            None，副作用为初始化内部响应队列与调用快照列表 calls
        """
        self._responses = deque(responses)
        self.calls: list[dict] = []  # 每次 chat 的 (system, messages, tools) 快照

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """记录本次调用快照并弹出下一个预置响应；队列空则返回占位响应。

        参数：
            system: str，系统提示词原文
            messages: list[dict]，本轮对话消息列表
            tools: list[dict]，可用工具的 schema 列表

        返回：
            LLMResponse：队列中下一个预置响应；无预置响应时返回占位文本响应

        异常：
            Exception：预置元素为异常实例时原样抛出（模拟 LLM 调用失败）
        """
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if not self._responses:
            return LLMResponse(text="（无更多预置响应）", raw="{}")
        item = self._responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """构造工具结果回填消息，与真实 provider 的回填格式一致。

        参数：
            call: ToolCall，已执行的工具调用（取其 call_id 关联结果）
            result: str，工具执行结果文本

        返回：
            dict：role 为 tool、按 tool_call_id 关联调用的消息字典
        """
        return {"role": "tool", "tool_call_id": call.call_id, "content": result}


def _contract(name: str, quanto: str, mark: str) -> Contract:
    """构造测试用合约对象，仅关键字段参数化，其余取固定常用值。

    参数：
        name: str，合约名（如 BTC_USDT）
        quanto: str，合约乘数字符串（内部转为 Decimal）
        mark: str，标记价格字符串（内部转为 Decimal）

    返回：
        Contract：交易状态、未下架、含固定费率与下单上下限的测试合约
    """
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


def _resp(text: str, calls: list[ToolCall], raw: str) -> LLMResponse:
    """构造预置的 LLM 响应，附带 assistant 回填消息。

    参数：
        text: str，助手可见文本（空串表示本轮纯工具调用）
        calls: list[ToolCall]，本轮要求执行的工具调用列表
        raw: str，原始响应文本（落审计 llm_raw 用）

    返回：
        LLMResponse：含工具调用与 assistant_message 的完整响应
    """
    return LLMResponse(
        text=text,
        tool_calls=calls,
        raw=raw,
        assistant_message={"role": "assistant", "content": text or "（调用工具）"},
    )


async def _make_env(tmp_path, *, max_failures: int = 3) -> SimpleNamespace:
    """搭建决策循环测试环境：临时 SQLite + MockGateway + 行情/触发器/策略书。

    参数：
        tmp_path: Path，pytest 临时目录夹具，数据库、策略书与审计快照目录落其中
        max_failures: int，连续失败熔断阈值（写入 LLMConfig，默认 3）

    返回：
        SimpleNamespace：装配好的测试环境，含 db/repo/gateway/candles/triggers、
        watchlist、settings、prompt_loader，以及 wake_calls/alerts 收集列表
    """
    db = Database()
    await db.open(tmp_path / "agent.db")
    repo = Repo(db)
    gateway = MockGateway(
        contracts={
            "BTC_USDT": _contract("BTC_USDT", "0.001", "60000"),
            "ETH_USDT": _contract("ETH_USDT", "0.01", "3000"),
        }
    )
    gateway.tickers = [
        Ticker(
            contract="BTC_USDT",
            last=Decimal("60010"),
            mark_price=Decimal("60000"),
            funding_rate=Decimal("0.0001"),
            high_24h=Decimal("61000"),
            low_24h=Decimal("59000"),
            change_percentage=Decimal("1.5"),
        ),
    ]
    gateway.candles = [
        Candle(
            t=1000 * i,
            o=Decimal("59000"),
            h=Decimal("60500"),
            l=Decimal("58500"),
            c=Decimal("60000"),
            v=Decimal("100"),
        )
        for i in range(1, 4)
    ]
    candles = CandleCache(gateway, ManualPriceSource())
    candles.backfill(["BTC_USDT", "ETH_USDT"], ["1h"], limit=24)
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("# 策略书\n稳健交易，控制回撤。", encoding="utf-8")
    env = SimpleNamespace(
        db=db,
        repo=repo,
        gateway=gateway,
        candles=candles,
        triggers=TriggerManager(lambda t, p: None),
        watchlist=["BTC_USDT", "ETH_USDT"],
        wake_calls=[],
        alerts=[],
        settings=Settings(
            llm=LLMConfig(max_consecutive_failures=max_failures),
            audit=AuditConfig(dir=str(tmp_path / "audit")),  # 快照目录隔离，不污染仓库
        ),
        prompt_loader=PromptLoader(prompt_path),
    )

    def set_next_wake(minutes: int) -> int:
        """按调度范围裁剪唤醒分钟数并记录原值与生效值。

        参数：
            minutes: int，工具请求的下一次唤醒间隔分钟数

        返回：
            int：限制在 5 至 720 分钟内的实际唤醒间隔
        """
        effective = max(5, min(720, minutes))
        env.wake_calls.append((minutes, effective))
        return effective

    async def fake_daily_stats() -> DailyStats:
        """返回无盈亏、无订单的固定当日统计。

        参数：
            无

        返回：
            DailyStats：已实现盈亏和今日订单数均为零的统计对象
        """
        return DailyStats(realized_pnl=Decimal(0), orders_today=0)

    env.set_next_wake = set_next_wake
    env.daily_stats_fn = fake_daily_stats
    return env


def _make_loop(env: SimpleNamespace, provider: MockProvider, **kwargs) -> DecisionLoop:
    """组装可控的决策循环测试实例。

    参数：
        env: SimpleNamespace，已组装的测试环境
        provider: MockProvider，LLM 提供者测试替身
    **kwargs: object，覆盖 DecisionLoop 可选依赖的关键字参数

    返回：
    DecisionLoop：复用隔离环境依赖并注入指定提供者的决策循环
    """
    return DecisionLoop(
        settings=env.settings,
        watchlist=env.watchlist,
        provider=provider,
        gateway=env.gateway,
        risk_engine=RiskEngine(),
        repo=env.repo,
        candles=env.candles,
        triggers=env.triggers,
        prompt_loader=env.prompt_loader,
        set_next_wake=env.set_next_wake,
        on_alert=env.alerts.append,
        daily_stats_fn=env.daily_stats_fn,
        **kwargs,
    )


@pytest.fixture
async def env(tmp_path):
    """组装决策循环的隔离测试环境。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        AsyncIterator[SimpleNamespace]：yield 完整决策测试环境，并在用例收尾关闭数据库
    """
    env = await _make_env(tmp_path)
    yield env
    await env.db.close()


# ---------- 完整一轮：工具调用全部落审计 ----------


async def test_full_round_audited(env: SimpleNamespace):
    """验证三轮 LLM 对话、五次工具调用及完整快照均写入审计。

    参数：
        env: SimpleNamespace，已组装的测试环境

    返回：
        None：通过断言校验目标场景，无返回值
    """
    provider = MockProvider(
        [
            _resp(
                "先看行情",
                [
                    ToolCall("get_market_data", {"contract": "BTC_USDT", "interval": "1h"}, "c1"),
                ],
                '{"turn":1}',
            ),
            _resp(
                "",
                [
                    ToolCall(
                        "place_order",
                        {
                            "contract": "BTC_USDT",
                            "size": 1,
                            "leverage": 2,
                            "stop_loss_price": 58000,
                        },
                        "c3",
                    ),
                    ToolCall(
                        "set_price_alert",
                        {"contract": "BTC_USDT", "direction": "above", "price": 65000},
                        "c4",
                    ),
                    ToolCall("write_note", {"content": "BTC 走强，开多 1 张"}, "c5"),
                    ToolCall("set_next_wakeup", {"minutes": 30}, "c6"),
                ],
                '{"turn":2}',
            ),
            _resp("本轮完成：已开多并设置预警", [], '{"turn":3}'),
        ]
    )
    result = await _make_loop(env, provider).run_once("timer:60min")

    assert result.ok and result.tool_calls == 5
    assert len(provider.calls) == 3  # 多轮对话：工具结果回填后再问
    round_row = await env.repo.get_audit_round(result.round_id)
    assert round_row is not None and round_row.ended_at is not None
    assert round_row.wake_source == "timer:60min" and round_row.error == ""
    assert "稳健交易" in round_row.prompt_snapshot  # 策略书原文
    assert "## 可用工具" in round_row.prompt_snapshot  # 工具说明自动拼接
    assert "BTC_USDT" in round_row.context_snapshot  # 上下文快照
    assert "价格预警线" in round_row.context_snapshot  # 价格预警线段（本轮设置前为空列表）
    assert '{"turn":3}' in round_row.llm_raw

    calls = await env.repo.list_audit_tool_calls(result.round_id)
    assert [c.seq for c in calls] == [1, 2, 3, 4, 5]
    assert [c.tool for c in calls] == [
        "get_market_data",
        "place_order",
        "set_price_alert",
        "write_note",
        "set_next_wakeup",
    ]
    place = calls[1]
    assert place.risk_verdict == "allow" and place.risk_reason == ""
    assert json.loads(place.args_json)["size"] == 1
    assert "下单成功" in json.loads(place.result_json)["text"]

    assert len(env.gateway.placed) == 1  # 风控放行，真实下单
    orders = await env.repo.list_orders(result.round_id)
    assert len(orders) == 1 and orders[0].status == "finished"
    assert (await env.repo.recent_notes(1))[0].content == "BTC 走强，开多 1 张"
    assert len(env.triggers.list("BTC_USDT")) == 1  # 预警仅存于内存 TriggerManager
    assert env.wake_calls == [(30, 30)]  # 请求值与钳制后生效值

    decisions = await env.repo.list_decisions()
    assert len(decisions) == 1 and decisions[0].llm_raw != ""
    assert decisions[0].strategy_version == round_row.prompt_md5
    # strategy_md5 为策略书原文 md5（区别于 strategy_version 的拼装 md5），两表同步落库
    expected_md5 = hashlib.md5("# 策略书\n稳健交易，控制回撤。".encode("utf-8")).hexdigest()
    assert decisions[0].strategy_md5 == expected_md5
    assert round_row.strategy_md5 == expected_md5
    # 每次 LLM 调用的 messages 都含首轮账户上下文，工具结果也会持续回填
    second_msgs = provider.calls[1]["messages"]
    assert any(
        m.get("role") == "user" and "权益(估值)" in m.get("content", "") for m in second_msgs
    )


# ---------- 价格预警线：重复设置去重 + 取消（内存唯一存储） ----------


async def test_price_alert_dedup_and_cancel(env: SimpleNamespace):
    """相同 (contract, direction, price) 重复设置不创建第二个；取消按同三元组精确删除。

    参数：
        env: SimpleNamespace，已组装的测试环境

    返回：
        None：通过断言校验目标场景，无返回值
    """
    provider = MockProvider(
        [
            _resp(
                "",
                [
                    ToolCall(
                        "set_price_alert",
                        {"contract": "BTC_USDT", "direction": "above", "price": 65000},
                        "c1",
                    ),
                    # 完全相同的重复设置：返回已存在提示，不重复创建
                    ToolCall(
                        "set_price_alert",
                        {"contract": "BTC_USDT", "direction": "above", "price": 65000},
                        "c2",
                    ),
                ],
                '{"turn":1}',
            ),
            _resp(
                "",
                [
                    ToolCall(
                        "cancel_price_alert",
                        {"contract": "BTC_USDT", "direction": "above", "price": 65000},
                        "c3",
                    ),
                    # 已取消的目标再次取消：如实回报未找到，不报错
                    ToolCall(
                        "cancel_price_alert",
                        {"contract": "BTC_USDT", "direction": "above", "price": 65000},
                        "c4",
                    ),
                ],
                '{"turn":2}',
            ),
            _resp("完成", [], '{"turn":3}'),
        ]
    )
    result = await _make_loop(env, provider).run_once("timer")

    assert result.ok
    calls = await env.repo.list_audit_tool_calls(result.round_id)
    texts = [json.loads(c.result_json)["text"] for c in calls]
    assert "已设置价格预警" in texts[0]
    assert "无需重复创建" in texts[1]
    assert "已取消价格预警" in texts[2]
    assert "未找到" in texts[3]
    # 取消后内存索引为空；重复设置自始至终未产生第二个实例
    assert env.triggers.list() == []


async def test_price_alert_rejects_over_max(env: SimpleNamespace):
    """预警线全局上限：已达 MAX_ALERTS 时工具拒绝创建并返回错误文本，总数不变。

    参数：
        env: SimpleNamespace，已组装的测试环境

    返回：
        None：通过断言校验目标场景，无返回值
    """
    for i in range(MAX_ALERTS):
        env.triggers.add("BTC_USDT", ">=", Decimal(70000 + i))
    provider = MockProvider(
        [
            _resp(
                "",
                [
                    ToolCall(
                        "set_price_alert",
                        {"contract": "ETH_USDT", "direction": "below", "price": 3000},
                        "c1",
                    ),
                ],
                '{"turn":1}',
            ),
            _resp("已达上限，放弃设置", [], '{"turn":2}'),
        ]
    )
    result = await _make_loop(env, provider).run_once("timer")

    assert result.ok
    calls = await env.repo.list_audit_tool_calls(result.round_id)
    text = json.loads(calls[0].result_json)["text"]
    assert "错误" in text and "上限" in text and "cancel_price_alert" in text
    assert len(env.triggers.list()) == MAX_ALERTS  # 未创建新预警
    assert env.triggers.find("ETH_USDT", "<=", Decimal("3000")) is None


# ---------- 风控拒绝：记录且不下单 ----------


async def test_risk_deny_recorded_no_order(env: SimpleNamespace):
    """验证超出单仓上限时风控拒单、记录原因且不产生订单。

    参数：
        env: SimpleNamespace，已组装的测试环境

    返回：
        None：通过断言校验目标场景，无返回值
    """
    provider = MockProvider(
        [
            _resp(
                "",
                [
                    ToolCall(
                        "place_order",
                        {
                            "contract": "BTC_USDT",
                            "size": 100,
                            "leverage": 1,
                            "stop_loss_price": 58000,
                        },
                        "c1",
                    )
                ],
                '{"turn":1}',
            ),  # 名义价值 6000 > 权益 30%（3000）
            _resp("风控拒绝，放弃开仓", [], '{"turn":2}'),
        ]
    )
    result = await _make_loop(env, provider).run_once("timer")

    assert result.ok and result.tool_calls == 1
    assert env.gateway.placed == []  # 未下单
    assert await env.repo.list_orders(result.round_id) == []
    calls = await env.repo.list_audit_tool_calls(result.round_id)
    assert calls[0].tool == "place_order" and calls[0].risk_verdict == "deny"
    assert "单仓" in calls[0].risk_reason
    assert "风控拒绝" in json.loads(calls[0].result_json)["text"]


# ---------- 解析失败：本轮不交易并记录 ----------


async def test_parse_failure_no_trade(env: SimpleNamespace):
    """验证 LLM 参数解析失败时不交易并完整记录失败决策轮。

    参数：
        env: SimpleNamespace，已组装的测试环境

    返回：
        None：通过断言校验目标场景，无返回值
    """
    provider = MockProvider([LLMParseError("工具参数不是合法 JSON")])
    loop = _make_loop(env, provider)
    result = await loop.run_once("price_alert")

    assert not result.ok and "LLMParseError" in result.error
    assert env.gateway.placed == []
    assert loop.consecutive_failures == 1
    round_row = await env.repo.get_audit_round(result.round_id)
    assert round_row is not None and "LLMParseError" in round_row.error
    assert round_row.ended_at is not None
    assert await env.repo.list_audit_tool_calls(result.round_id) == []
    decisions = await env.repo.list_decisions()  # 失败轮也落决策记录
    assert len(decisions) == 1 and decisions[0].wake_source == "price_alert"
    # 失败路径同样落策略书原文 md5，保证失败轮也能按版本关联
    expected_md5 = hashlib.md5("# 策略书\n稳健交易，控制回撤。".encode("utf-8")).hexdigest()
    assert decisions[0].strategy_md5 == expected_md5
    assert round_row.strategy_md5 == expected_md5


# ---------- 连续失败：触发风控锁 + 告警一次 ----------


async def test_consecutive_failures_lock_and_alert(tmp_path):
    """验证连续失败达到阈值后锁定风控且只发送一次告警。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path, max_failures=2)
    try:
        provider = MockProvider([LLMError("boom")] * 5)
        loop = _make_loop(env, provider)
        r1 = await loop.run_once("timer")
        assert not r1.ok and not env.settings.risk.kill_switch and env.alerts == []

        r2 = await loop.run_once("timer")
        assert not r2.ok and loop.consecutive_failures == 2
        assert env.settings.risk.kill_switch is True  # 风控锁置位
        assert loop.risk_locked is True
        assert len(env.alerts) == 1 and "连续失败 2 次" in env.alerts[0]

        r3 = await loop.run_once("timer")  # 继续失败不重复告警
        assert not r3.ok and loop.consecutive_failures == 3
        assert len(env.alerts) == 1
    finally:
        await env.db.close()


# ---------- 参数校验失败：返回错误文本，不中断本轮 ----------


async def test_invalid_args_return_error_text(env: SimpleNamespace):
    """验证缺参、非法枚举和未知工具返回错误文本但不判整轮失败。

    参数：
        env: SimpleNamespace，已组装的测试环境

    返回：
        None：通过断言校验目标场景，无返回值
    """
    provider = MockProvider(
        [
            _resp(
                "",
                [
                    ToolCall("place_order", {"size": 1}, "c1"),  # 缺 contract
                    ToolCall(
                        "set_price_alert",
                        {"contract": "BTC_USDT", "direction": "up", "price": 100},
                        "c2",
                    ),
                    ToolCall("fly_to_moon", {}, "c3"),  # 未知工具
                ],
                '{"turn":1}',
            ),
            _resp("收到错误，修正", [], '{"turn":2}'),
        ]
    )
    result = await _make_loop(env, provider).run_once("timer")

    assert result.ok  # 参数错误不算轮失败
    assert env.gateway.placed == []
    calls = await env.repo.list_audit_tool_calls(result.round_id)
    texts = [json.loads(c.result_json)["text"] for c in calls]
    assert "参数错误" in texts[0] and "参数错误" in texts[1]
    assert "未知工具" in texts[2]


# ---------- 已移除工具不可调用 ----------


async def test_removed_tools_are_not_registered(env: SimpleNamespace):
    """验证已移除的账户与杠杆工具既不可调用也不在注册表中。

    参数：
        env: SimpleNamespace，已组装的测试环境

    返回：
        None：通过断言校验目标场景，无返回值
    """
    provider = MockProvider(
        [
            _resp(
                "",
                [
                    ToolCall("set_leverage", {"contract": "BTC_USDT", "leverage": 3}, "c1"),
                    ToolCall("get_account", {}, "c2"),
                ],
                '{"turn":1}',
            ),
            _resp("完成", [], '{"turn":2}'),
        ]
    )
    result = await _make_loop(env, provider).run_once("timer")

    calls = await env.repo.list_audit_tool_calls(result.round_id)
    assert all("未知工具" in json.loads(call.result_json)["text"] for call in calls)
    loop = _make_loop(env, MockProvider([]))
    assert {spec.name for spec in loop._registry.specs}.isdisjoint({"get_account", "set_leverage"})


def test_schemas_and_handlers_stay_in_sync():
    """schema 与 handler 同名成对：schema 有 handler 无 → 工具对 LLM 静默缺失；
    handler 有 schema 无 → ToolRegistry 构造即 KeyError（反向由构造路径隐式覆盖）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert set(SCHEMAS) == set(_HANDLERS)


# ---------- PromptLoader 热重载 ----------


async def test_prompt_hot_reload(tmp_path):
    """验证策略文件修改后 PromptLoader 热加载新正文并更新摘要。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    path = tmp_path / "system_prompt.md"
    path.write_text("版本A", encoding="utf-8")
    loader = PromptLoader(path)
    spec = ToolSpec(
        "write_note", "留笔记", {"type": "object", "required": ["content"]}, lambda args: None
    )

    p1, md5_1 = loader.system_prompt([spec])
    assert "版本A" in p1 and "write_note" in p1 and "content" in p1

    path.write_text("版本B：加仓要慢", encoding="utf-8")
    mtime = path.stat().st_mtime
    os.utime(path, (mtime + 5, mtime + 5))  # 强制 mtime 变化（防文件系统精度问题）
    p2, md5_2 = loader.system_prompt([spec])
    assert "版本B" in p2 and "版本A" not in p2
    assert md5_1 != md5_2


# ---------- 指标短名单转发 ----------


async def test_indicator_shortlist_forwarded_to_context(env: SimpleNamespace):
    """indicator_shortlist 回调经 DecisionLoop 转发给 ContextBuilder：指标行随回调重读变化。

    参数：
        env: SimpleNamespace，已组装的测试环境

    返回：
        None：通过断言校验目标场景，无返回值
    """

    class _Svc:
        """指标服务 stub：只实现上下文行接口，回显收到的键列表（直观察觉短名单来源）。"""

        def shortlist_line(self, contract: str, interval: str, keys: list[str]) -> str:
            """把收到的合约、周期和指标键回显为上下文指标行。

            参数：
                contract: str，合约名称
                interval: str，时间周期
                keys: list[str]，指标键列表

            返回：
                str：包含合约、周期及逗号分隔指标键的文本
            """
            return f"{contract} 指标({interval}): 键={','.join(keys)}"

    keys = ["rsi14"]
    provider = MockProvider([_resp("观望一", [], "r1"), _resp("观望二", [], "r2")])
    loop = _make_loop(env, provider, indicator_service=_Svc(), indicator_shortlist=lambda: keys)
    await loop.run_once("timer")
    assert "键=rsi14" in provider.calls[0]["messages"][0]["content"]
    keys[:] = ["macd"]  # 每次构建重读回调：短名单修订后下一轮上下文即生效
    await loop.run_once("timer")
    assert "键=macd" in provider.calls[1]["messages"][0]["content"]
