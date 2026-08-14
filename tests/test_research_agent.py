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

    参数：
        repo: Repo，测试数据库仓库
        settings: Settings，测试配置
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言取消传播、审计收尾和 rejected 响应同时成立
    """
    inner = _RejectThenHangProvider()
    provider = RetryingProvider(inner, max_attempts=2, backoff=())
    agent = await _build_agent(repo, settings, provider, tmp_path)
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
    assert links[0].status == "pending"
    assert links[0].topic == "油价"  # topic 透传
    assert links[0].await_verification is True  # 默认待验证
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
    assert report is not None and report.schema_version == 2
    views = await repo.research.list_asset_views_by_report(report.id)
    assert [view.contract for view in views] == ["BTC_USDT", "ETH_USDT"]
    assert json.loads(views[0].market_context_json)["contract"] == "BTC_USDT"
