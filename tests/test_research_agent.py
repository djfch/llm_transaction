"""研报 agent 测试：完整一轮（预注入→工具循环→JSON 解析→落库→审计）。

provider 用确定性 Mock（不触网）；数据源用假实现；审计落 tmp_path。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime

import pytest

from src.agent.providers.base import LLMError, LLMParseError, LLMResponse
from src.agent.providers.retry import RetryingProvider
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
from src.utils import LLMIdentity

logger = get_logger(__name__)

GOOD_JSON = json.dumps(
    {
        "summary": "BTC 流动性改善",
        "cross_market_view": "单标的研报，无跨标的比较",
        "global_risks": ["高利率"],
        "asset_views": [
            {
                "contract": "BTC_USDT",
                "direction": "偏多",
                "confidence": "高",
                "horizon": "当日",
                "market_regime": "上涨趋势",
                "technical_confirmation": "确认",
                "basis_type": "混合",
                "evidence": [{"point": "ETF 连续流入", "source": "指标快照"}],
                "risks": ["高利率"],
                "narrative": "流动性宽松，技术结构确认",
            }
        ],
    },
    ensure_ascii=False,
)


class _FakeJin10:
    async def fetch_calendar(self):
        # 事件日期按北京时区动态生成（复审 #6 配套）：写死日期跨天后即过期
        """返回测试用经济日历事件。

        参数：
            self: _FakeJin10，当前测试替身实例
        返回：
            list[CalendarEvent]，返回该测试辅助函数构造或记录的结果
        """
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
        """返回测试用快讯列表。

        参数：
            self: _FakeJin10，当前测试替身实例
            hours: int，回溯小时数
        返回：
            list[FlashItem]，返回该测试辅助函数构造或记录的结果
        """
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
        """返回指定文章的测试详情。

        参数：
            self: _FakeJin10，当前测试替身实例
            item_id: str，文章标识
        返回：
            str，返回该测试辅助函数构造或记录的结果
        """
        return "详情"

    async def search_news(self, keyword, limit=20):
        """返回符合条件的新闻搜索结果。

        参数：
            self: _FakeJin10，当前测试替身实例
            keyword: str，搜索关键词
            limit: int，返回数量上限
        返回：
            list[object]，返回该测试辅助函数构造或记录的结果
        """
        return []


class _FakeBb:
    async def fetch_flash(self, hours=24):
        """返回测试用快讯列表。

        参数：
            self: _FakeBb，当前测试替身实例
            hours: int，回溯小时数
        返回：
            list[FlashItem]，返回该测试辅助函数构造或记录的结果
        """
        return []

    async def fetch_indicators(self):
        """返回测试用市场指标摘要。

        参数：
            self: _FakeBb，当前测试替身实例
        返回：
            str，返回该测试辅助函数构造或记录的结果
        """
        return "## BTC ETF 净流入\n+2.1 亿"

    async def search_news(self, keyword, limit=20):
        """返回符合条件的新闻搜索结果。

        参数：
            self: _FakeBb，当前测试替身实例
            keyword: str，搜索关键词
            limit: int，返回数量上限
        返回：
            list[object]，返回该测试辅助函数构造或记录的结果
        """
        return []


class _ResearchMarketData:
    async def snapshot(self, contract: str, limit: int = 30) -> dict:
        """返回指定合约的测试市场快照。

        参数：
            self: _ResearchMarketData，当前测试替身实例
            contract: str，合约标识
            limit: int，返回数量上限
        返回：
            dict，返回该测试辅助函数构造或记录的结果
        """
        return {
            "contract": contract,
            "requested_limit": limit,
            "data_status": "完整",
            "funding_rate": "0.0001",
            "timeframes": {},
        }


class _SequentialProvider:
    """确定性 Mock：第一轮取公共信息和白名单行情，第二轮输出 JSON。"""

    def __init__(self) -> None:
        """初始化测试替身及其可观测状态。

        参数：
            self: _SequentialProvider，当前测试替身实例
        返回：
            None，初始化并保存测试替身状态
        """
        self._calls = 0

    async def chat(self, system: str, messages: list[dict], tools: list[dict]):
        """返回该轮测试预设的模型响应。

        参数：
            self: _SequentialProvider，当前测试替身实例
            system: str，系统提示词
            messages: list[dict]，对话消息列表
            tools: list[dict]，工具定义列表
        返回：
            LLMResponse，返回该测试辅助函数构造或记录的结果
        """
        from src.agent.providers.base import LLMResponse, ToolCall

        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                text="先看日历与指标",
                tool_calls=[
                    ToolCall("fetch_calendar", {}),
                    ToolCall("fetch_indicators", {}),
                    ToolCall("get_research_market_data", {"contract": "BTC_USDT"}),
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
        """构造模型可消费的工具结果消息。

        参数：
            self: _SequentialProvider，当前测试替身实例
            call: object，工具调用对象
            result: str，工具执行结果文本
        返回：
            dict，返回该测试辅助函数构造或记录的结果
        """
        return {"role": "user", "content": f"工具 {call.name} 结果：{result}"}


class _BadJsonProvider(_SequentialProvider):
    """持续输出非法 JSON（同上下文重发 3 次后仍失败，落 error 报告）。"""

    def __init__(self) -> None:
        """初始化测试替身及其可观测状态。

        参数：
            self: _BadJsonProvider，当前测试替身实例
        返回：
            None，初始化并保存测试替身状态
        """
        super().__init__()
        self._bad = True

    async def chat(self, system: str, messages: list[dict], tools: list[dict]):
        """返回该轮测试预设的模型响应。

        参数：
            self: _BadJsonProvider，当前测试替身实例
            system: str，系统提示词
            messages: list[dict]，对话消息列表
            tools: list[dict]，工具定义列表
        返回：
            LLMResponse，返回该测试辅助函数构造或记录的结果
        """
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


class _RejectThenHangProvider:
    """首次抛出带原文的解析错误，第二次等待研报超时保险丝取消。"""

    def __init__(self, *, main_bad_first: bool = False) -> None:
        """初始化调用计数与是否先返回主对话坏业务 JSON。

        参数：
            main_bad_first: bool，是否先正常返回一段会触发最终 JSON 重问的文本

        返回：
            None，初始化测试提供商状态
        """
        self.calls = 0
        self.main_bad_first = main_bad_first
        self.waiting = asyncio.Event()

    async def chat(self, system: str, messages: list[dict], tools: list[dict]):
        """按配置返回主对话文本，随后拒绝一次响应并永久等待。

        参数：
            system: str，系统提示词
            messages: list[dict]，对话消息列表
            tools: list[dict]，工具定义列表

        返回：
            LLMResponse，配置要求时返回触发最终 JSON 重问的主对话文本

        异常：
            LLMParseError: 指定的重试阶段调用模拟响应解析失败
        """
        self.calls += 1
        if self.main_bad_first and self.calls == 1:
            return LLMResponse(
                text="不是业务 JSON",
                raw="raw-main-bad-json",
                assistant_message={"role": "assistant", "content": "不是业务 JSON"},
            )
        reject_call = 2 if self.main_bad_first else 1
        if self.calls == reject_call:
            raise LLMParseError("工具参数不是合法 JSON", raw="raw-before-timeout")
        self.waiting.set()
        await asyncio.Event().wait()

    def tool_result_message(self, call, result: str) -> dict:
        """构造不会在本场景实际使用的工具结果消息。

        参数：
            call: object，工具调用对象
            result: str，工具执行结果文本

        返回：
            dict，模型可消费的工具结果消息
        """
        return {"role": "user", "content": result}


@pytest.fixture
async def repo(tmp_path) -> AsyncIterator[Repo]:
    """创建测试数据库仓库并在用例结束后关闭连接。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        AsyncIterator[Repo]，返回该测试辅助函数构造或记录的结果
    """
    db = Database()
    await db.open(tmp_path / "research.db")
    try:
        yield Repo(db)
    finally:
        await db.close()


@pytest.fixture
def settings() -> Settings:
    """创建模拟交易模式的测试配置。

    参数：无
    返回：
        Settings，返回该测试辅助函数构造或记录的结果
    """
    return Settings(mode="paper")


async def _build_agent(
    repo: Repo, settings: Settings, provider, tmp_path, notify_event=None
) -> ResearchAgent:
    """组装使用测试替身的研报 Agent。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        provider: object，模型提供方测试替身
        tmp_path: Path，pytest 提供的临时目录
        notify_event: object，可选事件通知回调
    返回：
        ResearchAgent，返回该测试辅助函数构造或记录的结果
    """
    audit = AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit")))
    data = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    return ResearchAgent(
        settings=settings,
        provider=provider,
        repo=repo,
        audit=audit,
        prompt_loader=ResearchPromptLoader(tmp_path / "research_prompt.md"),
        data_provider=data,
        market_data=_ResearchMarketData(),
        watchlist=("BTC_USDT",),
        notify_event=notify_event,
        max_turns=10,
        timeout_seconds=60,
    )


async def test_full_round_success(repo: Repo, settings: Settings, tmp_path) -> None:
    """完整一轮：工具循环→JSON 落库→审计轮完整。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
    assert result["asset_count"] == 1
    report = await repo.research.latest_report()
    assert report is not None
    views = await repo.research.list_asset_views_by_report(report.id)
    assert len(views) == 1 and views[0].direction == "偏多"
    assert report.report_type == "us"
    # 审计轮完整：1 轮 + 3 次工具调用
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None
    assert audit_round.wake_source == "research"
    assert audit_round.error == ""
    calls = await repo.list_audit_tool_calls(result["round_id"])
    assert [c.tool for c in calls] == [
        "fetch_calendar",
        "fetch_indicators",
        "get_research_market_data",
    ]


