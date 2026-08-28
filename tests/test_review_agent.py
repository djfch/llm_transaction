"""src/review/agent.py 测试：自定义 StubProvider（实现 LLMProvider 协议）+ tmp_path SQLite。

覆盖：(a) 直接返文本 → 报告落库 action=none、审计轮 wake_source='review'、通知被调；
(b) 先调 get_review_stats 再 submit 再返文本 → 版本创建 + action=rewrite + attach + 工具审计行；
(c) stub 抛 LLMError → error 报告 + 告警 + 不抛；(d) provider None → 无审计无报告；
另：空文本兜底、set_provider 热替换、轮始/轮末 WS 事件序列（成功 ok=True / 失败 ok=False /
未配置零事件）、事件回调抛错容错（run 不受影响）。
"""

import asyncio
import json
import time
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.agent.providers.base import LLMError, LLMParseError, LLMResponse, ToolCall
from src.agent.providers.retry import RetryingProvider
from src.audit.trail import AuditTrail
from src.config import AuditConfig, Settings
from src.memory import Database, Repo
from src.review.agent import ReviewAgent
from src.review.indicator_config import IndicatorConfigStore
from src.review.prompts import ReviewPromptLoader
from src.review.strategy import StrategyStore
from src.utils import LLMIdentity
from tests.research_helpers import save_report_fixture

_INIT = "初始策略书：" + "稳健交易，控制回撤。" * 10
_PERIOD = (1000.0, 2000.0)


class StubProvider:
    """按脚本回放响应的 stub（实现 LLMProvider 协议）：LLMResponse 返回、异常抛出。"""

    def __init__(self, script: list) -> None:
        """初始化测试替身并保存后续调用所需的预设数据。

        参数：
            script: list，按调用顺序消费的模拟响应脚本

        返回：
            None，初始化当前测试替身，无返回值
        """
        self._script = deque(script)
        self.chat_count = 0

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """按测试脚本顺序返回模拟的 LLM 响应或异常。

        参数：
            system: str，传给 LLM 的系统提示词
            messages: list[dict]，传给 LLM 的消息历史
            tools: list[dict]，传给 LLM 的工具定义

        返回：
            LLMResponse，脚本队首的预置模型响应；脚本耗尽时返回占位响应

        异常：
            Exception，脚本中的当前响应项是异常对象时原样抛出
        """
        self.chat_count += 1
        if not self._script:
            return LLMResponse(text="（脚本外响应）", raw="{}")
        item = self._script.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """把工具调用结果封装为模拟提供商消息。

        参数：
            call: ToolCall，待封装的工具调用
            result: str，工具执行结果文本

        返回：
            dict，包含 role=tool、call_id 与结果内容的工具消息
        """
        return {"role": "tool", "call_id": call.call_id, "content": result}


@pytest.fixture
async def env(tmp_path):
    """提供复盘 Agent 测试环境夹具。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        AsyncIterator[SimpleNamespace]，提供完整复盘测试环境并在结束后关闭数据库
    """
    db = Database()
    await db.open(tmp_path / "review.db")
    repo = Repo(db)
    prompt_file = tmp_path / "system_prompt.md"
    prompt_file.write_text(_INIT, encoding="utf-8")
    store = StrategyStore(prompt_file, repo)
    await store.seed_if_empty()  # v1
    review_prompt = tmp_path / "review_prompt.md"
    review_prompt.write_text("# 复盘纪律\n先统计后下钻。", encoding="utf-8")
    alerts: list[str] = []
    events: list[dict] = []  # 收集 notify_event 广播的 WS 事件
    yield SimpleNamespace(
        repo=repo,
        store=store,
        audit=AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit"))),
        loader=ReviewPromptLoader(review_prompt),
        alerts=alerts,
        events=events,
        settings=Settings(),
    )
    await db.close()


def _make_agent(env, provider, **kwargs) -> ReviewAgent:
    # 默认同步收集 WS 事件；用例可经 kwargs 覆盖注入（如抛错容错测试）
    """组装使用指定提供商的复盘 Agent。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        provider: LLMProvider | None，本轮使用的模拟 LLM 提供商
        **kwargs: dict[str, object]，按名称传入的可选参数

    返回：
        ReviewAgent，绑定测试仓储、审计、策略存储、提示词与通知回调的复盘 Agent
    """
    kwargs.setdefault("notify_event", lambda payload: env.events.append(payload))
    return ReviewAgent(
        settings=env.settings,
        provider=provider,
        repo=env.repo,
        audit=env.audit,
        store=env.store,
        prompt_loader=env.loader,
        on_alert=lambda msg: env.alerts.append(msg),  # 同步回调（maybe_await 消化）
        **kwargs,
    )


def _openai_tool_raw(name: str, arguments: str) -> str:
    """生成带单个工具调用的 OpenAI 兼容原始响应。

    参数：
        name: str，模型请求调用的工具名
        arguments: str，供应商原样返回的工具参数字符串

    返回：
        str，紧凑 JSON 格式的供应商响应
    """
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
                }
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


def _retrying_review_provider() -> RetryingProvider:
    """构造“拒绝工具 A、接受并执行工具 B、最终文本”的复盘重试脚本。

    参数：无

    返回：
        RetryingProvider，零退避且包含三段模型响应的测试提供商
    """
    rejected = LLMParseError(
        "工具参数不是合法 JSON", raw=_openai_tool_raw("get_round_context", "{坏 JSON")
    )
    accepted = LLMResponse(
        raw=_openai_tool_raw("get_review_stats", '{"start_ts":1000,"end_ts":2000}'),
        tool_calls=[
            ToolCall(
                name="get_review_stats",
                args={"start_ts": 1000, "end_ts": 2000},
                call_id="accepted-1",
            )
        ],
    )
    final = LLMResponse(
        text="最终复盘结论。",
        raw=json.dumps(
            {"choices": [{"message": {"content": "最终复盘结论。"}}]}, ensure_ascii=False
        ),
    )
    return RetryingProvider(StubProvider([rejected, accepted, final]), backoff=())


async def _seed_trades(repo: Repo) -> None:
    """区间内一笔平仓成交（join decisions），供预统计产生非空样本。

    参数：
        repo: Repo，连接测试数据库的仓储实例

    返回：
        None，执行上述模拟操作或副作用，无返回值
    """
    await repo.save_decision(round_id="r1", mode="paper", strategy_md5="m1")
    await repo.save_trade(
        "r1",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("8"),
        source="llm_close",
        created_at=1500.0,
    )


async def test_run_records_provider_identity(env):
    """复盘轮开轮即把 provider 的模型身份落入审计四列（跨模型效果对比的数据源）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，断言审计行四列与注入身份一致，无返回值
    """
    await _seed_trades(env.repo)
    provider = StubProvider([LLMResponse(text="# 复盘结论\n整体表现平稳。", raw="raw-1")])
    provider.identity = LLMIdentity(
        credential_name="kimi-main",
        provider="openai_responses",
        model="kimi-k2-thinking",
        thinking_effort="high",
    )
    result = await _make_agent(env, provider).run(*_PERIOD)

    assert result["ok"] is True
    row = await env.repo.get_audit_round(result["round_id"])
    assert row is not None
    assert (
        row.llm_credential_name,
        row.llm_provider,
        row.llm_model,
        row.llm_thinking_effort,
    ) == ("kimi-main", "openai_responses", "kimi-k2-thinking", "high")


