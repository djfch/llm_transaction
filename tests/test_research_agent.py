"""研报 agent 测试：完整一轮（预注入→工具循环→JSON 解析→落库→审计）。

provider 用确定性 Mock（不触网）；数据源用假实现；审计落 tmp_path。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.audit.logger import get_logger  # noqa: F401  （确保日志初始化）
from src.audit.trail import AuditTrail
from src.config import AuditConfig, Settings
from src.memory import Database, Repo
from src.research.agent import ResearchAgent
from src.research.payload import _parse_payload
from src.research.providers.base import (
    CalendarEvent,
    FlashItem,
    ResearchDataProvider,
)
from src.research.providers.blockbeats import BlockbeatsSource  # noqa: F401
from src.research.providers.jin10 import BEIJING_TZ
from src.research.prompts import ResearchPromptLoader

logger = get_logger(__name__)

GOOD_JSON = json.dumps(
    {
        "direction": "偏多",
        "confidence": "高",
        "horizon": "当日",
        "evidence": [{"point": "ETF 连续流入", "source": "指标快照"}],
        "risks": ["高利率"],
        "narrative": "流动性宽松，看多",
    },
    ensure_ascii=False,
)


class _FakeJin10:
    async def fetch_calendar(self):
        # 事件日期按北京时区动态生成（复审 #6 配套）：写死日期跨天后即过期
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        return [
            CalendarEvent(
                title="非农",
                pub_time=f"{today} 20:30",
                star=5,
                actual="",
                consensus="8.3",
                previous="5.7",
                affect_txt="未公布",
            )
        ]

    async def fetch_flash(self, hours=24):
        return [
            FlashItem(
                id="j1",
                source="jin10",
                title="金十新闻",
                summary="摘要",
                detail="全文",
                url="",
                published_at=1000.0,
            )
        ]

    async def fetch_article_detail(self, item_id):
        return "详情"

    async def search_news(self, keyword, limit=20):
        return []


class _FakeBb:
    async def fetch_flash(self, hours=24):
        return []

    async def fetch_indicators(self):
        return "## BTC ETF 净流入\n+2.1 亿"

    async def search_news(self, keyword, limit=20):
        return []


class _SequentialProvider:
    """确定性 Mock：第一轮调 2 个工具，第二轮直接输出 JSON。"""

    def __init__(self) -> None:
        self._calls = 0

    async def chat(self, system: str, messages: list[dict], tools: list[dict]):
        from src.agent.providers.base import LLMResponse, ToolCall

        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                text="先看日历与指标",
                tool_calls=[
                    ToolCall("fetch_calendar", {}),
                    ToolCall("fetch_indicators", {}),
                ],
                raw=f"raw-{self._calls}",
                assistant_message={"role": "assistant", "content": "工具轮"},
            )
        return LLMResponse(
            text=GOOD_JSON,
            raw=f"raw-{self._calls}",
            assistant_message={"role": "assistant", "content": GOOD_JSON},
        )

    def tool_result_message(self, call, result: str) -> dict:
        return {"role": "user", "content": f"工具 {call.name} 结果：{result}"}


class _BadJsonProvider(_SequentialProvider):
    """持续输出非法 JSON（同上下文重发 3 次后仍失败，落 error 报告）。"""

    def __init__(self) -> None:
        super().__init__()
        self._bad = True

    async def chat(self, system: str, messages: list[dict], tools: list[dict]):
        from src.agent.providers.base import LLMResponse

        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                text="不是JSON",
                raw=f"raw-{self._calls}",
                assistant_message={"role": "assistant", "content": "不是JSON"},
            )
        return LLMResponse(
            text="还是不是JSON",
            raw=f"raw-{self._calls}",
            assistant_message={"role": "assistant", "content": "还是不是JSON"},
        )


@pytest.fixture
async def repo(tmp_path) -> Repo:
    db = Database()
    await db.open(tmp_path / "research.db")
    return Repo(db)


@pytest.fixture
def settings() -> Settings:
    return Settings(mode="paper")


async def _build_agent(
    repo: Repo, settings: Settings, provider, tmp_path, notify_event=None
) -> ResearchAgent:
    audit = AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit")))
    data = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    return ResearchAgent(
        settings=settings,
        provider=provider,
        repo=repo,
        audit=audit,
        prompt_loader=ResearchPromptLoader(tmp_path / "research_prompt.md"),
        data_provider=data,
        notify_event=notify_event,
        max_turns=10,
        timeout_seconds=60,
    )


async def test_full_round_success(repo: Repo, settings: Settings, tmp_path) -> None:
    """完整一轮：工具循环→JSON 落库→审计轮完整。"""
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
    assert result["direction"] == "偏多" and result["confidence"] == "高"
    report = await repo.research.latest_report()
    assert report is not None
    assert report.direction == "偏多"
    assert report.report_type == "us"
    # 审计轮完整：1 轮 + 2 次工具调用
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None
    assert audit_round.wake_source == "research"
    assert audit_round.error == ""
    calls = await repo.list_audit_tool_calls(result["round_id"])
    assert [c.tool for c in calls] == ["fetch_calendar", "fetch_indicators"]


async def test_bad_json_falls_to_error_report(repo: Repo, settings: Settings, tmp_path) -> None:
    """输出非法 JSON、同上下文重发 3 次仍失败：落 error 报告 + 审计轮 error，返回 ok=False。"""
    provider = _BadJsonProvider()
    agent = await _build_agent(repo, settings, provider, tmp_path)
    result = await agent.run(report_type="asia")
    assert result["ok"] is False
    assert "解析失败" in result["error"]
    assert provider._calls == 3  # 初始输出 + 2 次同参重发，累计 3 次输出才判失败
    report = await repo.research.latest_report(include_error=True)
    assert report is not None and report.error != ""
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None and audit_round.error != ""


async def test_no_provider_returns_failure(repo: Repo, settings: Settings, tmp_path) -> None:
    """LLM 未配置：直接失败，不落审计、不落研报。"""
    agent = await _build_agent(repo, settings, None, tmp_path)
    result = await agent.run()
    assert result["ok"] is False and result["error_code"] == "llm_not_configured"
    assert await repo.research.latest_report() is None


def test_parse_payload_edge_cases() -> None:
    """JSON 解析容错：代码块包裹、缺字段、非法取值、合法。"""
    assert _parse_payload(f"```json\n{GOOD_JSON}\n```")["direction"] == "偏多"
    assert _parse_payload('{"direction": "偏多"}') is None  # 缺 confidence
    assert _parse_payload('{"direction": "大涨", "confidence": "高"}') is None  # 非法取值
    assert _parse_payload("not json") is None
    assert _parse_payload(GOOD_JSON)["confidence"] == "高"
    # L6 回归：evidence/risks 非 list 必须拒绝（触发重试规范化）
    assert _parse_payload('{"direction": "偏多", "confidence": "高", "evidence": "字符串"}') is None
    assert _parse_payload('{"direction": "偏多", "confidence": "高", "risks": 5}') is None


async def test_tool_call_failure_does_not_abort(repo: Repo, settings: Settings, tmp_path) -> None:
    """工具执行失败（未知工具）不中断循环，最终仍产出研报。"""

    class _ToolErrorProvider(_SequentialProvider):
        async def chat(self, system: str, messages: list[dict], tools: list[dict]):
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            if self._calls == 1:
                return LLMResponse(
                    text="调个不存在的工具",
                    tool_calls=[ToolCall("not_a_tool", {})],
                    raw=f"raw-{self._calls}",
                    assistant_message={"role": "assistant", "content": "工具轮"},
                )
            return LLMResponse(
                text=GOOD_JSON,
                raw=f"raw-{self._calls}",
                assistant_message={"role": "assistant", "content": GOOD_JSON},
            )

    agent = await _build_agent(repo, settings, _ToolErrorProvider(), tmp_path)
    result = await agent.run()
    assert result["ok"] is True  # 未知工具返回错误文本，LLM 继续


# ---------- 审查补齐：T2-T5 ----------


async def test_provider_chat_raises_falls_to_error(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """T2：provider.chat 抛异常 → 落 error 报告 + 审计轮 error，返回 ok=False。"""

    class _RaisingProvider:
        async def chat(self, system, messages, tools):
            raise RuntimeError("LLM 服务不可用")

        def tool_result_message(self, call, result):
            return {"role": "user", "content": "x"}

    agent = await _build_agent(repo, settings, _RaisingProvider(), tmp_path)
    result = await agent.run()
    assert result["ok"] is False and "RuntimeError" in result["error"]
    report = await repo.research.latest_report(include_error=True)
    assert report is not None and report.error != ""
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None and audit_round.error != ""


async def test_json_retry_success_path(repo: Repo, settings: Settings, tmp_path) -> None:
    """T3：首次输出坏 JSON、同上下文重发后合法 → ok=True（重试成功路径）。"""

    class _RetryProvider(_SequentialProvider):
        def __init__(self) -> None:
            super().__init__()
            self._retry_called = False
            self.seen: list[list[dict]] = []  # 每次调用收到的 messages 快照

        async def chat(self, system, messages, tools):
            from src.agent.providers.base import LLMResponse

            self._calls += 1
            self.seen.append([dict(m) for m in messages])
            if self._calls == 1:
                return LLMResponse(
                    text="坏JSON",
                    raw="raw1",
                    assistant_message={"role": "assistant", "content": "坏JSON"},
                )
            self._retry_called = True
            return LLMResponse(
                text=GOOD_JSON,
                raw="raw2",
                assistant_message={"role": "assistant", "content": GOOD_JSON},
            )

    provider = _RetryProvider()
    agent = await _build_agent(repo, settings, provider, tmp_path)
    result = await agent.run()
    assert result["ok"] is True and result["direction"] == "偏多"
    assert provider._retry_called  # 确实走了重试
    # 同上下文重发：重试请求与产生坏输出的请求一致，未追加纠错/失败内容
    assert provider._calls == 2
    assert provider.seen[1] == provider.seen[0]


async def test_json_retry_third_attempt_success(repo: Repo, settings: Settings, tmp_path) -> None:
    """坏 JSON 连续 2 次、第 3 次输出合法 → ok=True；三次请求上下文完全一致。"""

    class _ThirdTimeProvider(_SequentialProvider):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[list[dict]] = []

        async def chat(self, system, messages, tools):
            from src.agent.providers.base import LLMResponse

            self._calls += 1
            self.seen.append([dict(m) for m in messages])
            if self._calls <= 2:
                return LLMResponse(
                    text=f"坏JSON{self._calls}",
                    raw=f"raw-{self._calls}",
                    assistant_message={"role": "assistant", "content": "坏"},
                )
            return LLMResponse(
                text=GOOD_JSON,
                raw="raw-3",
                assistant_message={"role": "assistant", "content": GOOD_JSON},
            )

    provider = _ThirdTimeProvider()
    agent = await _build_agent(repo, settings, provider, tmp_path)
    result = await agent.run()
    assert result["ok"] is True and result["direction"] == "偏多"
    assert provider._calls == 3  # 初始 + 2 次同参重发
    assert provider.seen[0] == provider.seen[1] == provider.seen[2]


async def test_reask_uses_full_context_after_tool_round(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """工具轮后产出坏 JSON：重发上下文为 [user, assistant, 工具结果] 全序列，坏输出不回灌。"""

    class _ToolThenBadJson(_SequentialProvider):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[list[dict]] = []

        async def chat(self, system, messages, tools):
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            self.seen.append([dict(m) for m in messages])
            if self._calls == 1:
                return LLMResponse(
                    text="先查日历",
                    tool_calls=[ToolCall("fetch_calendar", {})],
                    raw="raw-1",
                    assistant_message={"role": "assistant", "content": "工具轮"},
                )
            if self._calls == 2:
                return LLMResponse(
                    text="坏JSON",
                    raw="raw-2",
                    assistant_message={"role": "assistant", "content": "坏JSON"},
                )
            return LLMResponse(
                text=GOOD_JSON,
                raw="raw-3",
                assistant_message={"role": "assistant", "content": GOOD_JSON},
            )

    provider = _ToolThenBadJson()
    agent = await _build_agent(repo, settings, provider, tmp_path)
    result = await agent.run()
    assert result["ok"] is True
    assert provider._calls == 3
    # 重发（第 3 次调用）请求与产生坏 JSON（第 2 次调用）的请求上下文完全一致：
    # [user 预注入, assistant 工具轮, 工具结果] 全序列，坏 JSON 文本本身未回灌
    assert provider.seen[2] == provider.seen[1]
    assert len(provider.seen[2]) == 3
    assert provider.seen[2][0]["role"] == "user"
    assert provider.seen[2][1] == {"role": "assistant", "content": "工具轮"}
    assert "坏JSON" not in str(provider.seen[2])


async def test_parse_retry_phase_under_timeout_fuse(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """JSON 重试阶段同受超时保险丝约束：重发卡死 → TimeoutError 落 error 报告。"""

    class _BadThenHang:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, system, messages, tools):
            from src.agent.providers.base import LLMResponse

            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    text="坏JSON",
                    raw="raw-1",
                    assistant_message={"role": "assistant", "content": "坏JSON"},
                )
            import asyncio

            await asyncio.sleep(60)  # 重发请求卡死，远超 2s 保险丝

        def tool_result_message(self, call, result):
            return {"role": "user", "content": "x"}

    provider = _BadThenHang()
    agent = await _build_agent(repo, settings, provider, tmp_path)
    agent._timeout = 2  # 压到 2 秒（构造参数已过，直接改实例属性）
    result = await agent.run()
    assert result["ok"] is False and "TimeoutError" in result["error"]
    report = await repo.research.latest_report(include_error=True)
    assert report is not None and report.error != ""


async def test_timeout_terminates_round(repo: Repo, settings: Settings, tmp_path) -> None:
    """T4：超时强制终止 → 落 error 报告，审计轮收尾。"""

    class _SlowProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, system, messages, tools):
            self.calls += 1
            import asyncio

            await asyncio.sleep(60)  # 远超 2s 超时

        def tool_result_message(self, call, result):
            return {"role": "user", "content": "x"}

    provider = _SlowProvider()
    agent = await _build_agent(repo, settings, provider, tmp_path)
    agent._timeout = 2  # 压到 2 秒（构造参数已过，直接改实例属性）
    result = await agent.run()
    assert result["ok"] is False and "TimeoutError" in result["error"]
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None and audit_round.error != ""


async def test_max_turns_exhausted_warns(repo: Repo, settings: Settings, tmp_path) -> None:
    """T5：无限工具调用直到轮次上限 → 强制结束，耗尽且无有效输出时落 error 报告。"""

    class _LoopProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, system, messages, tools):
            from src.agent.providers.base import LLMResponse, ToolCall

            self.calls += 1
            return LLMResponse(  # 永远返回工具调用，必耗到 max_turns
                text="继续查",
                tool_calls=[ToolCall("fetch_calendar", {})],
                raw=f"raw-{self.calls}",
                assistant_message={"role": "assistant", "content": "工具轮"},
            )

        def tool_result_message(self, call, result):
            return {"role": "user", "content": f"工具 {call.name} 结果：{result}"}

    agent = await _build_agent(repo, settings, _LoopProvider(), tmp_path)
    agent._max_turns = 5  # 压小轮次
    result = await agent.run()
    assert result["ok"] is False  # 轮次耗尽且无有效 JSON 输出 → 失败落 error 报告
    report = await repo.research.latest_report(include_error=True)
    assert report is not None and report.error != ""


# ---------- 审查补齐：H1 因果链暂存-回填 ----------


class _CausalLinkProvider(_SequentialProvider):
    """第一轮提交一条因果链（不传 report_id——LLM 无法预知本轮 id），第二轮输出 JSON。"""

    async def chat(self, system, messages, tools):
        from src.agent.providers.base import LLMResponse, ToolCall

        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                text="形成因果链",
                tool_calls=[
                    ToolCall(
                        "submit_causal_links",
                        {
                            "chain": [
                                {"node": "油价上涨", "kind": "事件"},
                                {"node": "通胀预期上升", "kind": "推断"},
                                {"node": "BTC 承压", "kind": "标的结论"},
                            ],
                            "confidence": 0.7,
                            "evidence": ["金十快讯"],
                        },
                    )
                ],
                raw=f"raw-{self._calls}",
                assistant_message={"role": "assistant", "content": "工具轮"},
            )
        return LLMResponse(
            text=GOOD_JSON,
            raw=f"raw-{self._calls}",
            assistant_message={"role": "assistant", "content": GOOD_JSON},
        )


async def test_full_round_flushes_causal_links(repo: Repo, settings: Settings, tmp_path) -> None:
    """回归（H1）：完整一轮中提交因果链 → 落研报后由代码回填本轮 report_id。

    修复前 LLM 被要求传尚不存在的研报 id，因果链按设计永远走不通；
    本测试走此前零覆盖的"run 完整一轮中提交"路径。
    """
    agent = await _build_agent(repo, settings, _CausalLinkProvider(), tmp_path)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
    links = await repo.research.list_causal_links()
    assert len(links) == 1
    assert links[0].report_id == result["report_id"]
    assert links[0].status == "pending"
    chain = json.loads(links[0].chain_json)
    assert [n["node"] for n in chain] == ["油价上涨", "通胀预期上升", "BTC 承压"]


async def test_failed_round_discards_causal_links(repo: Repo, settings: Settings, tmp_path) -> None:
    """H1 配套：本轮研报失败时暂存的因果链被丢弃（不留孤儿、不错挂历史研报）。"""

    class _SubmitThenBadJson(_SequentialProvider):
        async def chat(self, system, messages, tools):
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            if self._calls == 1:
                return LLMResponse(
                    text="先存链",
                    tool_calls=[
                        ToolCall(
                            "submit_causal_links",
                            {"chain": [{"node": "a"}, {"node": "b"}], "confidence": 0.5},
                        )
                    ],
                    raw="raw-1",
                    assistant_message={"role": "assistant", "content": "工具轮"},
                )
            return LLMResponse(  # 重试也输出坏 JSON → 整轮失败
                text="不是JSON",
                raw=f"raw-{self._calls}",
                assistant_message={"role": "assistant", "content": "不是JSON"},
            )

    agent = await _build_agent(repo, settings, _SubmitThenBadJson(), tmp_path)
    result = await agent.run()
    assert result["ok"] is False
    assert await repo.research.list_causal_links() == []


async def test_flush_causal_links_partial_failure(
    repo: Repo, settings: Settings, tmp_path, monkeypatch
) -> None:
    """复审 #7：多条暂存、单条落库失败 → 跳过该条不影响研报主产物，其余照常落库。"""

    class _TwoChainsProvider(_SequentialProvider):
        async def chat(self, system, messages, tools):
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            if self._calls == 1:
                return LLMResponse(
                    text="两条链",
                    tool_calls=[
                        ToolCall(
                            "submit_causal_links",
                            {"chain": [{"node": "a1"}, {"node": "b1"}], "confidence": 0.6},
                        ),
                        ToolCall(
                            "submit_causal_links",
                            {"chain": [{"node": "a2"}, {"node": "b2"}], "confidence": 0.7},
                        ),
                    ],
                    raw="raw-1",
                    assistant_message={"role": "assistant", "content": "工具轮"},
                )
            return LLMResponse(
                text=GOOD_JSON,
                raw=f"raw-{self._calls}",
                assistant_message={"role": "assistant", "content": GOOD_JSON},
            )

    real_save = repo.research.save_causal_link
    calls = {"n": 0}

    async def flaky_save(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("DB 抖动")
        return await real_save(**kwargs)

    monkeypatch.setattr(repo.research, "save_causal_link", flaky_save)
    agent = await _build_agent(repo, settings, _TwoChainsProvider(), tmp_path)
    result = await agent.run()
    assert result["ok"] is True  # 主产物不受单条失败影响
    links = await repo.research.list_causal_links()
    assert len(links) == 1  # 第 1 条成功，第 2 条跳过
    assert links[0].report_id == result["report_id"]


async def test_retry_round_tool_calls_executed_and_flushed(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """复审 #9④：JSON 重试轮携带的工具调用（L7 块）被执行，因果链随成功研报回填。"""

    class _RetryToolProvider(_SequentialProvider):
        async def chat(self, system, messages, tools):
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            if self._calls == 1:  # 首轮坏 JSON → 触发重试
                return LLMResponse(
                    text="坏JSON",
                    raw="raw-1",
                    assistant_message={"role": "assistant", "content": "坏JSON"},
                )
            if self._calls == 2:  # 重试轮带工具调用（L7 路径）
                return LLMResponse(
                    text="补条链",
                    tool_calls=[
                        ToolCall(
                            "submit_causal_links",
                            {"chain": [{"node": "x"}, {"node": "y"}], "confidence": 0.5},
                        )
                    ],
                    raw="raw-2",
                    assistant_message={"role": "assistant", "content": "补条链"},
                )
            return LLMResponse(  # 工具回填后输出合法 JSON
                text=GOOD_JSON,
                raw="raw-3",
                assistant_message={"role": "assistant", "content": GOOD_JSON},
            )

    agent = await _build_agent(repo, settings, _RetryToolProvider(), tmp_path)
    result = await agent.run()
    assert result["ok"] is True
    links = await repo.research.list_causal_links()
    assert len(links) == 1 and links[0].report_id == result["report_id"]
    # 重试轮的工具调用也进审计（seq 高位基数 900+）
    calls = await repo.list_audit_tool_calls(result["round_id"])
    assert any(c.tool == "submit_causal_links" for c in calls)


# ---------- WS 事件发射（对照复盘 agent 模式） ----------


async def test_event_success_sequence(repo: Repo, settings: Settings, tmp_path) -> None:
    """WS 事件：成功一轮恰好推 start + ok=True，round_id 一致。"""
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path, events.append)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[0]["data"]["round_id"] == result["round_id"]
    assert events[1]["data"] == {"round_id": result["round_id"], "ok": True}


async def test_event_failure_sequence(repo: Repo, settings: Settings, tmp_path) -> None:
    """WS 事件：失败一轮恰好推 start + ok=False。"""
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _BadJsonProvider(), tmp_path, events.append)
    result = await agent.run(report_type="asia")
    assert result["ok"] is False
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[0]["data"]["round_id"] == result["round_id"]
    assert events[1]["data"] == {"round_id": result["round_id"], "ok": False}


async def test_event_no_provider_zero_events(repo: Repo, settings: Settings, tmp_path) -> None:
    """WS 事件：LLM 未配置早退，零事件。"""
    events: list[dict] = []
    agent = await _build_agent(repo, settings, None, tmp_path, events.append)
    result = await agent.run()
    assert result["ok"] is False
    assert events == []


async def test_event_notifier_default_no_break(repo: Repo, settings: Settings, tmp_path) -> None:
    """WS 事件：未注入 notify_event（默认 None）时 run 正常完成不炸。"""
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path)
    result = await agent.run(report_type="us")
    assert result["ok"] is True


async def test_event_notifier_raise_does_not_break(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """WS 事件：广播回调抛异常只记日志，不拖垮 run。"""

    def _boom(_event: dict) -> None:
        raise RuntimeError("WS 连接断开")

    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path, _boom)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