async def test_run_records_provider_identity(repo: Repo, settings: Settings, tmp_path) -> None:
    """研报轮开轮即把 provider 的模型身份落入审计四列（跨模型效果对比的数据源）。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，断言审计行四列与注入身份一致
    """
    provider = _SequentialProvider()
    provider.identity = LLMIdentity(
        credential_name="ds-main",
        provider="openai_compat",
        model="deepseek-v4-flash",
        thinking_effort="high",
    )
    agent = await _build_agent(repo, settings, provider, tmp_path)
    result = await agent.run(report_type="us")

    assert result["ok"] is True
    row = await repo.get_audit_round(result["round_id"])
    assert row is not None
    assert (
        row.llm_credential_name,
        row.llm_provider,
        row.llm_model,
        row.llm_thinking_effort,
    ) == ("ds-main", "openai_compat", "deepseek-v4-flash", "high")


async def test_bad_json_falls_to_error_report(repo: Repo, settings: Settings, tmp_path) -> None:
    """输出非法 JSON、同上下文重发 3 次仍失败：落 error 报告 + 审计轮 error，返回 ok=False。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
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
    """LLM 未配置：直接失败，不落审计、不落研报。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    agent = await _build_agent(repo, settings, None, tmp_path)
    result = await agent.run()
    assert result["ok"] is False and result["error_code"] == "llm_not_configured"
    assert await repo.research.latest_report() is None


def test_parse_payload_edge_cases() -> None:
    """JSON 解析容错：代码块包裹、缺字段、非法文本、合法。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    kwargs = {
        "expected_contracts": ("BTC_USDT",),
        "queried_contracts": {"BTC_USDT"},
        "data_statuses": {"BTC_USDT": "完整"},
    }
    parsed = _parse_payload(GOOD_JSON, **kwargs)
    assert parsed is not None and parsed["asset_views"][0]["direction"] == "偏多"
    assert _parse_payload('{"summary": "缺逐标的"}', **kwargs) is None
    assert _parse_payload("not json", **kwargs) is None


async def test_tool_call_failure_does_not_abort(repo: Repo, settings: Settings, tmp_path) -> None:
    """工具执行失败（未知工具）不中断循环，最终仍产出研报。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    class _ToolErrorProvider(_SequentialProvider):
        async def chat(self, system: str, messages: list[dict], tools: list[dict]):
            """返回该轮测试预设的模型响应。

            参数：
                self: _ToolErrorProvider，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            if self._calls == 1:
                return LLMResponse(
                    text="调个不存在的工具",
                    tool_calls=[
                        ToolCall("not_a_tool", {}),
                        ToolCall("get_research_market_data", {"contract": "BTC_USDT"}),
                    ],
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
    """T2：provider.chat 抛异常 → 落 error 报告 + 审计轮 error，返回 ok=False。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    class _RaisingProvider:
        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _RaisingProvider，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            异常：
                RuntimeError: 测试场景主动触发该失败条件时抛出
            """
            raise LLMError("LLM 服务不可用", raw='{"output":"已收到"}')

        def tool_result_message(self, call, result):
            """构造模型可消费的工具结果消息。

            参数：
                self: _RaisingProvider，当前测试替身实例
                call: object，工具调用对象
                result: str，工具执行结果文本
            返回：
                dict，返回该测试辅助函数构造或记录的结果
            """
            return {"role": "user", "content": "x"}

    agent = await _build_agent(repo, settings, _RaisingProvider(), tmp_path)
    result = await agent.run()
    assert result["ok"] is False and "LLMError" in result["error"]
    report = await repo.research.latest_report(include_error=True)
    assert report is not None and report.error != ""
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None and audit_round.error != ""
    assert audit_round.llm_raw == '{"output":"已收到"}'


async def test_json_retry_success_path(repo: Repo, settings: Settings, tmp_path) -> None:
    """T3：首次输出坏 JSON、同上下文重发后合法 → ok=True（重试成功路径）。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    class _RetryProvider(_SequentialProvider):
        def __init__(self) -> None:
            """初始化测试替身及其可观测状态。

            参数：
                self: _RetryProvider，当前测试替身实例
            返回：
                None，初始化并保存测试替身状态
            """
            super().__init__()
            self._retry_called = False
            self.seen: list[list[dict]] = []  # 每次调用收到的 messages 快照

        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _RetryProvider，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            self.seen.append([dict(m) for m in messages])
            if self._calls == 1:
                return LLMResponse(
                    text="先取行情",
                    tool_calls=[ToolCall("get_research_market_data", {"contract": "BTC_USDT"})],
                    raw="raw-market",
                    assistant_message={"role": "assistant", "content": "工具轮"},
                )
            if self._calls == 2:
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
    assert result["ok"] is True and result["asset_count"] == 1
    assert provider._retry_called  # 确实走了重试
    # 同上下文重发：重试请求与产生坏输出的请求一致，未追加纠错/失败内容
    assert provider._calls == 3
    assert provider.seen[2] == provider.seen[1]


