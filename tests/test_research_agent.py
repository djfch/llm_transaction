"""研报 agent 测试：完整一轮（预注入→工具循环→JSON 解析→落库→审计）。

provider 用确定性 Mock（不触网）；数据源用假实现；审计落 tmp_path。
"""

from __future__ import annotations

import json

import pytest

from src.audit.logger import get_logger  # noqa: F401  （确保日志初始化）
from src.audit.trail import AuditTrail
from src.config import AuditConfig, Settings
from src.memory import Database, Repo
from src.research.agent import ResearchAgent, _parse_payload
from src.research.providers.base import (
    CalendarEvent,
    FlashItem,
    ResearchDataProvider,
)
from src.research.providers.blockbeats import BlockbeatsSource  # noqa: F401
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
        return [
            CalendarEvent(
                title="非农",
                pub_time="2026-08-07 20:30",
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
    """输出非法 JSON 两次（验证重试后仍失败落 error 报告）。"""

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


async def _build_agent(repo: Repo, settings: Settings, provider, tmp_path) -> ResearchAgent:
    audit = AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit")))
    data = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    return ResearchAgent(
        settings=settings,
        provider=provider,
        repo=repo,
        audit=audit,
        prompt_loader=ResearchPromptLoader(tmp_path / "research_prompt.md"),
        data_provider=data,
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
    """输出非法 JSON 重试仍失败：落 error 报告 + 审计轮 error，返回 ok=False。"""
    agent = await _build_agent(repo, settings, _BadJsonProvider(), tmp_path)
    result = await agent.run(report_type="asia")
    assert result["ok"] is False
    assert "解析失败" in result["error"]
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
    """T3：首次输出坏 JSON、重试后合法 → ok=True（重试成功路径）。"""

    class _RetryProvider(_SequentialProvider):
        def __init__(self) -> None:
            super().__init__()
            self._retry_called = False

        async def chat(self, system, messages, tools):
            from src.agent.providers.base import LLMResponse

            self._calls += 1
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