async def test_run_success_without_revision(env):
    """(a) 直接返文本：报告落库 action=none、审计轮 wake_source='review'、通知被调。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，通过断言验证上述行为，无返回值
    """
    await _seed_trades(env.repo)
    provider = StubProvider([LLMResponse(text="# 复盘结论\n整体表现平稳。", raw="raw-1")])
    result = await _make_agent(env, provider).run(*_PERIOD)
    assert result["ok"] is True and result["strategy_action"] == "none"
    assert result.get("error_code") is None  # 成功路径不携带错误码
    assert result["new_version_id"] is None
    report = await env.repo.review.get_review_report(result["report_id"])
    assert report.report_md == "# 复盘结论\n整体表现平稳。"
    assert report.error == ""
    assert report.round_id == result["round_id"]  # 报告关联产生它的审计轮
    stats = json.loads(report.stats_json)  # 代码侧预统计已落 stats_json
    assert stats["close_count"] == 1 and stats["total_pnl"] == "8"
    round_row = await env.repo.get_audit_round(result["round_id"])
    assert round_row.wake_source == "review"
    assert round_row.context_snapshot.startswith("复盘区间：")
    assert "## 当前策略书全文" in round_row.context_snapshot
    assert _INIT in round_row.context_snapshot
    assert "## 区间预统计" in round_row.context_snapshot
    assert round_row.ended_at is not None and round_row.error == ""
    assert len(env.alerts) == 1
    assert "策略未调整" in env.alerts[0] and len(env.alerts[0]) <= 500
    # WS 事件序列：轮始 → 轮末（成功 ok=True），round_id 与审计轮一致
    assert [e["type"] for e in env.events] == ["review_round_start", "review_round"]
    assert env.events[0]["data"] == {"round_id": result["round_id"]}
    assert env.events[1]["data"] == {
        "round_id": result["round_id"],
        "ok": True,
        "applied": True,  # issue #100：生效结果随事件暴露
    }
    assert len(await env.repo.review.list_strategy_versions()) == 1  # 只有播种的 v1


async def test_run_empty_text_fallback(env):
    """LLM 最终文本为空 → 兜底「（复盘未产出报告）」，仍落库成功。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，通过断言验证上述行为，无返回值
    """
    provider = StubProvider([LLMResponse(text="", raw="{}")])
    result = await _make_agent(env, provider).run(*_PERIOD)
    assert result["ok"] is True
    report = await env.repo.review.get_review_report(result["report_id"])
    assert report.report_md == "（复盘未产出报告）"


async def test_run_with_strategy_revision(env):
    """(b) 先查统计再 submit 再返文本：版本创建 + action=rewrite + attach + 工具审计行。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，通过断言验证上述行为，无返回值
    """
    await _seed_trades(env.repo)
    new_prompt = "新策略书：" + "顺势加仓，严格止损。" * 10
    provider = StubProvider(
        [
            LLMResponse(
                text="",
                raw="raw-1",
                tool_calls=[
                    ToolCall(
                        name="get_review_stats",
                        args={"start_ts": 1000, "end_ts": 2000},
                        call_id="c1",
                    )
                ],
            ),
            LLMResponse(
                text="",
                raw="raw-2",
                tool_calls=[
                    ToolCall(
                        name="submit_strategy_revision",
                        args={"new_prompt_md": new_prompt, "reason": "收紧止损"},
                        call_id="c2",
                    )
                ],
            ),
            LLMResponse(text="已收紧止损纪律。", raw="raw-3"),
        ]
    )
    result = await _make_agent(env, provider).run(*_PERIOD)
    assert result["ok"] is True and result["strategy_action"] == "rewrite"
    assert provider.chat_count == 3
    version = await env.repo.review.get_strategy_version(result["new_version_id"])
    assert version.created_by == "review_agent"
    assert version.report_id == result["report_id"]  # attach_report_to_version 回填
    assert env.store.current() == new_prompt  # 策略书文件已原子替换
    report = await env.repo.review.get_review_report(result["report_id"])
    assert report.new_version_id == version.id
    calls = await env.repo.list_audit_tool_calls(result["round_id"])
    assert [c.tool for c in calls] == ["get_review_stats", "submit_strategy_revision"]
    assert all(c.risk_verdict == "" for c in calls)  # 复盘工具无风控判定
    assert f"策略已更新至 v{version.id}" in env.alerts[0]