async def test_json_retry_third_attempt_success(repo: Repo, settings: Settings, tmp_path) -> None:
    """坏 JSON 连续 2 次、第 3 次输出合法 → ok=True；三次请求上下文完全一致。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    class _ThirdTimeProvider(_SequentialProvider):
        def __init__(self) -> None:
            """初始化测试替身及其可观测状态。

            参数：
                self: _ThirdTimeProvider，当前测试替身实例
            返回：
                None，初始化并保存测试替身状态
            """
            super().__init__()
            self.seen: list[list[dict]] = []

        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _ThirdTimeProvider，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            self.seen.append([dict(m) for m in messages])
            if self._calls == 1:
                return LLMResponse(
                    text="先取行情",
                    tool_calls=[ToolCall("get_research_market_data", {"contract": "BTC_USDT"})],
                    raw="raw-market",
                    assistant_message={"role": "assistant", "content": "工具轮"},
                )
            if self._calls <= 3:
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
    assert result["ok"] is True and result["asset_count"] == 1
    assert provider._calls == 4  # 行情工具 + 初始 + 2 次同参重发
    assert provider.seen[1] == provider.seen[2] == provider.seen[3]


async def test_reask_uses_full_context_after_tool_round(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """工具轮后产出坏 JSON：重发上下文为 [user, assistant, 工具结果] 全序列，坏输出不回灌。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    class _ToolThenBadJson(_SequentialProvider):
        def __init__(self) -> None:
            """初始化测试替身及其可观测状态。

            参数：
                self: _ToolThenBadJson，当前测试替身实例
            返回：
                None，初始化并保存测试替身状态
            """
            super().__init__()
            self.seen: list[list[dict]] = []

        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _ToolThenBadJson，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            self.seen.append([dict(m) for m in messages])
            if self._calls == 1:
                return LLMResponse(
                    text="先查日历",
                    tool_calls=[
                        ToolCall("fetch_calendar", {}),
                        ToolCall("get_research_market_data", {"contract": "BTC_USDT"}),
                    ],
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
    assert len(provider.seen[2]) == 4
    assert provider.seen[2][0]["role"] == "user"
    assert provider.seen[2][1] == {"role": "assistant", "content": "工具轮"}
    assert "坏JSON" not in str(provider.seen[2])


async def test_parse_retry_phase_under_timeout_fuse(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """JSON 重试阶段同受超时保险丝约束：重发卡死 → TimeoutError 落 error 报告。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    class _BadThenHang:
        def __init__(self) -> None:
            """初始化测试替身及其可观测状态。

            参数：
                self: _BadThenHang，当前测试替身实例
            返回：
                None，初始化并保存测试替身状态
            """
            self.calls = 0

        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _BadThenHang，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
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
            """构造模型可消费的工具结果消息。

            参数：
                self: _BadThenHang，当前测试替身实例
                call: object，工具调用对象
                result: str，工具执行结果文本
            返回：
                dict，返回该测试辅助函数构造或记录的结果
            """
            return {"role": "user", "content": "x"}

    provider = _BadThenHang()
    agent = await _build_agent(repo, settings, provider, tmp_path)
    agent._timeout = 2  # 压到 2 秒（构造参数已过，直接改实例属性）
    result = await agent.run()
    assert result["ok"] is False and "TimeoutError" in result["error"]
    report = await repo.research.latest_report(include_error=True)
    assert report is not None and report.error != ""


async def test_provider_retry_timeout_keeps_prior_rejected_raw(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """provider 内部重试被研报超时取消时，先前拒绝响应仍进入审计。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言超时失败轮保留 rejected 响应信封
    """
    provider = RetryingProvider(_RejectThenHangProvider(), max_attempts=2, backoff=(10.0,))
    agent = await _build_agent(repo, settings, provider, tmp_path)
    agent._timeout = 0.02

    result = await agent.run()

    assert result["ok"] is False and "TimeoutError" in result["error"]
    audit_round = await repo.latest_audit_round("paper")
    attempts = [json.loads(line) for line in audit_round.llm_raw.splitlines()]
    assert [(item["status"], item["raw"]) for item in attempts] == [
        ("rejected", "raw-before-timeout")
    ]


async def test_final_json_retry_timeout_keeps_prior_rejected_raw(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """最终 JSON 重问内部超时，也必须保留已收到的拒绝响应。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言主响应与重问拒绝响应均进入同一审计轮
    """
    inner = _RejectThenHangProvider(main_bad_first=True)
    provider = RetryingProvider(inner, max_attempts=2, backoff=())
    agent = await _build_agent(repo, settings, provider, tmp_path)
    agent._timeout = 0.02

    result = await agent.run()

    assert result["ok"] is False and "TimeoutError" in result["error"]
    audit_round = await repo.latest_audit_round("paper")
    attempts = [json.loads(line) for line in audit_round.llm_raw.splitlines()]
    assert [(item["status"], item["raw"]) for item in attempts] == [
        ("accepted", "raw-main-bad-json"),
        ("rejected", "raw-before-timeout"),
    ]


async def test_external_cancel_keeps_retry_raw_and_propagates_cancelled_error(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """外部主动取消研报任务时，重试器内已收到的响应仍落审计且取消原样传播。

    同时断言取消收尾副作用：失败报告落库、审计轮以 CancelledError 结束、
    事件序列以 research_round ok=False 收尾。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言取消传播、审计收尾、失败报告和 rejected 响应同时成立
    """
    inner = _RejectThenHangProvider()
    provider = RetryingProvider(inner, max_attempts=2, backoff=())
    events: list[dict] = []
    agent = await _build_agent(repo, settings, provider, tmp_path, notify_event=events.append)
    agent._timeout = 60
    task = asyncio.create_task(agent.run())
    await asyncio.wait_for(inner.waiting.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    audit_round = await repo.latest_audit_round("paper")
    attempts = [json.loads(line) for line in audit_round.llm_raw.splitlines()]
    assert [(item["status"], item["raw"]) for item in attempts] == [
        ("rejected", "raw-before-timeout")
    ]
    assert audit_round.error == "CancelledError: 研报被取消"
    assert audit_round.ended_at is not None
    report = await repo.research.latest_report(include_error=True)
    assert report is not None and report.error == "CancelledError: 研报被取消"
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[-1]["data"] == {"round_id": audit_round.round_id, "ok": False}


async def test_timeout_terminates_round(repo: Repo, settings: Settings, tmp_path) -> None:
    """T4：超时强制终止 → 落 error 报告，审计轮收尾。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    class _SlowProvider:
        def __init__(self) -> None:
            """初始化测试替身及其可观测状态。

            参数：
                self: _SlowProvider，当前测试替身实例
            返回：
                None，初始化并保存测试替身状态
            """
            self.calls = 0

        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _SlowProvider，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
            self.calls += 1
            import asyncio

            await asyncio.sleep(60)  # 远超 2s 超时

        def tool_result_message(self, call, result):
            """构造模型可消费的工具结果消息。

            参数：
                self: _SlowProvider，当前测试替身实例
                call: object，工具调用对象
                result: str，工具执行结果文本
            返回：
                dict，返回该测试辅助函数构造或记录的结果
            """
            return {"role": "user", "content": "x"}

    provider = _SlowProvider()
    agent = await _build_agent(repo, settings, provider, tmp_path)
    agent._timeout = 2  # 压到 2 秒（构造参数已过，直接改实例属性）
    result = await agent.run()
    assert result["ok"] is False and "TimeoutError" in result["error"]
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None and audit_round.error != ""


async def test_max_turns_exhausted_warns(repo: Repo, settings: Settings, tmp_path) -> None:
    """T5：无限工具调用直到轮次上限 → 强制结束，耗尽且无有效输出时落 error 报告。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    class _LoopProvider:
        def __init__(self) -> None:
            """初始化测试替身及其可观测状态。

            参数：
                self: _LoopProvider，当前测试替身实例
            返回：
                None，初始化并保存测试替身状态
            """
            self.calls = 0

        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _LoopProvider，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
            from src.agent.providers.base import LLMResponse, ToolCall

            self.calls += 1
            return LLMResponse(  # 永远返回工具调用，必耗到 max_turns
                text="继续查",
                tool_calls=[ToolCall("fetch_calendar", {})],
                raw=f"raw-{self.calls}",
                assistant_message={"role": "assistant", "content": "工具轮"},
            )

        def tool_result_message(self, call, result):
            """构造模型可消费的工具结果消息。

            参数：
                self: _LoopProvider，当前测试替身实例
                call: object，工具调用对象
                result: str，工具执行结果文本
            返回：
                dict，返回该测试辅助函数构造或记录的结果
            """
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
        """返回该轮测试预设的模型响应。

        参数：
            self: _CausalLinkProvider，当前测试替身实例
            system: str，系统提示词
            messages: list[dict]，对话消息列表
            tools: list[dict]，工具定义列表
        返回：
            LLMResponse，返回该测试辅助函数构造或记录的结果
        """
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
                            "topic": "油价",
                        },
                    ),
                    ToolCall("get_research_market_data", {"contract": "BTC_USDT"}),
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

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    agent = await _build_agent(repo, settings, _CausalLinkProvider(), tmp_path)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
    links = await repo.research.list_causal_links()
    assert len(links) == 1
    assert links[0].report_id == result["report_id"]
    assert links[0].status == "tracking"
    assert links[0].topic == "油价"  # topic 透传
    assert links[0].supersedes_id is None
    chain = json.loads(links[0].chain_json)
    assert [n["node"] for n in chain] == ["油价上涨", "通胀预期上升", "BTC 承压"]


async def test_failed_round_discards_causal_links(repo: Repo, settings: Settings, tmp_path) -> None:
    """H1 配套：本轮研报失败时暂存的因果链被丢弃（不留孤儿、不错挂历史研报）。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    class _SubmitThenBadJson(_SequentialProvider):
        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _SubmitThenBadJson，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            if self._calls == 1:
                return LLMResponse(
                    text="先存链",
                    tool_calls=[
                        ToolCall(
                            "submit_causal_links",
                            {
                                "chain": [{"node": "a"}, {"node": "b"}],
                                "confidence": 0.5,
                                "topic": "关税",
                            },
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
    """复审 #7：多条暂存、单条落库失败 → 跳过该条不影响研报主产物，其余照常落库。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """

    class _TwoChainsProvider(_SequentialProvider):
        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _TwoChainsProvider，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            if self._calls == 1:
                return LLMResponse(
                    text="两条链",
                    tool_calls=[
                        ToolCall(
                            "submit_causal_links",
                            {
                                "chain": [{"node": "a1"}, {"node": "b1"}],
                                "confidence": 0.6,
                                "topic": "关税",
                            },
                        ),
                        ToolCall(
                            "submit_causal_links",
                            {
                                "chain": [{"node": "a2"}, {"node": "b2"}],
                                "confidence": 0.7,
                                "topic": "非农",
                            },
                        ),
                        ToolCall("get_research_market_data", {"contract": "BTC_USDT"}),
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
        """模拟单条因果链保存失败并转发其余保存请求。

        参数：
            **kwargs: dict[str, object]，透传的关键字参数
        返回：
            CausalLink，未触发模拟故障时保存并返回的因果链
        异常：
            RuntimeError: 处理第二条因果链时模拟数据库抖动
        """
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
    """复审 #9④：JSON 重试轮携带的工具调用（L7 块）被执行，因果链随成功研报回填。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    class _RetryToolProvider(_SequentialProvider):
        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _RetryToolProvider，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            if self._calls == 1:
                return LLMResponse(
                    text="先取行情",
                    tool_calls=[ToolCall("get_research_market_data", {"contract": "BTC_USDT"})],
                    raw="raw-market",
                    assistant_message={"role": "assistant", "content": "工具轮"},
                )
            if self._calls == 2:  # 首轮坏 JSON → 触发重试
                return LLMResponse(
                    text="坏JSON",
                    raw="raw-1",
                    assistant_message={"role": "assistant", "content": "坏JSON"},
                )
            if self._calls == 3:  # 重试轮带工具调用（L7 路径）
                return LLMResponse(
                    text="补条链",
                    tool_calls=[
                        ToolCall(
                            "submit_causal_links",
                            {
                                "chain": [{"node": "x"}, {"node": "y"}],
                                "confidence": 0.5,
                                "topic": "关税",
                            },
                        )
                    ],
                    raw="raw-4",
                    assistant_message={"role": "assistant", "content": "补条链"},
                )
            return LLMResponse(  # 工具回填后输出合法 JSON
                text=GOOD_JSON,
                raw="raw-4",
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
    """WS 事件：成功一轮恰好推 start + ok=True，round_id 一致。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path, events.append)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[0]["data"]["round_id"] == result["round_id"]
    assert events[1]["data"] == {"round_id": result["round_id"], "ok": True}


async def test_event_failure_sequence(repo: Repo, settings: Settings, tmp_path) -> None:
    """WS 事件：失败一轮恰好推 start + ok=False。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _BadJsonProvider(), tmp_path, events.append)
    result = await agent.run(report_type="asia")
    assert result["ok"] is False
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[0]["data"]["round_id"] == result["round_id"]
    assert events[1]["data"] == {"round_id": result["round_id"], "ok": False}


async def test_event_no_provider_zero_events(repo: Repo, settings: Settings, tmp_path) -> None:
    """WS 事件：LLM 未配置早退，零事件。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    events: list[dict] = []
    agent = await _build_agent(repo, settings, None, tmp_path, events.append)
    result = await agent.run()
    assert result["ok"] is False
    assert events == []


async def test_event_notifier_default_no_break(repo: Repo, settings: Settings, tmp_path) -> None:
    """WS 事件：未注入 notify_event（默认 None）时 run 正常完成不炸。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path)
    result = await agent.run(report_type="us")
    assert result["ok"] is True


async def test_event_notifier_raise_does_not_break(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """WS 事件：广播回调抛异常只记日志，不拖垮 run。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """

    def _boom(_event: dict) -> None:
        """模拟依赖调用失败。

        参数：
            _event: dict，待广播的测试事件
        返回：
            None，不会正常返回，用于模拟失败路径
        异常：
            RuntimeError: 测试场景主动触发该失败条件时抛出
        """
        raise RuntimeError("WS 连接断开")

    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path, _boom)
    result = await agent.run(report_type="us")
    assert result["ok"] is True


V2_JSON = json.dumps(
    {
        "summary": "逐标的研报",
        "cross_market_view": "BTC 与 ETH 同步",
        "global_risks": ["CPI"],
        "asset_views": [
            {
                "contract": contract,
                "direction": "偏多",
                "confidence": "高",
                "horizon": "3日",
                "market_regime": "上涨趋势",
                "technical_confirmation": "确认",
                "basis_type": "宏观驱动",
                "evidence": [{"point": "资金流入", "source": "快讯"}],
                "risks": ["数据反转"],
                "narrative": f"{contract} 结构向上",
            }
            for contract in ("BTC_USDT", "ETH_USDT")
        ],
    },
    ensure_ascii=False,
)


class _V2Provider(_SequentialProvider):
    async def chat(self, system: str, messages: list[dict], tools: list[dict]):
        """返回该轮测试预设的模型响应。

        参数：
            self: _V2Provider，当前测试替身实例
            system: str，系统提示词
            messages: list[dict]，对话消息列表
            tools: list[dict]，工具定义列表
        返回：
            LLMResponse，返回该测试辅助函数构造或记录的结果
        """
        from src.agent.providers.base import LLMResponse, ToolCall

        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                text="读取白名单市场快照",
                tool_calls=[
                    ToolCall("get_research_market_data", {"contract": "BTC_USDT"}),
                    ToolCall("get_research_market_data", {"contract": "ETH_USDT", "limit": 20}),
                ],
                raw="raw-v2-tools",
                assistant_message={"role": "assistant", "content": "工具轮"},
            )
        return LLMResponse(
            text=V2_JSON,
            raw="raw-v2-final",
            assistant_message={"role": "assistant", "content": V2_JSON},
        )


async def test_v2_round_saves_every_whitelist_asset(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """验证第二版研报会为白名单中的每个合约保存资产结论。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    audit = AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit-v2")))
    agent = ResearchAgent(
        settings=settings,
        provider=_V2Provider(),
        repo=repo,
        audit=audit,
        prompt_loader=ResearchPromptLoader(tmp_path / "research_prompt-v2.md"),
        data_provider=ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb()),
        market_data=_ResearchMarketData(),
        watchlist=("BTC_USDT", "ETH_USDT"),
        max_turns=10,
        timeout_seconds=60,
    )

    result = await agent.run(report_type="us_open")

    assert result["ok"] is True
    assert result["asset_count"] == 2
    report = await repo.research.get_report(result["report_id"])
    assert report is not None and report.schema_version == 3
    views = await repo.research.list_asset_views_by_report(report.id)
    assert [view.contract for view in views] == ["BTC_USDT", "ETH_USDT"]
    assert json.loads(views[0].market_context_json)["contract"] == "BTC_USDT"


async def test_cancel_after_success_persist_no_double_write(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功研报落库后、finalization 阶段被取消：禁止双写失败报告，审计以成功闭合且仅一次。

    在 end_round 处注入一次性取消（因果链回填/end_round 窗口）：旧实现会经 _fail 再插
    失败报告并把审计轮以 error 闭合，同一 round 成功/失败双写。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，替换 AuditTrail.end_round 注入取消

    返回：
        None，断言仅一份成功研报、审计成功闭合一次、事件以 ok=True 收尾
    """
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path, events.append)
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
    task = asyncio.create_task(agent.run(report_type="us"))
    with pytest.raises(asyncio.CancelledError):
        await task

    items, total = await repo.research.list_reports_page(10, 0)
    assert total == 1  # 同一 round 只有成功研报一份，无失败报告双写
    assert items[0].error == ""
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None and audit_round.ended_at is not None
    assert audit_round.error == ""  # 审计轮以成功语义闭合
    assert state["closed"] == 1  # 真实闭合仅发生一次（由取消分支补闭合）
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[-1]["data"] == {"round_id": audit_round.round_id, "ok": True}


async def test_cancel_between_commit_and_return_rechecks_success(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消掐在「成功 INSERT/COMMIT 已执行、保存函数未返回」窗口：反查库识出已提交，禁止失败双写。

    monkeypatch save_report_bundle 为一次性 fake：先调真实方法真实提交、再抛
    CancelledError（模拟底层已提交但调用方收到取消、report_id 仍 None）。修复前
    该场景会经 _fail 再写一份失败报告；修复后按 round_id 反查库改走成功收尾。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，替换 save_report_bundle 注入提交后取消

    返回：
        None，断言取消传播、仅一份成功研报、因果链补落库、审计成功闭合一次、ok=True 收尾
    """
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _CausalLinkProvider(), tmp_path, events.append)
    real_save = repo.research.save_report_bundle
    state = {"fired": False}

    async def committed_then_cancelled(**kwargs):
        """首次调用真实提交成功后抛取消（模拟 COMMIT 已执行、调用方收到取消的窗口）。

        参数：
            kwargs: dict，save_report_bundle 的关键字参数，原样透传真实方法

        返回：
            tuple：首次调用不返回（抛取消）；其后调用委托真实方法返回落库结果

        异常：
            asyncio.CancelledError：首次调用真实提交后抛出，模拟取消送达时机
        """
        if not state["fired"]:
            state["fired"] = True
            await real_save(**kwargs)
            raise asyncio.CancelledError()
        return await real_save(**kwargs)

    monkeypatch.setattr(repo.research, "save_report_bundle", committed_then_cancelled)
    task = asyncio.create_task(agent.run(report_type="us"))
    with pytest.raises(asyncio.CancelledError):
        await task

    items, total = await repo.research.list_reports_page(10, 0)
    assert total == 1  # 成功报告已提交被反查识别：无失败报告双写
    assert items[0].error == ""
    links = await repo.research.list_causal_links()
    assert len(links) == 1 and links[0].report_id == items[0].id  # 取消时 flush 未跑，收尾补落库
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None and audit_round.ended_at is not None
    assert audit_round.error == ""  # 审计轮以成功语义闭合
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[-1]["data"] == {"round_id": audit_round.round_id, "ok": True}


async def test_cancel_recheck_db_failure_falls_back_to_fail(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """取消收尾的反查自身失败（DB 抖动）：回落失败语义收尾，取消原样传播。

    与 test_cancel_between_commit_and_return_rechecks_success 同场景（成功 COMMIT 已执行、
    调用方收到取消），但 find_report_by_round_id 抛 RuntimeError：_committed_report_id
    记日志后按「未提交」处理，走 _fail 落失败报告——此时库内成功+失败各一份（可接受
    退化，优于静默丢轮），日志须留下反查失败痕迹。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，注入提交后取消与反查失败
        caplog: pytest.LogCaptureFixture，捕获反查失败日志

    返回：
        None，断言取消传播、失败报告落库、反查失败日志留痕
    """
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path)
    real_save = repo.research.save_report_bundle
    state = {"fired": False}

    async def committed_then_cancelled(**kwargs):
        """首次调用真实提交成功后抛取消（模拟 COMMIT 已执行、调用方收到取消的窗口）。

        参数：
            kwargs: dict，save_report_bundle 的关键字参数，原样透传真实方法

        返回：
            tuple：首次调用不返回（抛取消）；其后调用委托真实方法返回落库结果

        异常：
            asyncio.CancelledError：首次调用真实提交后抛出，模拟取消送达时机
        """
        if not state["fired"]:
            state["fired"] = True
            await real_save(**kwargs)
            raise asyncio.CancelledError()
        return await real_save(**kwargs)

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

    monkeypatch.setattr(repo.research, "save_report_bundle", committed_then_cancelled)
    monkeypatch.setattr(repo.research, "find_report_by_round_id", broken_find)
    task = asyncio.create_task(agent.run(report_type="us"))
    with (
        caplog.at_level("ERROR", logger="src.research.agent"),
        pytest.raises(asyncio.CancelledError),
    ):
        await task

    items, total = await repo.research.list_reports_page(10, 0)
    # 成功报告（fake 已提交）+ 失败报告（反查失败回落 _fail）各一份：可接受退化双写
    assert total == 2
    errors = {item.error for item in items}
    assert "CancelledError: 研报被取消" in errors  # 失败报告来自 _fail 回落
    assert "" in errors  # 成功报告仍在（反查失败不会抹掉已提交结果）
    assert "反查成功报告失败" in caplog.text  # 反查失败留痕


async def test_end_round_runtime_error_returns_success_no_double_write(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功研报落库后 end_round 首次抛普通异常（RuntimeError）：按成功语义补全，禁止双写。

    普通异常与取消同口径（except Exception 分支）：report_id 已置位时经
    _complete_interrupted 补全收尾——已落库因果链保留、审计轮补成功闭合、
    轮末事件 ok=True、返回成功 dict，不再经 _fail 插失败报告。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，向 AuditTrail.end_round 注入一次性异常

    返回：
        None，断言成功返回、单份成功研报、因果链保留、审计成功闭合一次、ok=True 收尾
    """
    real_end_round = AuditTrail.end_round
    state = {"failed": False, "closed": 0}

    async def flaky_end_round(self, round_id, llm_raw, error=""):
        """首次调用抛普通异常（真实闭合不发生），其后调用转真实闭合。

        参数：
            self: AuditTrail，审计溯源实例
            round_id: str，待结束的审计轮次编号
            llm_raw: str | None，本轮 LLM 原始输出
            error: str，需要记录的错误文本

        返回：
            None：首次调用抛错，后续调用委托真实闭合

        异常：
            RuntimeError：首次调用时模拟数据库抖动导致闭合失败
        """
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("审计闭合抖动")
        state["closed"] += 1
        await real_end_round(self, round_id, llm_raw, error)

    monkeypatch.setattr(AuditTrail, "end_round", flaky_end_round)
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _CausalLinkProvider(), tmp_path, events.append)
    result = await agent.run(report_type="us")

    assert result["ok"] is True
    items, total = await repo.research.list_reports_page(10, 0)
    assert total == 1 and items[0].error == ""  # 同一 round 只有成功研报一份，无失败报告双写
    links = await repo.research.list_causal_links()
    assert len(links) == 1 and links[0].report_id == result["report_id"]  # 因果链不丢
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None and audit_round.ended_at is not None
    assert audit_round.error == ""  # 审计轮以成功语义闭合
    assert state["closed"] == 1  # 真实闭合仅发生一次（由补全收尾完成）
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[-1]["data"] == {"round_id": audit_round.round_id, "ok": True}


async def test_prompt_load_failure_lands_failed_report(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提示词加载抛错（begin_round 前的初始化步骤）：ok=False 且失败报告落库，不向上抛。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，破坏提示词加载

    返回：
        None，断言失败报告落库、未开审计轮、零事件
    """
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path, events.append)

    def _broken_prompt(self, tool_docs: str):
        """模拟提示词加载失败（类级补丁，首参为实例）。

        参数：
            self: ResearchPromptLoader，提示词加载器实例（本桩不使用）
            tool_docs: str，渲染后的工具说明文本（本桩不使用）

        返回：
            tuple[str, str]，永不返回；固定抛错

        异常：
            RuntimeError：模拟提示词文件缺失
        """
        raise RuntimeError("提示词文件缺失")

    monkeypatch.setattr(ResearchPromptLoader, "system_prompt", _broken_prompt)
    result = await agent.run()
    assert result["ok"] is False and "提示词文件缺失" in result["error"]
    items, total = await repo.research.list_reports_page(10, 0)
    assert total == 1
    assert items[0].error == "RuntimeError: 提示词文件缺失"
    assert items[0].round_id == ""  # begin_round 前失败：无审计轮关联
    assert await repo.latest_audit_round("paper") is None  # 未开审计轮
    assert events == []  # round_id 为空：零事件


async def test_deps_construction_failure_lands_failed_report(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """deps 构造抛错（曾在 try 外逃逸仅留日志）：ok=False 且失败报告落库，不向上抛。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，破坏工具依赖装配

    返回：
        None，断言失败报告落库、未开审计轮、零事件
    """
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path, events.append)

    def _broken_deps(**kwargs):
        """模拟工具依赖装配失败。

        参数：
            kwargs: dict，依赖装配参数（本桩不使用）

        返回：
            ResearchToolDeps，永不返回；固定抛错

        异常：
            RuntimeError：模拟数据源装配失败
        """
        raise RuntimeError("数据源装配失败")

    monkeypatch.setattr("src.research.agent.ResearchToolDeps", _broken_deps)
    result = await agent.run()
    assert result["ok"] is False and "数据源装配失败" in result["error"]
    items, total = await repo.research.list_reports_page(10, 0)
    assert total == 1
    assert items[0].error == "RuntimeError: 数据源装配失败"
    assert await repo.latest_audit_round("paper") is None  # 未开审计轮
    assert events == []  # round_id 为空：零事件


async def test_preinjection_base_exception_group_closes_round(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预注入抛 BaseExceptionGroup（含 CancelledError，非 Exception 子类）：兜底收尾轮次必闭合。

    修复前 run() 只有 CancelledError 与 Exception 两个分支：BaseExceptionGroup
    （成员为 CancelledError 时不归 Exception）漏网即打穿收尾——无失败报告、
    审计轮 ended_at 永久为 null（孤儿轮）、异常冲出点火方。BaseException 兜底后
    与 Exception 同口径：_fail 落失败报告 + end_round 闭合 + 轮末 ok=False 事件。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，替换预注入注入漏网 BaseException

    返回：
        None，断言失败报告带轮次编号、审计轮闭合、事件序列完整、不向上抛
    """
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path, events.append)

    async def _broken_preinject(deps, hours):
        """模拟预注入数据层抛含取消的异常组（anyio 混合失败的最坏形态）。

        参数：
            deps: ResearchToolDeps，工具依赖（本桩不使用）
            hours: int，回溯小时数（本桩不使用）

        返回：
            str，永不返回；固定抛 BaseExceptionGroup

        异常：
            BaseExceptionGroup：成员为 CancelledError，except Exception 接不住
        """
        raise BaseExceptionGroup("数据层混合失败", [asyncio.CancelledError("传输中止")])

    monkeypatch.setattr("src.research.agent.build_preinjection", _broken_preinject)
    result = await agent.run(report_type="asia")
    assert result["ok"] is False and "数据层混合失败" in result["error"]
    items, total = await repo.research.list_reports_page(10, 0)
    assert total == 1
    assert items[0].error.startswith("BaseExceptionGroup: 数据层混合失败")
    assert items[0].round_id != ""  # begin_round 后失败：失败报告带真实轮次编号
    audit_round = await repo.get_audit_round(items[0].round_id)
    assert audit_round is not None and audit_round.ended_at is not None  # 轮次闭合，不孤儿
    assert audit_round.error.startswith("BaseExceptionGroup:")
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[-1]["data"] == {"round_id": items[0].round_id, "ok": False}


# ---------- 成功落库后被打断的补全收尾（_complete_interrupted） ----------


async def test_snapshot_write_failure_is_non_fatal(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审计 JSON 快照写盘失败（OSError）降级为日志：run 成功、审计轮成功闭合不改写。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，向 AuditTrail._write_snapshot 注入 OSError

    返回：
        None，断言 run 成功、单份成功研报、审计轮以成功闭合
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
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
    items, total = await repo.research.list_reports_page(10, 0)
    assert total == 1 and items[0].error == ""
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None
    assert audit_round.ended_at is not None and audit_round.error == ""  # 已提交结果不被反转


async def test_cancel_during_causal_flush_replays_remaining_links(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消掐在因果链 flush 中途（第 1 条 save）：补全收尾断点续传，两条链都落库。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，向 save_causal_link 注入一次性取消

    返回：
        None，断言取消传播、双链落库、单份成功研报、审计成功闭合与 ok=True 收尾
    """

    class _TwoChainsProvider(_SequentialProvider):
        """第一轮提交两条因果链 + 取行情，第二轮输出合法 JSON。"""

        async def chat(self, system, messages, tools):
            """返回该轮测试预设的模型响应。

            参数：
                self: _TwoChainsProvider，当前测试替身实例
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表
            返回：
                LLMResponse，返回该测试辅助函数构造或记录的结果
            """
            from src.agent.providers.base import LLMResponse, ToolCall

            self._calls += 1
            if self._calls == 1:
                return LLMResponse(
                    text="两条链",
                    tool_calls=[
                        ToolCall(
                            "submit_causal_links",
                            {
                                "chain": [{"node": "a1"}, {"node": "b1"}],
                                "confidence": 0.6,
                                "topic": "关税",
                            },
                        ),
                        ToolCall(
                            "submit_causal_links",
                            {
                                "chain": [{"node": "a2"}, {"node": "b2"}],
                                "confidence": 0.7,
                                "topic": "非农",
                            },
                        ),
                        ToolCall("get_research_market_data", {"contract": "BTC_USDT"}),
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
    state = {"cancelled": False}

    async def cancelling_save(**kwargs):
        """首次调用抛取消（该条不落库、队首保留），其后调用转真实落库。

        参数：
            **kwargs: dict[str, object]，透传给真实 save_causal_link 的关键字参数

        返回：
            CausalLink，后续调用委托真实落库返回的因果链

        异常：
            asyncio.CancelledError：首次调用时模拟外部取消
        """
        if not state["cancelled"]:
            state["cancelled"] = True
            raise asyncio.CancelledError()
        return await real_save(**kwargs)

    monkeypatch.setattr(repo.research, "save_causal_link", cancelling_save)
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _TwoChainsProvider(), tmp_path, events.append)
    task = asyncio.create_task(agent.run(report_type="us"))
    with pytest.raises(asyncio.CancelledError):
        await task

    links = await repo.research.list_causal_links()
    assert len(links) == 2  # 补全重放：剩余两条链都落库
    items, total = await repo.research.list_reports_page(10, 0)
    assert total == 1 and items[0].error == ""  # 无失败报告双写
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None and audit_round.ended_at is not None
    assert audit_round.error == ""  # 审计轮以成功语义闭合
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[-1]["data"] == {"round_id": audit_round.round_id, "ok": True}


async def test_cancel_in_begin_round_commit_window_claims_round(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消掐在 begin_round「COMMIT 已执行、await 未返回」窗口：认领预分配轮，失败收尾正常闭合审计。

    monkeypatch begin_round 为 fake：先调真实方法真实建轮（COMMIT 已执行）、再抛
    CancelledError（模拟 await 未返回即被取消、局部 round_id 仍 ""）。修复前 _fail
    因 round_id 为空只落失败报告、不 end_round：审计轮 ended_at 永久为 null 且无
    轮末事件；修复后按预分配编号反查认领，失败报告 + end_round + ok=False 轮末事件
    齐全（begin_round 未正常返回，故无轮始事件）。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，替换 AuditTrail.begin_round 注入提交后取消

    返回：
        None，断言取消传播、恰好一份失败报告、审计轮以取消错误闭合、轮末 ok=False 事件
    """
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path, events.append)
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
    task = asyncio.create_task(agent.run(report_type="us", round_id="pre-research-1"))
    with pytest.raises(asyncio.CancelledError):
        await task

    items, total = await repo.research.list_reports_page(10, 0)
    assert total == 1  # 恰好一份失败报告（带预分配轮次编号），无重复落库
    assert items[0].error == "CancelledError: 研报被取消"
    assert items[0].round_id == "pre-research-1"
    audit_round = await repo.get_audit_round("pre-research-1")
    assert audit_round is not None and audit_round.ended_at is not None  # 认领后正常闭合
    assert audit_round.error == "CancelledError: 研报被取消"
    # begin_round 未返回故无轮始事件；轮末 ok=False 事件由认领后的 _fail 补发
    assert [e["type"] for e in events] == ["research_round"]
    assert events[0]["data"] == {"round_id": "pre-research-1", "ok": False}


async def test_save_post_commit_exception_recovers_success(
    repo: Repo, settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功报告 COMMIT 后、保存函数未返回时抛普通异常：反查识出已提交，按成功语义收尾不双写。

    monkeypatch save_report_bundle 为一次性 fake：先调真实方法真实提交、再抛
    RuntimeError（模拟 COMMIT 已执行、返回前失败、report_id 仍 None）。修复前
    except Exception 分支会经 _fail 再写一份失败报告；修复后与取消同口径反查，
    按成功语义补全收尾并返回成功结果。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，替换 save_report_bundle 注入提交后普通异常

    返回：
        None，断言恰好一份成功报告、因果链补落库、审计成功闭合、返回成功语义结果
    """
    events: list[dict] = []
    agent = await _build_agent(repo, settings, _CausalLinkProvider(), tmp_path, events.append)
    real_save = repo.research.save_report_bundle
    state = {"fired": False}

    async def committed_then_raise(**kwargs):
        """首次调用真实提交成功后抛普通异常（模拟 COMMIT 已执行、保存函数未返回的窗口）。

        参数：
            kwargs: dict，save_report_bundle 的关键字参数，原样透传真实方法

        返回：
            tuple：首次调用不返回（抛普通异常）；其后调用委托真实方法返回落库结果

        异常：
            RuntimeError：首次调用真实提交后抛出，模拟 post-commit 失败
        """
        if not state["fired"]:
            state["fired"] = True
            await real_save(**kwargs)
            raise RuntimeError("post-commit failure")
        return await real_save(**kwargs)

    monkeypatch.setattr(repo.research, "save_report_bundle", committed_then_raise)
    result = await agent.run(report_type="us")

    assert result["ok"] is True
    items, total = await repo.research.list_reports_page(10, 0)
    assert total == 1  # 成功报告已提交被反查识别：无失败报告双写
    assert items[0].error == ""
    assert result["report_id"] == items[0].id
    assert result["asset_count"] == 1  # 由报告 raw_json 的 asset_views 推导
    links = await repo.research.list_causal_links()
    assert len(links) == 1 and links[0].report_id == items[0].id  # 取消窗口同口径补落库
    audit_round = await repo.latest_audit_round("paper")
    assert audit_round is not None and audit_round.ended_at is not None
    assert audit_round.error == ""  # 审计轮以成功语义闭合
    assert result["round_id"] == audit_round.round_id
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[-1]["data"] == {"round_id": audit_round.round_id, "ok": True}


async def test_run_records_research_prompt_md5(repo: Repo, settings: Settings, tmp_path) -> None:
    """研报落库记录所用提示词正文 md5：与版本表同口径，供复盘按版本归因（issue #113）。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言报告头 research_prompt_md5 等于正文内容的 md5
    """
    import hashlib

    content = "研报提示词正文：" + "先事实后判断，逐标的给结论。" * 10
    (tmp_path / "research_prompt.md").write_text(content, encoding="utf-8")
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
    report = await repo.research.latest_report()
    assert report is not None
    expected = hashlib.md5(content.encode("utf-8")).hexdigest()
    assert report.research_prompt_md5 == expected
    assert report.research_prompt_version_id is None  # 版本表无该 md5 的 applied 版本


async def test_run_records_prompt_version_id_resolved_at_build_time(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """R5-4：落库版本 id 为构建 prompt 时点该 md5 的最新 applied 版本（draft 不命中）。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言报告头 research_prompt_version_id 精确指向目标版本
    """
    import hashlib

    content = "研报提示词正文：" + "先事实后判断，逐标的给结论。" * 10
    (tmp_path / "research_prompt.md").write_text(content, encoding="utf-8")
    md5 = hashlib.md5(content.encode("utf-8")).hexdigest()
    await repo.research_prompt.save_version("无关旧版本", "aa" * 16, "human", "干扰项")
    target = await repo.research_prompt.save_version(content, md5, "human", "目标版本")
    await repo.research_prompt.save_version(
        content, md5, "review_agent", "同文草稿", status="draft"
    )
    agent = await _build_agent(repo, settings, _SequentialProvider(), tmp_path)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
    report = await repo.research.latest_report()
    assert report is not None
    assert report.research_prompt_md5 == md5
    assert report.research_prompt_version_id == target.id  # 草稿不命中，取最新 applied


async def test_run_records_prompt_md5_sampled_at_build_time(
    repo: Repo, settings: Settings, tmp_path
) -> None:
    """回归（审查 P2-2）：运行途中提示词被热替换，落库 md5 仍为构建 prompt 时的正文版本。

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言报告头 research_prompt_md5 等于原文 md5 而非热替换后的新文 md5
    """
    import hashlib
    import os
    import time

    original = "研报提示词正文：" + "先事实后判断，逐标的给结论。" * 10
    prompt_file = tmp_path / "research_prompt.md"
    prompt_file.write_text(original, encoding="utf-8")

    class _HotSwapProvider(_SequentialProvider):
        """首轮调用前热替换提示词文件（模拟运行途中人工保存），随后按序返回预设响应。"""

        async def chat(self, system: str, messages: list[dict], tools: list[dict]):
            """首轮先热替换提示词文件（mtime 显式拨快），再交回父类按序响应。

            参数：
                system: str，系统提示词
                messages: list[dict]，对话消息列表
                tools: list[dict]，工具定义列表

            返回：
                LLMResponse，父类预设的模型响应
            """
            if self._calls == 0:
                prompt_file.write_text(
                    "被热替换的新提示词：" + "另一套纪律。" * 20, encoding="utf-8"
                )
                future = time.time() + 5  # 显式拨快 mtime，确保缓存判定感知替换
                os.utime(prompt_file, (future, future))
            return await super().chat(system, messages, tools)

    agent = await _build_agent(repo, settings, _HotSwapProvider(), tmp_path)
    result = await agent.run(report_type="us")
    assert result["ok"] is True
    report = await repo.research.latest_report()
    assert report is not None
    assert report.research_prompt_md5 == hashlib.md5(original.encode("utf-8")).hexdigest()
