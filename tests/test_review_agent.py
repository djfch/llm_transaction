"""src/review/agent.py 测试：自定义 StubProvider（实现 LLMProvider 协议）+ tmp_path SQLite。

覆盖：(a) 直接返文本 → 报告落库 action=none、审计轮 wake_source='review'、通知被调；
(b) 先调 get_review_stats 再 submit 再返文本 → 版本创建 + action=rewrite + attach + 工具审计行；
(c) stub 抛 LLMError → error 报告 + 告警 + 不抛；(d) provider None → 无审计无报告；
另：空文本兜底、set_provider 热替换、轮始/轮末 WS 事件序列（成功 ok=True / 失败 ok=False /
未配置零事件）、事件回调抛错容错（run 不受影响）。
"""

import json
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.agent.providers.base import LLMError, LLMResponse, ToolCall
from src.audit.trail import AuditTrail
from src.config import AuditConfig, Settings
from src.memory import Database, Repo
from src.review.agent import ReviewAgent
from src.review.indicator_config import IndicatorConfigStore
from src.review.prompts import ReviewPromptLoader
from src.review.strategy import StrategyStore

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
    assert round_row.ended_at is not None and round_row.error == ""
    assert len(env.alerts) == 1
    assert "策略未调整" in env.alerts[0] and len(env.alerts[0]) <= 500
    # WS 事件序列：轮始 → 轮末（成功 ok=True），round_id 与审计轮一致
    assert [e["type"] for e in env.events] == ["review_round_start", "review_round"]
    assert env.events[0]["data"] == {"round_id": result["round_id"]}
    assert env.events[1]["data"] == {"round_id": result["round_id"], "ok": True}
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