async def test_retry_audit_separates_rejected_and_accepted_tool_calls(env):
    """解析失败后重试成功时，拒绝响应不冒领最终实际工具的审计结果。

    参数：
        env: SimpleNamespace，包含真实复盘 Agent、SQLite 审计与策略依赖

    返回：
        None，断言逐次响应状态、实际工具审计及最终报告均保持一致
    """
    result = await _make_agent(env, _retrying_review_provider()).run(*_PERIOD)

    assert result["ok"] is True
    round_row = await env.repo.get_audit_round(result["round_id"])
    attempts = [json.loads(line) for line in round_row.llm_raw.splitlines()]
    assert [item["status"] for item in attempts] == ["rejected", "accepted", "accepted"]
    assert (
        json.loads(attempts[0]["raw"])["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
        == "get_round_context"
    )
    calls = await env.repo.list_audit_tool_calls(result["round_id"])
    assert [call.tool for call in calls] == ["get_review_stats"]


async def test_run_llm_failure_lands_error_report(env):
    """(c) LLM 抛错：error 报告 + 审计轮 error + 失败告警，不向上抛。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，通过断言验证上述行为，无返回值
    """
    provider = StubProvider([LLMError("boom", raw='{"reasoning_content":"已收到"}')])
    result = await _make_agent(env, provider).run(*_PERIOD)
    assert result["ok"] is False and "LLMError: boom" in result["error"]
    report = await env.repo.review.get_review_report(result["report_id"])
    assert report.error == "LLMError: boom" and report.report_md == ""
    assert report.strategy_action == "none"
    assert report.round_id == result["round_id"]  # 失败轮同样关联审计轮（便于排查）
    round_row = await env.repo.get_audit_round(result["round_id"])
    assert round_row.error == "LLMError: boom"
    assert round_row.llm_raw == '{"reasoning_content":"已收到"}'
    assert len(env.alerts) == 1 and "复盘失败" in env.alerts[0]
    # 失败路径也发齐两条事件，尾部 review_round 带 ok=False
    assert [e["type"] for e in env.events] == ["review_round_start", "review_round"]
    assert env.events[-1]["data"] == {"round_id": result["round_id"], "ok": False}


async def test_run_survives_notify_event_failure(env):
    """notify_event 每次调用都抛错：_emit_event 容错生效，run 仍成功、报告正常落库。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，通过断言验证上述行为，无返回值
    """

    def _boom(payload: dict) -> None:
        """模拟依赖调用失败并抛出预设异常。

        参数：
            payload: dict，待发送的事件或 JSON 数据

        返回：
            None，执行上述模拟操作或副作用，无返回值

        异常：
            RuntimeError，模拟数据源或网络连接失败时抛出
        """
        raise RuntimeError("广播队列挂了")

    provider = StubProvider([LLMResponse(text="# 复盘结论\n事件失败无妨。", raw="raw-1")])
    result = await _make_agent(env, provider, notify_event=_boom).run(*_PERIOD)
    assert result["ok"] is True  # start/end 两次广播均抛错，不翻盘复盘结果
    report = await env.repo.review.get_review_report(result["report_id"])
    assert report.report_md == "# 复盘结论\n事件失败无妨。"
    assert report.error == ""
    assert len(await env.repo.review.list_strategy_versions()) == 1  # 未产生新版本


async def test_run_without_provider_no_audit_no_report(env):
    """(d) provider None：返回失败但不落审计、不落报告、不告警；error_code 结构化。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，通过断言验证上述行为，无返回值
    """
    result = await _make_agent(env, None).run(*_PERIOD)
    assert result["ok"] is False
    assert result["error_code"] == "llm_not_configured"  # 结构化错误码（路由据此映 503）
    assert result["error"]  # 文案非空即可，不锁定具体措辞
    assert await env.repo.review.latest_review_period_end() is None
    assert await env.repo.latest_audit_round("paper") is None
    assert env.alerts == []
    assert env.events == []  # LLM 未配置提前返回：零事件


async def test_set_provider_hot_swap(env):
    """set_provider 热替换：先 None 后注入，注入后即可正常复盘。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，通过断言验证上述行为，无返回值
    """
    agent = _make_agent(env, None)
    agent.set_provider(StubProvider([LLMResponse(text="热替换后报告", raw="{}")]))
    result = await agent.run(*_PERIOD)
    assert result["ok"] is True


async def test_run_with_indicator_config_revision(env, tmp_path):
    """指标短名单修订：轮末把报告 id 回填到指标配置版本（同策略版本关联模式，判空跳过）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    indicator_store = IndicatorConfigStore(
        tmp_path / "indicator_config.yaml", env.repo, valid_keys=frozenset({"ema20", "rsi14"})
    )
    provider = StubProvider(
        [
            LLMResponse(
                text="",
                raw="raw-1",
                tool_calls=[
                    ToolCall(
                        name="submit_indicator_config",
                        args={"shortlist": ["ema20"], "reason": "聚焦趋势指标"},
                        call_id="c1",
                    )
                ],
            ),
            LLMResponse(text="已聚焦趋势指标。", raw="raw-2"),
        ]
    )
    agent = _make_agent(env, provider, indicator_config_store=indicator_store)
    result = await agent.run(*_PERIOD)
    assert result["ok"] is True
    assert result["strategy_action"] == "none" and result["new_version_id"] is None  # 未动策略
    versions = await env.repo.indicator_config.list_versions()
    assert len(versions) == 1 and versions[0].created_by == "review_agent"
    assert versions[0].report_id == result["report_id"]  # 轮末 attach_report_to_version 回填


async def test_watchlist_hot_update_visible_to_review_agent(env, tmp_path):
    """复盘 agent 持有活名单引用：构造之后热加入的合约，轮内指标工具不再拦截（Codex P2 回归）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    from src.gateway.base import Candle
    from src.market.indicator_service import IndicatorService

    class _CandleCache:
        def get_recent(self, contract, interval, n):
            """返回测试替身中预设的最近 K 线。

            参数：
                contract: str，目标合约标识
                interval: str，K 线周期
                n: int，需要读取的 K 线数量

            返回：
                list[Candle]，固定生成的 60 根模拟 K 线
            """
            return [
                Candle(
                    t=1_700_000_000 + i * 3600,
                    o=Decimal(100 + i),
                    h=Decimal(101 + i),
                    l=Decimal(99 + i),
                    c=Decimal(100 + i),
                    v=Decimal(10),
                )
                for i in range(60)
            ]

    class _OiCache:
        def get(self, contract):
            """返回测试替身中指定合约的预设值。

            参数：
                contract: str，目标合约标识

            返回：
                Decimal，固定的模拟持仓量
            """
            return Decimal("12345")

    live_watchlist = ["BTC_USDT"]
    provider = StubProvider(
        [
            LLMResponse(
                text="",
                raw="raw-1",
                tool_calls=[
                    ToolCall(name="get_indicators", args={"contract": "ETH_USDT"}, call_id="c1")
                ],
            ),
            LLMResponse(text="看完新合约。", raw="raw-2"),
        ]
    )
    agent = _make_agent(
        env,
        provider,
        indicator_service=IndicatorService(_CandleCache(), _OiCache()),
        watchlist=live_watchlist,
    )
    live_watchlist.append("ETH_USDT")  # 启动后热更新（PUT /api/watchlist 原地改同一 list）
    result = await agent.run(*_PERIOD)
    assert result["ok"] is True
    calls = await env.repo.list_audit_tool_calls(result["round_id"])
    assert len(calls) == 1 and calls[0].tool == "get_indicators"
    assert "不在 watchlist" not in calls[0].result_json  # 活引用：新合约不再被拦截


class HangingProvider:
    """chat 挂起的 provider 桩（模拟复盘生成进行中，供外部取消回归）。"""

    def __init__(self) -> None:
        """初始化进入事件。

        参数：无

        返回：
            None，就地初始化 entered 事件，供测试等待 chat 真正开始
        """
        self.entered = asyncio.Event()

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """标记进入后挂起，直至任务被取消（永不正常返回）。

        参数：
            system: str，传给 LLM 的系统提示词
            messages: list[dict]，传给 LLM 的消息历史
            tools: list[dict]，传给 LLM 的工具定义

        返回：
            LLMResponse，永不返回；挂起只能被外部取消打断
        """
        self.entered.set()
        await asyncio.sleep(60)  # 远超测试等待，仅取消可打断

    def tool_result_message(self, call: ToolCall, result: str) -> dict:
        """把工具调用结果封装为模拟提供商消息（本桩不会走到）。

        参数：
            call: ToolCall，待封装的工具调用
            result: str，工具执行结果文本

        返回：
            dict，包含 role=tool、call_id 与结果内容的工具消息
        """
        return {"role": "tool", "call_id": call.call_id, "content": result}


async def test_run_external_cancel_lands_error_report_and_propagates(env):
    """外部取消（如停机 shutdown）：失败收尾三件套齐全，取消原样传播。

    断言：①error 报告落库；②审计轮 ended_at 非空且 error 含 CancelledError；
    ③事件序列以 review_round ok=False 收尾；task 以 CancelledError 结束。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，通过断言验证上述行为，无返回值
    """
    provider = HangingProvider()
    agent = _make_agent(env, provider)
    task = asyncio.create_task(agent.run(*_PERIOD))
    await asyncio.wait_for(provider.entered.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    assert total == 1
    assert reports[0].error == "CancelledError: 复盘被取消"
    round_row = await env.repo.latest_audit_round("paper")
    assert round_row is not None and round_row.ended_at is not None
    assert round_row.error == "CancelledError: 复盘被取消"
    # 轮始事件已发，尾部 review_round 带 ok=False（与 LLM 失败路径同构）
    assert [e["type"] for e in env.events] == ["review_round_start", "review_round"]
    assert env.events[-1]["data"] == {"round_id": round_row.round_id, "ok": False}


async def test_run_cancel_after_success_report_no_double_write(env, monkeypatch):
    """成功报告落库后、finalization 阶段被取消：禁止双写失败报告，审计以成功闭合且仅一次。

    在 end_round 处注入一次性取消（版本关联/end_round 窗口）：旧实现会经 _fail 再插
    失败报告并把审计轮以 error 闭合，同一 round 成功/失败双写。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        monkeypatch: pytest.MonkeyPatch，替换 AuditTrail.end_round 注入取消

    返回：
        None，断言仅一份成功报告、审计成功闭合一次、事件以 ok=True 收尾
    """
    provider = StubProvider([LLMResponse(text="# 复盘结论\n取消前已落库。", raw="raw-1")])
    agent = _make_agent(env, provider)
    state = {"cancelled": False, "closed": 0}
    real_end_round = AuditTrail.end_round

    async def cancelling_end_round(self, round_id, llm_raw, error=""):
        """首次调用向当前任务注入取消（真实闭合不发生），其后调用正常闭合。

        参数：
            self: AuditTrail，审计溯源实例
            round_id: str，待结束的审计轮次编号
            llm_raw: str | None，本轮 LLM 原始输出
            error: str，需要记录的错误文本

        返回：
            None：首次调用在 sleep 处被 CancelledError 打断，后续调用转真实闭合
        """
        if not state["cancelled"]:
            state["cancelled"] = True
            asyncio.current_task().cancel()
            await asyncio.sleep(0)  # 取消在此送达，真实闭合不发生
        state["closed"] += 1
        await real_end_round(self, round_id, llm_raw, error)

    monkeypatch.setattr(AuditTrail, "end_round", cancelling_end_round)
    task = asyncio.create_task(agent.run(*_PERIOD))
    with pytest.raises(asyncio.CancelledError):
        await task

    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    assert total == 1  # 同一 round 只有成功报告一份，无失败报告双写
    assert reports[0].error == ""
    assert reports[0].report_md == "# 复盘结论\n取消前已落库。"
    round_row = await env.repo.latest_audit_round("paper")
    assert round_row is not None and round_row.ended_at is not None
    assert round_row.error == ""  # 审计轮以成功语义闭合
    assert state["closed"] == 1  # 真实闭合仅发生一次（由取消分支补闭合）
    assert [e["type"] for e in env.events] == ["review_round_start", "review_round"]
    assert env.events[-1]["data"] == {
        "round_id": round_row.round_id,
        "ok": True,
        "applied": True,
    }


async def test_cancel_between_commit_and_return_rechecks_success(env, monkeypatch):
    """取消掐在「成功 INSERT/COMMIT 已执行、保存函数未返回」窗口：反查库识出已提交，禁止失败双写。

    monkeypatch save_review_report 为一次性 fake：先调真实方法真实落库、再抛
    CancelledError（模拟底层已提交但调用方收到取消、report_id 仍 None）。修复前
    该场景会经 _fail 再写一份失败报告；修复后按 round_id 反查库改走成功收尾
    （若修复正确 _fail 不会再被调用，fake 也不会再被触发）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        monkeypatch: pytest.MonkeyPatch，替换 save_review_bundle 注入落库后取消

    返回：
        None，断言取消传播、仅一份成功报告、审计成功闭合、ok=True 收尾、不告警
    """
    provider = StubProvider([LLMResponse(text="# 复盘结论\n提交成功但未返回。", raw="raw-1")])
    agent = _make_agent(env, provider)
    real_save = env.repo.review.save_review_bundle
    state = {"fired": False}

    async def committed_then_cancelled(*args, **kwargs):
        """首次调用真实落库成功后抛取消（模拟 COMMIT 已执行、调用方收到取消的窗口）。

        参数：
            args: tuple，save_review_bundle 的位置参数，原样透传真实方法
            kwargs: dict，save_review_bundle 的关键字参数，原样透传真实方法

        返回：
            ReviewReport：首次调用不返回（抛取消）；其后调用委托真实方法返回落库报告

        异常：
            asyncio.CancelledError：首次调用真实落库后抛出，模拟取消送达时机
        """
        if not state["fired"]:
            state["fired"] = True
            await real_save(*args, **kwargs)
            raise asyncio.CancelledError()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(env.repo.review, "save_review_bundle", committed_then_cancelled)
    task = asyncio.create_task(agent.run(*_PERIOD))
    with pytest.raises(asyncio.CancelledError):
        await task

    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    assert total == 1  # 成功报告已提交被反查识别：无失败报告双写
    assert reports[0].error == ""
    assert reports[0].report_md == "# 复盘结论\n提交成功但未返回。"
    round_row = await env.repo.latest_audit_round("paper")
    assert round_row is not None and round_row.ended_at is not None
    assert round_row.error == ""  # 审计轮以成功语义闭合
    assert [e["type"] for e in env.events] == ["review_round_start", "review_round"]
    assert env.events[-1]["data"] == {
        "round_id": round_row.round_id,
        "ok": True,
        "applied": True,
    }
    assert env.alerts == []  # 打断收尾路径既不发成功告警也不发失败告警


async def test_cancel_recheck_db_failure_falls_back_to_fail(env, monkeypatch, caplog):
    """取消收尾的反查自身失败（DB 抖动）：回落失败语义收尾，取消原样传播。

    与 test_cancel_between_commit_and_return_rechecks_success 同场景（成功 COMMIT 已执行、
    调用方收到取消），但 find_report_by_round_id 抛 RuntimeError：记日志后按「未提交」
    处理，走 _fail 落失败报告——此时库内成功+失败各一份（可接受退化，优于静默丢轮），
    日志须留下反查失败痕迹。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        monkeypatch: pytest.MonkeyPatch，注入落库后取消与反查失败
        caplog: pytest.LogCaptureFixture，捕获反查失败日志

    返回：
        None，断言取消传播、失败报告落库、反查失败日志留痕
    """
    provider = StubProvider([LLMResponse(text="# 复盘结论\n提交成功但未返回。", raw="raw-1")])
    agent = _make_agent(env, provider)
    real_save = env.repo.review.save_review_bundle
    state = {"fired": False}

    async def committed_then_cancelled(*args, **kwargs):
        """首次调用真实落库成功后抛取消（模拟 COMMIT 已执行、调用方收到取消的窗口）。

        参数：
            args: tuple，save_review_bundle 的位置参数，原样透传真实方法
            kwargs: dict，save_review_bundle 的关键字参数，原样透传真实方法

        返回：
            ReviewReport：首次调用不返回（抛取消）；其后调用委托真实方法返回落库报告

        异常：
            asyncio.CancelledError：首次调用真实落库后抛出，模拟取消送达时机
        """
        if not state["fired"]:
            state["fired"] = True
            await real_save(*args, **kwargs)
            raise asyncio.CancelledError()
        return await real_save(*args, **kwargs)

    async def broken_find(round_id: str):
        """模拟反查时数据库不可用。

        参数：
            round_id: str，待反查的审计轮次编号（本桩不使用）

        返回：
            None，永不返回；固定抛错

        异常：
            RuntimeError：模拟反查查询失败
        """
        raise RuntimeError("db gone")

    monkeypatch.setattr(env.repo.review, "save_review_bundle", committed_then_cancelled)
    monkeypatch.setattr(env.repo.review, "find_report_by_round_id", broken_find)
    task = asyncio.create_task(agent.run(*_PERIOD))
    with caplog.at_level("ERROR", logger="src.review.agent"), pytest.raises(asyncio.CancelledError):
        await task

    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    # 成功报告（fake 已提交）+ 失败报告（反查失败回落 _fail）各一份：可接受退化双写
    assert total == 2
    errors = {item.error for item in reports}
    assert "CancelledError: 复盘被取消" in errors  # 失败报告来自 _fail 回落
    assert "" in errors  # 成功报告仍在（反查失败不会抹掉已提交结果）
    assert "反查成功报告失败" in caplog.text  # 反查失败留痕


async def test_run_prompt_load_failure_lands_error_report(env, monkeypatch):
    """提示词加载抛错（begin_round 前的初始化步骤）：ok=False 且失败报告落库，不向上抛。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        monkeypatch: pytest.MonkeyPatch，破坏提示词加载

    返回：
        None，断言失败报告落库、未开审计轮、零事件
    """
    provider = StubProvider([LLMResponse(text="不会用到", raw="raw-1")])
    agent = _make_agent(env, provider)

    def _broken_prompt(tool_docs: str):
        """模拟提示词加载失败。

        参数：
            tool_docs: str，渲染后的工具说明（本桩不使用）

        返回：
            tuple[str, str]，永不返回；固定抛错

        异常：
            RuntimeError：模拟提示词文件损坏
        """
        raise RuntimeError("提示词文件损坏")

    monkeypatch.setattr(env.loader, "system_prompt", _broken_prompt)
    result = await agent.run(*_PERIOD)
    assert result["ok"] is False and "提示词文件损坏" in result["error"]
    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    assert total == 1
    assert reports[0].error == "RuntimeError: 提示词文件损坏"
    assert reports[0].round_id == ""  # begin_round 前失败：无审计轮关联
    assert await env.repo.latest_audit_round("paper") is None  # 未开审计轮
    assert env.events == []  # round_id 为空：零事件


# ---------- 成功落库后被打断的补全收尾（_complete_interrupted） ----------


def _dual_revision_provider() -> StubProvider:
    """构造「提交策略修订 → 提交指标修订 → 最终文本」的复盘脚本。

    参数：无

    返回：
        StubProvider，依次回放两个修订工具调用与最终文本的三段式 stub
    """
    new_prompt = "新策略书：" + "顺势加仓，严格止损。" * 10
    return StubProvider(
        [
            LLMResponse(
                text="",
                raw="raw-1",
                tool_calls=[
                    ToolCall(
                        name="submit_strategy_revision",
                        args={"new_prompt_md": new_prompt, "reason": "收紧止损"},
                        call_id="c1",
                    )
                ],
            ),
            LLMResponse(
                text="",
                raw="raw-2",
                tool_calls=[
                    ToolCall(
                        name="submit_indicator_config",
                        args={"shortlist": ["ema20"], "reason": "聚焦趋势"},
                        call_id="c2",
                    )
                ],
            ),
            LLMResponse(text="双修订完成。", raw="raw-3"),
        ]
    )


def _indicator_store(env, tmp_path) -> IndicatorConfigStore:
    """构造只认 ema20/rsi14 的指标短名单存储（供双修订脚本使用）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象（取其 repo 落版本）
        tmp_path: Path，pytest 提供的临时目录

    返回：
        IndicatorConfigStore，绑定临时配置文件、仓储与合法键集合的存储实例
    """
    return IndicatorConfigStore(
        tmp_path / "indicator_config.yaml", env.repo, valid_keys=frozenset({"ema20", "rsi14"})
    )


async def _assert_interrupted_success_cleanup(
    env, round_id: str | None = None, *, strategy_linked: bool = True
) -> None:
    """断言补全收尾的共同不变量：单份成功报告、版本回填、审计成功闭合、ok=True 收尾。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        round_id: str | None，预期审计轮次编号；None 时不校验编号本身
        strategy_linked: bool，策略版本是否应已回填 report_id（关联持续失败场景为 False）

    返回：
        None，通过断言验证上述行为，无返回值
    """
    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    assert total == 1  # 同一 round 只有成功报告一份，无失败报告双写
    assert reports[0].error == ""
    strategy_versions = await env.repo.review.list_strategy_versions()
    new_strategy = [v for v in strategy_versions if v.created_by == "review_agent"]
    assert len(new_strategy) == 1
    if strategy_linked:
        assert new_strategy[0].report_id == reports[0].id  # 策略版本关联已补回填
    else:
        assert new_strategy[0].report_id is None  # 关联持续失败：留空只记日志，不反转结果
    indicator_versions = await env.repo.indicator_config.list_versions()
    assert len(indicator_versions) == 1
    assert indicator_versions[0].report_id == reports[0].id  # 指标版本关联已补回填
    round_row = await env.repo.latest_audit_round("paper")
    assert round_row is not None and round_row.ended_at is not None
    assert round_row.error == ""  # 审计轮以成功语义闭合
    if round_id is not None:
        assert round_row.round_id == round_id
    assert [e["type"] for e in env.events] == ["review_round_start", "review_round"]
    assert env.events[-1]["data"] == {
        "round_id": round_row.round_id,
        "ok": True,
        "applied": True,
    }


async def test_cancel_at_strategy_attach_replays_completion(env, tmp_path, monkeypatch):
    """取消掐在策略版本 attach：补全收尾重放两个版本关联，审计成功闭合，取消原样传播。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，向策略版本 attach 注入一次性取消

    返回：
        None，断言取消传播且补全收尾副作用齐全
    """
    agent = _make_agent(
        env, _dual_revision_provider(), indicator_config_store=_indicator_store(env, tmp_path)
    )
    real_attach = env.repo.review.attach_report_to_version
    state = {"cancelled": False}

    async def cancelling_attach(version_id, report_id):
        """首次调用抛取消（真实关联不发生），其后调用转真实关联。

        参数：
            version_id: int，待关联的策略版本编号
            report_id: int，已落库成功报告的编号

        返回：
            None：后续调用委托真实 attach 完成关联

        异常：
            asyncio.CancelledError：首次调用时模拟外部取消
        """
        if not state["cancelled"]:
            state["cancelled"] = True
            raise asyncio.CancelledError()
        await real_attach(version_id, report_id)

    monkeypatch.setattr(env.repo.review, "attach_report_to_version", cancelling_attach)
    with pytest.raises(asyncio.CancelledError):
        await agent.run(*_PERIOD)
    await _assert_interrupted_success_cleanup(env)


async def test_cancel_at_indicator_attach_replays_completion(env, tmp_path, monkeypatch):
    """取消掐在指标版本 attach：策略关联已发生，补全收尾重放幂等关联后成功闭合。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，向指标版本 attach 注入一次性取消

    返回：
        None，断言取消传播且补全收尾副作用齐全
    """
    agent = _make_agent(
        env, _dual_revision_provider(), indicator_config_store=_indicator_store(env, tmp_path)
    )
    real_attach = env.repo.indicator_config.attach_report_to_version
    state = {"cancelled": False}

    async def cancelling_attach(version_id, report_id):
        """首次调用抛取消（真实关联不发生），其后调用转真实关联。

        参数：
            version_id: int，待关联的指标配置版本编号
            report_id: int，已落库成功报告的编号

        返回：
            None：后续调用委托真实 attach 完成关联

        异常：
            asyncio.CancelledError：首次调用时模拟外部取消
        """
        if not state["cancelled"]:
            state["cancelled"] = True
            raise asyncio.CancelledError()
        await real_attach(version_id, report_id)

    monkeypatch.setattr(env.repo.indicator_config, "attach_report_to_version", cancelling_attach)
    with pytest.raises(asyncio.CancelledError):
        await agent.run(*_PERIOD)
    await _assert_interrupted_success_cleanup(env)


async def test_attach_runtime_error_returns_success_without_double_write(
    env, tmp_path, monkeypatch
):
    """策略版本 attach 持续抛 RuntimeError：按成功语义补全返回 ok=True，禁止双写失败报告。

    普通异常与取消同口径：补全收尾中重放仍失败只记日志（策略版本 report_id 留空），
    指标版本关联补回放成功；不发成功告警、不落失败报告。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，向策略版本 attach 注入持续异常

    返回：
        None，断言成功返回、单份成功报告与审计成功闭合
    """
    agent = _make_agent(
        env, _dual_revision_provider(), indicator_config_store=_indicator_store(env, tmp_path)
    )

    async def broken_attach(version_id, report_id):
        """模拟版本关联持续失败。

        参数：
            version_id: int，待关联的策略版本编号
            report_id: int，已落库成功报告的编号

        返回：
            None，永不返回；固定抛错

        异常：
            RuntimeError：模拟数据库抖动导致关联失败
        """
        raise RuntimeError("关联写入抖动")

    monkeypatch.setattr(env.repo.review, "attach_report_to_version", broken_attach)
    result = await agent.run(*_PERIOD)
    assert result["ok"] is True and result["strategy_action"] == "rewrite"
    await _assert_interrupted_success_cleanup(env, result["round_id"], strategy_linked=False)
    assert env.alerts == []  # 打断收尾路径既不发成功告警也不发失败告警


async def test_snapshot_write_failure_is_non_fatal(env, monkeypatch):
    """审计 JSON 快照写盘失败（OSError）降级为日志：run 成功、审计轮成功闭合不改写。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        monkeypatch: pytest.MonkeyPatch，向 AuditTrail._write_snapshot 注入 OSError

    返回：
        None，断言 run 成功、单份成功报告、审计轮以成功闭合
    """

    async def broken_snapshot(self, round_id):
        """模拟快照写盘失败。

        参数：
            self: AuditTrail，审计溯源实例
            round_id: str，待写快照的审计轮次编号

        返回：
            None，永不返回；固定抛错

        异常：
            OSError：模拟磁盘满或权限不足
        """
        raise OSError("磁盘满")

    monkeypatch.setattr(AuditTrail, "_write_snapshot", broken_snapshot)
    provider = StubProvider([LLMResponse(text="# 复盘结论\n快照失败无妨。", raw="raw-1")])
    result = await _make_agent(env, provider).run(*_PERIOD)
    assert result["ok"] is True
    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    assert total == 1 and reports[0].error == ""
    round_row = await env.repo.get_audit_round(result["round_id"])
    assert round_row.ended_at is not None and round_row.error == ""  # 已提交结果不被反转


async def test_cancel_in_begin_round_commit_window_claims_round(env, monkeypatch):
    """取消掐在 begin_round「COMMIT 已执行、await 未返回」窗口：认领预分配轮，失败收尾正常闭合审计。

    monkeypatch begin_round 为 fake：先调真实方法真实建轮（COMMIT 已执行）、再抛
    CancelledError（模拟 await 未返回即被取消、局部 round_id 仍 ""）。修复前 _fail
    因 round_id 为空只落失败报告、不 end_round：审计轮 ended_at 永久为 null 且无
    轮末事件；修复后按预分配编号反查认领，失败报告 + end_round + ok=False 轮末事件
    齐全（begin_round 未正常返回，故无轮始事件）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        monkeypatch: pytest.MonkeyPatch，替换 AuditTrail.begin_round 注入提交后取消

    返回：
        None，断言取消传播、恰好一份失败报告、审计轮以取消错误闭合、轮末 ok=False 事件与失败告警
    """
    provider = StubProvider([LLMResponse(text="不会用到", raw="raw-1")])
    agent = _make_agent(env, provider)
    real_begin = AuditTrail.begin_round

    async def committed_then_cancelled_begin(*args, **kwargs):
        """真实建轮后抛取消（模拟 begin_round 内部 COMMIT 已执行、await 未返回的窗口）。

        参数：
            args: tuple，begin_round 的位置参数，原样透传真实方法
            kwargs: dict，begin_round 的关键字参数，原样透传真实方法

        返回：
            str：永不返回；固定抛取消

        异常：
            asyncio.CancelledError：真实建轮提交后抛出，模拟取消送达时机
        """
        await real_begin(*args, **kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(AuditTrail, "begin_round", committed_then_cancelled_begin)
    task = asyncio.create_task(agent.run(*_PERIOD, round_id="pre-review-1"))
    with pytest.raises(asyncio.CancelledError):
        await task

    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    assert total == 1  # 恰好一份失败报告（带预分配轮次编号），无重复落库
    assert reports[0].error == "CancelledError: 复盘被取消"
    assert reports[0].round_id == "pre-review-1"
    round_row = await env.repo.get_audit_round("pre-review-1")
    assert round_row is not None and round_row.ended_at is not None  # 认领后正常闭合
    assert round_row.error == "CancelledError: 复盘被取消"
    # begin_round 未返回故无轮始事件；轮末 ok=False 事件由认领后的 _fail 补发
    assert [e["type"] for e in env.events] == ["review_round"]
    assert env.events[0]["data"] == {"round_id": "pre-review-1", "ok": False}
    assert len(env.alerts) == 1 and "复盘失败" in env.alerts[0]  # 失败告警照常发送


async def test_save_post_commit_exception_recovers_success(env, monkeypatch):
    """成功报告 COMMIT 后、保存函数未返回时抛普通异常：反查识出已提交，按成功语义收尾不双写。

    monkeypatch save_review_report 为一次性 fake：先调真实方法真实落库、再抛
    RuntimeError（模拟 COMMIT 已执行、返回前失败、report_id 仍 None）。修复前
    except Exception 分支会经 _fail 再写一份失败报告；修复后与取消同口径反查，
    按成功语义补全收尾并返回成功结果。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        monkeypatch: pytest.MonkeyPatch，替换 save_review_bundle 注入落库后普通异常

    返回：
        None，断言恰好一份成功报告、审计成功闭合、返回成功语义结果、零告警
    """
    provider = StubProvider([LLMResponse(text="# 复盘结论\n提交成功但未返回。", raw="raw-1")])
    agent = _make_agent(env, provider)
    real_save = env.repo.review.save_review_bundle
    state = {"fired": False}

    async def committed_then_raise(*args, **kwargs):
        """首次调用真实落库成功后抛普通异常（模拟 COMMIT 已执行、保存函数未返回的窗口）。

        参数：
            args: tuple，save_review_bundle 的位置参数，原样透传真实方法
            kwargs: dict，save_review_bundle 的关键字参数，原样透传真实方法

        返回：
            ReviewReport：首次调用不返回（抛普通异常）；其后调用委托真实方法返回落库报告

        异常：
            RuntimeError：首次调用真实落库后抛出，模拟 post-commit 失败
        """
        if not state["fired"]:
            state["fired"] = True
            await real_save(*args, **kwargs)
            raise RuntimeError("post-commit failure")
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(env.repo.review, "save_review_bundle", committed_then_raise)
    result = await agent.run(*_PERIOD)

    assert result["ok"] is True
    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    assert total == 1  # 成功报告已提交被反查识别：无失败报告双写
    assert reports[0].error == ""
    assert reports[0].report_md == "# 复盘结论\n提交成功但未返回。"
    assert result["report_id"] == reports[0].id
    assert result["strategy_action"] == "none"
    assert result["new_version_id"] is None
    round_row = await env.repo.latest_audit_round("paper")
    assert round_row is not None and round_row.ended_at is not None
    assert round_row.error == ""  # 审计轮以成功语义闭合
    assert result["round_id"] == round_row.round_id
    assert [e["type"] for e in env.events] == ["review_round_start", "review_round"]
    assert env.events[-1]["data"] == {
        "round_id": round_row.round_id,
        "ok": True,
        "applied": True,
    }
    assert env.alerts == []  # 打断收尾路径既不发成功告警也不发失败告警


async def test_strategy_revision_is_draft_until_success(env):
    """草稿模式：工具调用不动文件，报告成功才生效；失败路径文件不变且草稿废弃（issue #62/#73）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，断言成功轮文件最终更新且版本 applied；工具调用后立即读文件仍为旧内容
    """
    await _seed_trades(env.repo)
    new_prompt = "草稿策略书：" + "顺势加仓，严格止损。" * 10
    old_content = env.store.current()
    provider = StubProvider(
        [
            LLMResponse(
                text="",
                raw="raw-1",
                tool_calls=[
                    ToolCall(
                        name="submit_strategy_revision",
                        args={"new_prompt_md": new_prompt, "reason": "测试草稿"},
                        call_id="c1",
                    )
                ],
            ),
            LLMResponse(text="完成。", raw="raw-2"),
        ]
    )
    agent = _make_agent(env, provider)
    # 工具执行后、报告落库前：文件必须仍是旧内容（先记账后生效）
    result = await agent.run(*_PERIOD)
    assert result["ok"] is True
    assert env.store.current() == new_prompt  # 成功提交后文件更新
    version = await env.repo.review.get_strategy_version(2)  # v1 为种子，v2 为本轮草稿
    assert version is not None and version.status == "applied"
    assert old_content != new_prompt


async def test_failed_round_discards_draft(env):
    """复盘失败时本轮草稿被废弃：文件不变、版本状态 discarded（issue #73）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，断言 provider 抛错后文件保持旧内容、草稿状态为 discarded
    """
    new_prompt = "不会生效的策略书：" + "顺势加仓，严格止损。" * 10

    def boom(*args, **kwargs):
        """模拟报告生成前 LLM 崩溃。"""
        raise RuntimeError("llm crashed")

    provider = StubProvider(
        [
            LLMResponse(
                text="",
                raw="raw-1",
                tool_calls=[
                    ToolCall(
                        name="submit_strategy_revision",
                        args={"new_prompt_md": new_prompt, "reason": "注定失败"},
                        call_id="c1",
                    )
                ],
            ),
        ]
    )
    agent = _make_agent(env, provider)
    original_chat = provider.chat
    calls = {"n": 0}

    async def chat_boom_late(system, messages, tools):
        """首轮正常返回工具调用，次轮直接崩溃（模拟报告前 LLM 故障）。"""
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("llm crashed")
        return await original_chat(system, messages, tools)

    agent._provider.chat = chat_boom_late
    old_content = env.store.current()
    result = await agent.run(*_PERIOD)
    assert result["ok"] is False
    assert env.store.current() == old_content  # 文件从未被动过
    drafts = [
        v for v in await env.repo.review.list_strategy_versions() if v.created_by == "review_agent"
    ]
    assert drafts and all(v.status == "discarded" for v in drafts)


async def test_apply_failure_alerts_and_marks_not_applied(env):
    """草稿生效失败：review_round 事件 applied=false 且发 TG 告警（issue #102）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，断言 apply 抛错时事件带 applied=False、告警含「复盘告警」且报告仍成功
    """
    await _seed_trades(env.repo)
    new_prompt = "生效失败策略书：" + "顺势加仓，严格止损。" * 10
    provider = StubProvider(
        [
            LLMResponse(
                text="",
                raw="raw-1",
                tool_calls=[
                    ToolCall(
                        name="submit_strategy_revision",
                        args={"new_prompt_md": new_prompt, "reason": "注定生效失败"},
                        call_id="c1",
                    )
                ],
            ),
            LLMResponse(text="完成。", raw="raw-2"),
        ]
    )
    agent = _make_agent(env, provider)

    async def failing_apply(version_id: int):
        """模拟磁盘满等持久性生效失败。"""
        raise RuntimeError("disk full")

    env.store.apply_version = failing_apply
    result = await agent.run(*_PERIOD)
    assert result["ok"] is True  # 报告按成功语义落库
    final_event = env.events[-1]["data"]
    assert final_event["applied"] is False  # 生效失败随事件暴露
    assert any("复盘告警" in a and "未生效" in a for a in env.alerts)  # TG 告警已发


async def test_run_research_review_end_to_end(env):
    """研报复盘全流程集成：读案例 → 提交批改 → 随复盘报告单事务落库并附代码统计段。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象（无 candle_source，客观结果降级 unavailable）

    返回：
        None，断言研报复盘落库关联、统计段注入与简报引导文案
    """
    report = await save_report_fixture(
        env.repo,
        report_type="us_open",
        contract="BTC_USDT",
        direction="偏多",
        confidence="中",
        horizon="当日",
        narrative="结构向上。",
        evidence_json=json.dumps([{"point": "美联储转鸽", "source": "金十"}], ensure_ascii=False),
    )
    ts = time.time() - 25 * 3600  # 回拨创建时间使 horizon=当日窗口到期
    await env.repo._conn.execute(
        "UPDATE research_reports SET created_at=? WHERE id=?", (ts, report.id)
    )
    await env.repo._conn.execute(
        "UPDATE research_asset_views SET created_at=? WHERE report_id=?", (ts, report.id)
    )
    await env.repo._conn.commit()

    provider = StubProvider(
        [
            LLMResponse(
                text="",
                raw="raw-1",
                tool_calls=[
                    ToolCall(
                        name="get_research_review_case",
                        args={"report_id": report.id, "contract": "BTC_USDT"},
                        call_id="c1",
                    )
                ],
            ),
            LLMResponse(
                text="",
                raw="raw-2",
                tool_calls=[
                    ToolCall(
                        name="submit_research_review",
                        args={
                            "report_id": report.id,
                            "contract": "BTC_USDT",
                            "direction_relation": "realized",
                            "direction_reason": "窗口内上行",
                            "reasoning_quality": "sound",
                            "reasoning_review": "推理链完整",
                            "evidence_reviews": [
                                {
                                    "evidence_index": 0,
                                    "fact_status": "confirmed",
                                    "reasoning_status": "supported",
                                    "explanation": "金十快讯核对：依据成立",
                                }
                            ],
                            "confidence_assessment": "appropriate",
                            "confidence_reason": "置信度合理",
                            "improvement_advice": "无",
                        },
                        call_id="c2",
                    )
                ],
            ),
            LLMResponse(text="# 复盘结论\n本轮含研报复盘。", raw="raw-3"),
        ]
    )
    agent = _make_agent(env, provider)
    result = await agent.run(*_PERIOD)

    assert result["ok"] is True
    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    assert total == 1
    assert "## 研报复盘统计" in reports[0].report_md  # 代码确定性统计段已追加
    assert "批改条数：1" in reports[0].report_md

    reviews = await env.repo.research_review.list_reviews()
    assert len(reviews) == 1
    assert reviews[0].review_report_id == reports[0].id  # 与复盘报告同事务关联
    assert reviews[0].report_id == report.id
    assert reviews[0].contract == "BTC_USDT"
    outcome = json.loads(reviews[0].outcome_json)
    assert outcome["data_status"] == "unavailable"  # 未装配 K 线来源时降级

    round_row = await env.repo.latest_audit_round("paper")
    assert round_row is not None
    assert "研报复盘" in (round_row.context_snapshot or "")  # 简报含研报复盘工作引导


# ---------- 研报提示词草稿流（issue #113 C6） ----------


def _research_prompt_store(env, tmp_path):
    """构造已播种 v1 的研报提示词版本存储（供复盘 agent 装配）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象（取其 repo 落版本）
        tmp_path: Path，pytest 提供的临时目录

    返回：
        ResearchPromptStore，绑定临时提示词文件与仓储的存储实例（已完成 v1 播种）
    """
    from src.research.prompt_store import ResearchPromptStore

    path = tmp_path / "research_prompt.md"
    path.write_text("初始研报提示词：" + "先事实后判断。" * 20, encoding="utf-8")
    store = ResearchPromptStore(path, env.repo)
    return store


def _prompt_revision_provider(new_prompt: str) -> StubProvider:
    """构造「提交研报提示词修订 → 最终文本」的复盘脚本。

    参数：
        new_prompt: str，本轮要提交的研报提示词新全文

    返回：
        StubProvider，依次回放修订工具调用与最终文本的两段式 stub
    """
    return StubProvider(
        [
            LLMResponse(
                text="",
                raw="raw-1",
                tool_calls=[
                    ToolCall(
                        name="submit_research_prompt_revision",
                        args={"new_prompt_md": new_prompt, "reason": "研报复盘修订"},
                        call_id="c1",
                    )
                ],
            ),
            LLMResponse(text="完成。", raw="raw-2"),
        ]
    )


async def test_research_prompt_revision_applied_on_success(env, tmp_path):
    """研报提示词草稿随报告成功生效：文件更新、版本 applied、关联复盘报告 id。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言文件/状态/关联三件套
    """
    store = _research_prompt_store(env, tmp_path)
    await store.seed_if_empty()  # v1
    new_prompt = "修订后研报提示词：" + "逐条核对证据，先找反对材料。" * 10
    agent = _make_agent(env, _prompt_revision_provider(new_prompt), research_prompt_store=store)
    result = await agent.run(*_PERIOD)
    assert result["ok"] is True
    assert store.current() == new_prompt  # 成功提交后文件更新
    version = await env.repo.research_prompt.get_version(2)  # v1 种子，v2 本轮草稿
    assert version is not None and version.status == "applied"
    assert version.created_by == "review_agent"
    reports, _ = await env.repo.review.list_review_reports_page(10, 0)
    assert version.review_report_id == reports[0].id  # 版本↔报告关联已回填


async def test_failed_round_discards_research_prompt_draft(env, tmp_path):
    """复盘失败时研报提示词草稿被废弃：文件不变、版本状态 discarded。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言 provider 崩溃后文件保持旧内容、草稿状态为 discarded
    """
    store = _research_prompt_store(env, tmp_path)
    await store.seed_if_empty()
    new_prompt = "不会生效的提示词：" + "注定失败。" * 30
    provider = _prompt_revision_provider(new_prompt)
    agent = _make_agent(env, provider, research_prompt_store=store)
    original_chat = provider.chat
    calls = {"n": 0}

    async def chat_boom_late(system, messages, tools):
        """首轮正常返回工具调用，次轮直接崩溃（模拟报告前 LLM 故障）。"""
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("llm crashed")
        return await original_chat(system, messages, tools)

    agent._provider.chat = chat_boom_late
    result = await agent.run(*_PERIOD)
    assert result["ok"] is False
    assert store.current().startswith("初始研报提示词")  # 文件从未被动过
    drafts = [
        v for v in await env.repo.research_prompt.list_versions() if v.created_by == "review_agent"
    ]
    assert drafts and all(v.status == "discarded" for v in drafts)


async def test_cancel_at_prompt_attach_replays_completion(env, tmp_path, monkeypatch):
    """取消掐在研报提示词版本 attach：补全收尾重放幂等关联，审计成功闭合，取消原样传播。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，向研报提示词版本 attach 注入一次性取消

    返回：
        None，断言取消传播且补全收尾副作用齐全
    """
    store = _research_prompt_store(env, tmp_path)
    await store.seed_if_empty()
    new_prompt = "修订后研报提示词：" + "逐条核对证据。" * 20
    agent = _make_agent(env, _prompt_revision_provider(new_prompt), research_prompt_store=store)
    real_attach = env.repo.research_prompt.attach_report_to_version
    state = {"cancelled": False}

    async def cancelling_attach(version_id, report_id):
        """首次调用抛取消（真实关联不发生），其后调用转真实关联。

        参数：
            version_id: int，待关联的研报提示词版本编号
            report_id: int，已落库成功报告的编号

        返回：
            None：后续调用委托真实 attach 完成关联

        异常：
            asyncio.CancelledError：首次调用时模拟外部取消
        """
        if not state["cancelled"]:
            state["cancelled"] = True
            raise asyncio.CancelledError()
        await real_attach(version_id, report_id)

    monkeypatch.setattr(env.repo.research_prompt, "attach_report_to_version", cancelling_attach)
    with pytest.raises(asyncio.CancelledError):
        await agent.run(*_PERIOD)
    # 补全收尾：单份成功报告、草稿已生效并补回填关联、审计成功闭合、ok=True 轮末事件
    reports, total = await env.repo.review.list_review_reports_page(10, 0)
    assert total == 1 and reports[0].error == ""
    version = await env.repo.research_prompt.get_version(2)
    assert version is not None and version.status == "applied"
    assert version.review_report_id == reports[0].id
    assert store.current() == new_prompt
    round_row = await env.repo.latest_audit_round("paper")
    assert round_row is not None and round_row.ended_at is not None and round_row.error == ""
    assert [e["type"] for e in env.events] == ["review_round_start", "review_round"]
    assert env.events[-1]["data"]["ok"] is True
    assert env.alerts == []  # 打断收尾路径既不发成功告警也不发失败告警


async def test_research_prompt_tools_degrade_in_agent_loop(env):
    """未装配 store 时 agent 轮内调用两个工具只收降级提示，复盘正常完成。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        None，断言复盘成功且版本表为空
    """
    provider = StubProvider(
        [
            LLMResponse(
                text="",
                raw="raw-1",
                tool_calls=[
                    ToolCall(
                        name="submit_research_prompt_revision",
                        args={"new_prompt_md": "x" * 200, "reason": "未装配场景"},
                        call_id="c1",
                    )
                ],
            ),
            LLMResponse(text="完成。", raw="raw-2"),
        ]
    )
    agent = _make_agent(env, provider)  # 不传 research_prompt_store
    result = await agent.run(*_PERIOD)
    assert result["ok"] is True
    assert await env.repo.research_prompt.list_versions() == []
