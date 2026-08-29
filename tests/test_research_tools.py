"""研报工具测试：10 个工具执行、参数校验、数据源失败哨兵、预注入组装。

数据源全部用假实现（不触网络）；repo 用 tmp_path SQLite。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime

import pytest

from src.memory import Database, Repo
from tests.research_helpers import save_report_fixture
from src.research.preinject import build_preinjection
from src.research.providers.base import (
    CalendarEvent,
    FlashItem,
    ResearchDataProvider,
    ResearchSourceError,
)
from src.research.providers.jin10 import BEIJING_TZ
from src.research.tool_handlers import ResearchToolDeps
from src.research.tools import ResearchToolRegistry


@pytest.fixture
async def repo(tmp_path) -> AsyncIterator[Repo]:
    """创建隔离数据库的仓储夹具。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        AsyncIterator[Repo]：yield 连接 research.db 的仓储，并在夹具收尾关闭数据库
    """
    db = Database()
    await db.open(tmp_path / "research.db")
    try:
        yield Repo(db)
    finally:
        await db.close()


class _FakeJin10:
    async def fetch_calendar(self):
        # 事件日期按北京时区动态生成（复审 #6 修复）：写死日期会在跨天后被
        # 今日/明日过滤器排空，测试次日必红
        """返回当天一条五星事件和一条低星事件供过滤测试。

        参数：
            无

        返回：
            list[CalendarEvent]：包含高星与低星事件的模拟财经日历
        """
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        return [
            CalendarEvent(
                title="美国7月非农就业人口",
                pub_time=f"{today} 20:30",
                star=5,
                actual="",
                consensus="8.3",
                previous="5.7",
                affect_txt="未公布",
            ),
            CalendarEvent(
                title="低星事件",
                pub_time=f"{today} 09:00",
                star=1,
                actual="",
                consensus="",
                previous="",
                affect_txt="",
            ),
        ]

    async def fetch_flash(self, hours=24):
        """返回一条固定的金十快讯。

        参数：
            hours: int，回溯小时数

        返回：
            list[FlashItem]：包含摘要与全文的金十模拟快讯
        """
        return [
            FlashItem(
                id="j1",
                source="jin10",
                title="金十新闻",
                summary="摘要1",
                detail="全文1",
                url="",
                published_at=time.time() - 100,
            )
        ]

    async def fetch_article_detail(self, item_id):
        """返回固定的金十文章全文。

        参数：
            item_id: str，资讯条目编号

        返回：
            str：固定文本“金十详情全文”
        """
        return "金十详情全文"

    async def search_news(self, keyword, limit=20):
        """返回标题包含关键词的一条金十搜索结果。

        参数：
            keyword: str，搜索关键词
            limit: int，待校验的数量上限

        返回：
            list[FlashItem]：标题带搜索关键词的模拟新闻列表
        """
        return [
            FlashItem(
                id="j9",
                source="jin10",
                title=f"搜到{keyword}",
                summary="s",
                detail="d",
                url="",
                published_at=time.time() - 50,
            )
        ]


class _FakeBb:
    async def fetch_flash(self, hours=24):
        """返回一条固定的律动快讯。

        参数：
            hours: int，回溯小时数

        返回：
            list[FlashItem]：包含摘要与全文的律动模拟快讯
        """
        return [
            FlashItem(
                id="b1",
                source="blockbeats",
                title="律动新闻",
                summary="摘要2",
                detail="全文2",
                url="",
                published_at=time.time() - 50,
            )
        ]

    async def fetch_indicators(self):
        """返回固定的 BTC ETF 净流入指标文本。

        参数：
            无

        返回：
            str：包含 BTC ETF 净流入金额的 Markdown 文本
        """
        return "## BTC ETF 净流入\n+2.1 亿美元"

    async def search_news(self, keyword, limit=20):
        """返回空的律动新闻搜索结果。

        参数：
            keyword: str，搜索关键词
            limit: int，待校验的数量上限

        返回：
            list[FlashItem]：空的模拟新闻列表
        """
        return []


@pytest.fixture
async def deps(repo: Repo) -> ResearchToolDeps:
    """组装研报工具所需的测试依赖。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        ResearchToolDeps：绑定双数据源、临时仓储、paper 模式与关注列表的工具依赖
    """
    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    return ResearchToolDeps(
        provider=provider,
        repo=repo,
        mode="paper",
        watchlist_snapshot=("BTC_USDT", "ETH_USDT"),
    )


async def _run(deps, name: str, args: dict | None = None) -> str:
    """执行指定研报工具并返回文本结果。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖
        name: str，待执行的工具名称
        args: dict | None，传给工具的参数

    返回：
        str：指定研报工具执行后的文本结果
    """
    return await ResearchToolRegistry(deps).execute(name, args)


# ---------- 只读工具 ----------


async def test_fetch_calendar_filters_star(deps) -> None:
    """日历：只保留 star≥3 且今日/明日的条目。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(deps, "fetch_calendar")
    assert "美国7月非农就业人口" in text
    assert "低星事件" not in text


async def test_fetch_flash_compact(deps) -> None:
    """快讯：紧凑格式含时间/来源/标题/摘要。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(deps, "fetch_flash")
    assert "[jin10] 金十新闻" in text
    assert "[blockbeats] 律动新闻" in text
    assert "摘要1" in text


async def test_fetch_indicators(deps) -> None:
    """指标快照直通。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(deps, "fetch_indicators")
    assert "BTC ETF 净流入" in text and "+2.1" in text


async def test_get_macro_series_arg_validation(deps) -> None:
    """FRED：缺参数返回错误文本；非法 look_back 返回错误文本。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "必填" in await _run(deps, "get_macro_series", {})
    assert "参数错误" in await _run(deps, "get_macro_series", {"indicator": "cpi", "look_back": 1})


async def test_search_news(deps) -> None:
    """搜索合并去重。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(deps, "search_news", {"keyword": "美联储"})
    assert "搜到美联储" in text


async def test_read_timeline_empty(deps) -> None:
    """事实层无记录时返回提示（含默认回溯窗口文案）。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(deps, "read_timeline")
    assert "无符合条件的记录" in text and "近 7 天" in text


async def test_read_judgments_empty(deps) -> None:
    """判断层无记录时返回提示。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(deps, "read_judgments")
    assert "无研报记录" in text


async def test_unknown_tool_returns_error(deps) -> None:
    """未知工具：返回错误文本而非抛异常。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(deps, "not_a_tool")
    assert "未知工具" in text


# ---------- 写工具 ----------


async def test_submit_causal_links_staged(deps) -> None:
    """回归（H1）：合法因果链校验通过即暂存（无需 report_id），不直接落库。

    本轮研报 id 在工具循环结束后才生成，LLM 无法预知；提交先暂存 deps，
    由 agent 落研报后用代码回填 report_id。版本化：topic 必填、默认待跟踪。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [
                {"node": "油价上涨", "kind": "事件"},
                {"node": "BTC 承压", "kind": "标的结论"},
            ],
            "confidence": 0.7,
            "evidence": ["金十快讯"],
            "topic": "油价",
        },
    )
    assert "已暂存" in text
    assert len(deps.pending_causal_links) == 1
    staged = deps.pending_causal_links[0]
    assert staged["topic"] == "油价"
    assert staged["supersedes_id"] is None
    assert staged["status"] == "tracking"  # 默认待跟踪
    # 暂存 ≠ 落库：表内仍为空（等 agent 落研报后回填）
    assert await deps.repo.research.list_causal_links() == []


async def test_submit_causal_links_invalid(deps) -> None:
    """非法因果链：缺 topic/节点数/置信度校验返回错误文本，且不留暂存。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "参数错误" in await _run(
        deps, "submit_causal_links", {"chain": [{"node": "x"}], "confidence": 0.5}
    )  # 缺 topic
    assert "参数错误" in await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "a"}],
            "confidence": 0.5,
            "topic": "关税",
            "concluded": True,  # 结论链须 2-6 节点
        },
    )
    assert "参数错误" in await _run(
        deps,
        "submit_causal_links",
        {"chain": [{"node": "a"}, {"node": "b"}], "confidence": 1.5, "topic": "关税"},
    )
    assert deps.pending_causal_links == []


async def test_submit_causal_links_evidence_not_list(deps) -> None:
    """T10：evidence 非 list 被拒绝，且不留暂存。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "a"}, {"node": "b"}],
            "confidence": 0.5,
            "evidence": "不是列表",
            "topic": "关税",
        },
    )
    assert "参数错误" in text
    assert deps.pending_causal_links == []


async def test_submit_causal_links_pending_one_node_allowed(deps) -> None:
    """待跟踪中间态：1 节点半成品（事件未走完的观察）放行。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "非农数据即将公布", "kind": "事件"}],
            "confidence": 0.4,
            "topic": "非农",
        },
    )
    assert "已暂存" in text
    assert deps.pending_causal_links[0]["status"] == "tracking"


async def test_submit_causal_links_concluded_parsing(deps) -> None:
    """concluded 解析：true 字符串 → 结论链；非法值报错。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "a"}, {"node": "b"}],
            "confidence": 0.6,
            "topic": "关税",
            "concluded": "true",
        },
    )
    assert "已暂存" in text and "结论" in text
    assert deps.pending_causal_links[-1]["status"] == "concluded"
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "a"}, {"node": "b"}],
            "confidence": 0.6,
            "topic": "关税",
            "concluded": "也许",
        },
    )
    assert "参数错误" in text


async def test_submit_causal_links_await_verification_alias(deps) -> None:
    """await_verification 过渡别名：False→结论链、True→待跟踪；非法值与优先级校验。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "a"}, {"node": "b"}],
            "confidence": 0.6,
            "topic": "关税",
            "await_verification": False,
        },
    )
    assert "已暂存" in text and "结论" in text
    assert deps.pending_causal_links[-1]["status"] == "concluded"
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "a"}],
            "confidence": 0.6,
            "topic": "非农",
            "await_verification": True,
        },
    )
    assert "已暂存" in text and "待跟踪" in text
    assert deps.pending_causal_links[-1]["status"] == "tracking"
    # concluded 显式传入时优先，await_verification 被忽略
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "a"}, {"node": "b"}],
            "confidence": 0.6,
            "topic": "关税",
            "concluded": True,
            "await_verification": True,
        },
    )
    assert deps.pending_causal_links[-1]["status"] == "concluded"
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "a"}, {"node": "b"}],
            "confidence": 0.6,
            "topic": "关税",
            "await_verification": "也许",
        },
    )
    assert "参数错误" in text and "await_verification" in text


async def test_submit_causal_links_supersedes_validation(repo: Repo) -> None:
    """supersedes_id 校验：不存在/已被替代/主题不一致分别报错；合法替代通过。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    report = await save_report_fixture(repo, report_type="us", direction="偏多", confidence="高")
    v1 = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "a"}]', confidence=0.5, topic="非农"
    )
    other = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "b"}]', confidence=0.5, topic="关税"
    )
    superseded = await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "c"}]',
        confidence=0.6,
        topic="非农",
        supersedes_id=v1.id,
    )
    deps = ResearchToolDeps(
        provider=ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb()),
        repo=repo,
        mode="paper",
    )

    def _args(**extra) -> dict:
        """构造默认因果链参数并合并调用方覆盖项。

        参数：
            **extra: object，额外关键字参数

        返回：
            dict：包含两节点因果链、置信度、主题及覆盖项的工具参数
        """
        return {
            "chain": [{"node": "x"}, {"node": "y"}],
            "confidence": 0.6,
            "topic": "非农",
            **extra,
        }

    assert "不存在" in await _run(deps, "submit_causal_links", _args(supersedes_id=999))
    assert "已被替代" in await _run(deps, "submit_causal_links", _args(supersedes_id=v1.id))
    assert "主题" in await _run(deps, "submit_causal_links", _args(supersedes_id=other.id))
    assert deps.pending_causal_links == []
    text = await _run(deps, "submit_causal_links", _args(supersedes_id=superseded.id))
    assert "已暂存" in text
    assert deps.pending_causal_links[0]["supersedes_id"] == superseded.id


async def test_submit_causal_links_supersedes_shapes(repo: Repo) -> None:
    """supersedes_id 输入形态：0/负数/浮点/布尔被拒；数字字符串容错接受。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    report = await save_report_fixture(repo, report_type="us", direction="偏多", confidence="高")
    target = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "a"}]', confidence=0.5, topic="非农"
    )
    deps = ResearchToolDeps(
        provider=ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb()),
        repo=repo,
        mode="paper",
    )

    def _args(**extra) -> dict:
        """构造默认因果链参数并合并调用方覆盖项。

        参数：
            **extra: object，额外关键字参数

        返回：
            dict：包含两节点因果链、置信度、主题及覆盖项的工具参数
        """
        return {
            "chain": [{"node": "x"}, {"node": "y"}],
            "confidence": 0.6,
            "topic": "非农",
            **extra,
        }

    assert "正整数" in await _run(deps, "submit_causal_links", _args(supersedes_id=0))
    assert "正整数" in await _run(deps, "submit_causal_links", _args(supersedes_id=-3))
    assert "整数" in await _run(deps, "submit_causal_links", _args(supersedes_id=1.5))  # 浮点拒绝
    assert "整数" in await _run(deps, "submit_causal_links", _args(supersedes_id=True))  # 布尔拒绝
    assert deps.pending_causal_links == []
    text = await _run(deps, "submit_causal_links", _args(supersedes_id=str(target.id)))
    assert "已暂存" in text  # 数字字符串容错
    assert deps.pending_causal_links[0]["supersedes_id"] == target.id


async def test_submit_causal_links_same_round_double_supersede(repo: Repo) -> None:
    """同轮内两次声明替代同一旧链：第二次被拒（防双当前版进池）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    report = await save_report_fixture(repo, report_type="us", direction="偏多", confidence="高")
    target = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "a"}]', confidence=0.5, topic="非农"
    )
    deps = ResearchToolDeps(
        provider=ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb()),
        repo=repo,
        mode="paper",
    )

    def _args(**extra) -> dict:
        """构造默认因果链参数并合并调用方覆盖项。

        参数：
            **extra: object，额外关键字参数

        返回：
            dict：包含两节点因果链、置信度、主题及覆盖项的工具参数
        """
        return {
            "chain": [{"node": "x"}, {"node": "y"}],
            "confidence": 0.6,
            "topic": "非农",
            **extra,
        }

    assert "已暂存" in await _run(deps, "submit_causal_links", _args(supersedes_id=target.id))
    assert "重复替代" in await _run(deps, "submit_causal_links", _args(supersedes_id=target.id))
    assert len(deps.pending_causal_links) == 1


async def test_submit_causal_links_supersede_legacy_empty_topic(repo: Repo) -> None:
    """遗留链（topic=''，旧库迁移）可被新主题修正：空主题目标放行。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    report = await save_report_fixture(repo, report_type="us", direction="偏多", confidence="高")
    legacy = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "老链"}]', confidence=0.5, topic=""
    )
    deps = ResearchToolDeps(
        provider=ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb()),
        repo=repo,
        mode="paper",
    )
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "x"}, {"node": "y"}],
            "confidence": 0.6,
            "topic": "非农",
            "supersedes_id": legacy.id,
        },
    )
    assert "已暂存" in text
    assert deps.pending_causal_links[0]["supersedes_id"] == legacy.id


async def test_read_causal_links_arg_boundaries(deps) -> None:
    """read_causal_links 参数边界：days/limit 越界、非法类型（含布尔/浮点不截断）返回错误文本。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "参数错误" in await _run(deps, "read_causal_links", {"days": 0})
    assert "参数错误" in await _run(deps, "read_causal_links", {"days": 31})
    assert "参数错误" in await _run(deps, "read_causal_links", {"limit": 0})
    assert "参数错误" in await _run(deps, "read_causal_links", {"limit": 51})
    assert "参数错误" in await _run(deps, "read_causal_links", {"days": "x"})
    assert "参数错误" in await _run(deps, "read_causal_links", {"days": True})  # 布尔拒绝不截断
    assert "参数错误" in await _run(deps, "read_causal_links", {"days": 2.7})  # 浮点拒绝不截断
    assert "无已提交因果链" in await _run(deps, "read_causal_links", {"days": 1, "limit": 50})


async def test_submit_causal_links_concluded_shapes(deps) -> None:
    """concluded 全形态：数字 0/1、true/是/否 字符串均识别（映射 concluded/tracking）。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    cases = [(1, "concluded"), (0, "tracking"), ("true", "concluded"), ("是", "concluded")]
    for raw, expected in cases + [("否", "tracking")]:
        text = await _run(
            deps,
            "submit_causal_links",
            {
                "chain": [{"node": "a"}, {"node": "b"}],
                "confidence": 0.6,
                "topic": "关税",
                "concluded": raw,
            },
        )
        assert "已暂存" in text
        assert deps.pending_causal_links[-1]["status"] == expected


async def test_read_causal_links_empty(deps) -> None:
    """read_causal_links：无提交过链时返回提示（含主题过滤提示）。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "无已提交因果链" in await _run(deps, "read_causal_links")
    assert "无已提交因果链" in await _run(deps, "read_causal_links", {"topic": "非农"})


async def test_read_causal_links_lists_family(repo: Repo) -> None:
    """read_causal_links：列出链族（含历史版与状态标注、待跟踪/结论标记）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    report = await save_report_fixture(repo, report_type="us", direction="偏多", confidence="高")
    v1 = await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "旧推断", "kind": "推断"}]',
        confidence=0.5,
        topic="非农",
    )
    await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "修正推断"}]',
        confidence=0.7,
        topic="非农",
        supersedes_id=v1.id,
    )
    await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "关税结论"}]',
        confidence=0.6,
        topic="关税",
        status="concluded",
    )
    old = await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "远古推断"}]',
        confidence=0.4,
        topic="非农",
    )
    await repo._conn.execute(
        "UPDATE causal_links SET created_at=? WHERE id=?", (time.time() - 30 * 86400, old.id)
    )
    await repo._conn.commit()
    deps = ResearchToolDeps(
        provider=ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb()),
        repo=repo,
        mode="paper",
    )
    text = await _run(deps, "read_causal_links", {"topic": "非农", "days": 7})
    assert "已提交因果链" in text and "全历史" in text  # 指定主题查族谱不受 days 窗口限制
    assert f"[链#{v1.id}]" in text
    assert f"[链#{old.id}]" in text  # 30 天前的同主题旧链也被族谱覆盖
    assert "[非农]" in text
    assert f"替代链#{v1.id}" in text  # 修正版标注替代目标（方向：本链替代了旧链）
    assert "[已被替代]" in text  # 被替代的旧链中文标注
    assert "[待跟踪]" in text
    text_all = await _run(deps, "read_causal_links")
    assert "[关税]" in text_all and "[结论]" in text_all  # 结论链标注
    assert f"[链#{old.id}]" not in text_all  # 无主题时 days 窗口仍然生效


# ---------- 审查补齐：T9 参数边界 ----------


async def test_fetch_flash_hours_boundaries(deps) -> None:
    """T9：hours 边界——0 与 49 被拒，1 与 48 接受。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "参数错误" in await _run(deps, "fetch_flash", {"hours": 0})
    assert "参数错误" in await _run(deps, "fetch_flash", {"hours": 49})
    assert "参数错误" in await _run(deps, "fetch_flash", {"hours": "abc"})
    text = await _run(deps, "fetch_flash", {"hours": 1})
    assert "[jin10]" in text  # 1h 窗口内假数据（100 秒前）仍在


async def test_macro_series_boundaries(deps) -> None:
    """T9：look_back 边界与非法值。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "参数错误" in await _run(deps, "get_macro_series", {"indicator": "cpi", "look_back": 29})
    assert "参数错误" in await _run(
        deps, "get_macro_series", {"indicator": "cpi", "look_back": 2000}
    )
    assert "参数错误" in await _run(
        deps, "get_macro_series", {"indicator": "cpi", "look_back": "x"}
    )


# ---------- 数据源失败哨兵 ----------


class _BrokenProvider(ResearchDataProvider):
    async def fetch_calendar(self):
        """模拟经济日历数据源连接超时。

        参数：
            无

        返回：
            None：不会正常返回

        异常：
            ResearchSourceError：每次调用均抛出，用于模拟数据源连接超时
        """
        raise ResearchSourceError("连接超时")


async def test_source_failure_returns_sentinel(repo: Repo) -> None:
    """数据源失败：返回中文哨兵（不编造、不中断）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    deps = ResearchToolDeps(provider=_BrokenProvider(), repo=repo, mode="paper")
    text = await _run(deps, "fetch_calendar")
    assert "数据不可用" in text and "连接超时" in text


# ---------- 预注入组装 ----------


async def test_build_preinjection_sections(deps) -> None:
    """预注入六段齐全：日历/指标/快讯/时间线/判断史/待跟踪因果链；快讯与日历已落事实层。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await build_preinjection(deps, hours=24)
    assert "经济日历" in text
    assert "本轮白名单" in text
    assert "BTC_USDT" in text and "ETH_USDT" in text
    assert "get_research_market_data" in text
    assert "美国7月非农就业人口" in text
    assert "BTC ETF 净流入" in text  # 指标段内容
    assert "快讯" in text and "金十新闻" in text and "律动新闻" in text
    assert "事件时间线" in text and "暂无记录" in text
    assert "历史研报结论" in text and "首次研报" in text
    assert "待跟踪因果链" in text and "（暂无）" in text  # 无待跟踪链空态
    assert "近期研报复盘记录" in text  # 复盘记录段空态也在
    # 事实层已写入：日历 2 条（含低星）+ 快讯 2 条（金十/律动各一）
    rows = await deps.repo.research.list_timeline(0.0, None)
    assert len(rows) == 4
    kinds = {r.kind for r in rows}
    assert kinds == {"calendar", "flash"}
    # 回归（H1）：所有 timeline 行的 meta_json 必须是合法 JSON
    for r in rows:
        import json as _json

        meta = _json.loads(r.meta_json)
        assert isinstance(meta, dict)


async def test_build_preinjection_pending_links_section(repo: Repo) -> None:
    """预注入未闭合链段：带链 id/主题/节点链，且排除结论链与被替代链。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    report = await save_report_fixture(repo, report_type="us", direction="偏多", confidence="高")
    p1 = await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "非农数据", "kind": "事件"}, {"node": "观望"}]',
        confidence=0.6,
        topic="非农",
    )
    await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "关税结论"}]',
        confidence=0.6,
        topic="关税",
        status="concluded",  # 结论链不进池
    )
    old = await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "旧链"}]',
        confidence=0.5,
        topic="非农",
    )
    await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "新链"}]',
        confidence=0.7,
        topic="非农",
        supersedes_id=old.id,  # 替代后旧链不进池
    )
    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    deps = ResearchToolDeps(provider=provider, repo=repo, mode="paper")
    text = await build_preinjection(deps, hours=24)
    assert f"[链#{p1.id}]" in text and "[非农]" in text  # 带链 id 与主题
    assert "非农数据 → 观望" in text  # 节点链紧凑呈现
    assert "关税结论" not in text  # 结论链排除
    assert "旧链" not in text  # 被替代链排除
    assert "supersedes_id" in text  # 提示跟进修订方式


async def test_build_preinjection_recent_reviews_section(repo: Repo) -> None:
    """预注入复盘记录段：完整记录渲染、同一研报多次复盘全保留、按时间正序、上限 20 条。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    import json as _json

    now = time.time()
    outcome = {
        "data_status": "complete",
        "candles_actual": 96,
        "candles_expected": 96,
        "start_price": "100",
        "end_price": "110",
        "high": "120",
        "low": "90",
        "return_pct": "10",
        "max_up_pct": "20",
        "max_down_pct": "-10",
        "error": "",
    }
    for i in range(21):
        await repo.research_review.save_review(
            review_report_id=100 + i,
            report_id=42 if i % 2 == 0 else 43,
            contract="BTC_USDT",
            direction_relation=f"方向关系{i}",
            direction_reason=f"方向理由{i}",
            reasoning_quality="推理合理",
            reasoning_review=f"推理复核{i}",
            evidence_reviews_json=_json.dumps(
                [
                    {
                        "evidence_index": 0,
                        "fact_status": "成立",
                        "reasoning_status": "合理",
                        "explanation": f"说明{i}",
                    }
                ],
                ensure_ascii=False,
            ),
            confidence_assessment="置信度合规",
            confidence_reason=f"置信理由{i}",
            improvement_advice=f"建议{i}" if i != 20 else "建议20" + "长" * 3000,
            outcome_json=_json.dumps(outcome, ensure_ascii=False),
            created_at=now - (21 - i) * 60,
        )
    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    deps = ResearchToolDeps(provider=provider, repo=repo, mode="paper")
    text = await build_preinjection(deps, hours=24)
    assert "近期研报复盘记录（最近 20 条" in text
    assert "方向关系0" not in text  # 最旧一条被 20 条上限截掉
    assert "方向关系20" in text and "建议20" in text
    assert text.count("研报#42/BTC_USDT") == 10  # 同一研报多次复盘全保留（i=2..20 偶数）
    assert text.index("方向关系1") < text.index("方向关系20")  # 按时间正序
    # 完整记录渲染：理由、逐项依据核对与客观结果都进预注入
    assert "方向关系：方向关系1 —— 方向理由1" in text
    assert "推理质量：推理合理 —— 推理复核1" in text
    assert "置信度合规：置信度合规 —— 置信理由1" in text
    assert "依据评价：[0] 事实=成立 推理=合理：说明1" in text
    assert "客观结果：data_status=complete（K线 96/96） | 起价 100 → 止价 110" in text
    # 单条超 2000 字符截断并标注（i=20 的改进建议超长）
    assert "已截断，原文共" in text


async def test_build_preinjection_marks_manual_rereview(repo: Repo) -> None:
    """R6-5：人工重评记录在预注入复盘段首行标注替代关系与授权理由。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：断言 manual 复盘条目含「人工重评，替代复盘#N；授权理由：…」标注
    """
    await repo.research_review.save_review(
        review_report_id=100,
        report_id=42,
        contract="BTC_USDT",
        direction_relation="diverged",
        reasoning_quality="partial",
        confidence_assessment="too_high",
        review_kind="manual",
        rereview_of_id=7,
        rereview_reason="原复盘把震荡误判为背离",
    )
    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    deps = ResearchToolDeps(provider=provider, repo=repo, mode="paper")
    text = await build_preinjection(deps, hours=24)
    assert "人工重评，替代复盘#7；授权理由：原复盘把震荡误判为背离" in text


async def test_build_preinjection_review_id_namespace(repo: Repo) -> None:
    """R7-3：预注入复盘条目主标识为复盘记录自身编号，与复盘工具历史查询同命名空间。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：断言主标识「复盘#{id}（复盘报告#…）」每条恰好一次，
        人工重评的替代指向与主标识同命名空间
    """
    await repo.research_review.save_review(
        review_report_id=100,
        report_id=42,
        contract="BTC_USDT",
        direction_relation="aligned",
        reasoning_quality="sound",
        confidence_assessment="appropriate",
    )
    auto_id = (await repo.research_review.list_reviews(limit=1))[0].id
    await repo.research_review.save_review(
        review_report_id=101,
        report_id=42,
        contract="BTC_USDT",
        direction_relation="diverged",
        reasoning_quality="partial",
        confidence_assessment="too_high",
        review_kind="manual",
        rereview_of_id=auto_id,
        rereview_reason="原复盘把震荡误判为背离",
    )
    manual_id = (await repo.research_review.list_reviews(limit=2))[-1].id
    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    deps = ResearchToolDeps(provider=provider, repo=repo, mode="paper")
    text = await build_preinjection(deps, hours=24)
    # 计数带「（复盘报告#…） → 」后缀，避开「替代复盘#N」包含子串「复盘#N」的误计
    assert text.count(f"复盘#{auto_id}（复盘报告#100） → 研报#42/BTC_USDT") == 1
    assert text.count(f"复盘#{manual_id}（复盘报告#101） → 研报#42/BTC_USDT") == 1
    assert f"人工重评，替代复盘#{auto_id}；授权理由：原复盘把震荡误判为背离" in text


async def test_build_preinjection_partial_failure(repo: Repo) -> None:
    """预注入单段失败：标注不可用，其余段正常。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    deps = ResearchToolDeps(provider=provider, repo=repo, mode="paper")

    async def broken_calendar():
        """模拟预注入阶段的经济日历接口故障。

        参数：
            无

        返回：
            None：不会正常返回

        异常：
            ResearchSourceError：每次调用均抛出，用于验证单段失败降级
        """
        raise ResearchSourceError("日历接口挂了")

    provider._jin10.fetch_calendar = broken_calendar  # type: ignore[method-assign]
    text = await build_preinjection(deps, hours=24)
    assert "日历接口挂了" in text  # 失败段标注
    assert "金十新闻" in text  # 快讯段仍正常


async def test_preinject_dedup_key_second_normalized(repo: Repo) -> None:
    """回归（B 复审发现）：flash 落库 dedup_key 的时间戳按秒取整。

    与聚合器内存去重键 (int(published_at), title[:40]) 同口径；修复前
    dedup_key 带亚秒小数，commit 声称的"按秒归一化"未真正落地。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """

    class _FracFlashJin10(_FakeJin10):
        async def fetch_flash(self, hours=24):
            """返回发布时间带亚秒小数的金十快讯。

            参数：
                hours: int，回溯小时数

            返回：
                list[FlashItem]：包含一条亚秒时间戳快讯的列表
            """
            return [
                FlashItem(
                    id="j1",
                    source="jin10",
                    title="亚秒新闻",
                    summary="s",
                    detail="d",
                    url="",
                    published_at=time.time() - 100 + 0.37,
                )
            ]

    class _EmptyBb(_FakeBb):
        async def fetch_flash(self, hours=24):
            """返回空律动快讯列表以隔离去重键测试。

            参数：
                hours: int，回溯小时数

            返回：
                list[FlashItem]：空的模拟快讯列表
            """
            return []

    provider = ResearchDataProvider(jin10=_FracFlashJin10(), blockbeats=_EmptyBb())
    deps = ResearchToolDeps(provider=provider, repo=repo, mode="paper")
    await build_preinjection(deps, hours=24)
    rows = [r for r in await deps.repo.research.list_timeline(0.0, None) if r.kind == "flash"]
    assert len(rows) == 1
    ts_part = rows[0].dedup_key.split("|")[1]
    assert "." not in ts_part  # 按秒取整，无小数


# ---------- 历史时间窗与过滤（issue #113 C4） ----------


class _RecFred:
    """记录调用参数的 FRED 桩（满足 _FredLike 结构协议）。"""

    def __init__(self) -> None:
        """初始化空的调用记录列表。

        参数：无

        返回：
            None，初始化实例属性 calls
        """
        self.calls: list[tuple[str, int, float | None]] = []

    async def get_macro_series(self, indicator, look_back, end_ts=None):
        """记录调用参数并返回固定文本。

        参数：
            indicator: str，宏观指标名
            look_back: int，回溯天数
            end_ts: float | None，窗口终点时间戳

        返回：
            str：固定的宏观序列文本
        """
        self.calls.append((indicator, look_back, end_ts))
        return "宏观序列文本"


def _deps_with_fred(repo: Repo, fred: _RecFred) -> ResearchToolDeps:
    """组装带记录型 FRED 桩的研报工具依赖。

    参数：
        repo: Repo，测试数据库仓储
        fred: _RecFred，记录调用的 FRED 桩

    返回：
        ResearchToolDeps：绑定双假数据源与 FRED 桩的工具依赖
    """
    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb(), fred=fred)
    return ResearchToolDeps(provider=provider, repo=repo, mode="paper")


async def test_get_macro_series_explicit_window(repo) -> None:
    """宏观序列指定历史窗口：look_back 由窗口跨度向上取整天数推导，end_ts 透传 FRED。

    参数：
        repo: Repo，测试数据库仓储

    返回：
        None：断言窗口推导与透传参数
    """
    fred = _RecFred()
    deps = _deps_with_fred(repo, fred)
    text = await _run(
        deps, "get_macro_series", {"indicator": "cpi", "start_ts": 1_000_000, "end_ts": 1_100_000}
    )
    assert text == "宏观序列文本"
    assert fred.calls == [("cpi", 2, 1_100_000.0)]  # ceil(100000/86400)=2 天


async def test_get_macro_series_window_arg_validation(repo) -> None:
    """宏观序列窗口参数校验：end<=start、非数值、布尔均被拒且不触数据源。

    参数：
        repo: Repo，测试数据库仓储

    返回：
        None：断言三类非法输入返回参数错误且 FRED 桩零调用
    """
    fred = _RecFred()
    deps = _deps_with_fred(repo, fred)
    bad_end = await _run(
        deps, "get_macro_series", {"indicator": "cpi", "start_ts": 2000, "end_ts": 1000}
    )
    bad_type = await _run(deps, "get_macro_series", {"indicator": "cpi", "start_ts": "昨天"})
    bad_bool = await _run(deps, "get_macro_series", {"indicator": "cpi", "start_ts": True})
    assert "end_ts 须大于 start_ts" in bad_end
    assert "秒级时间戳数值" in bad_type
    assert "秒级时间戳数值" in bad_bool
    assert fred.calls == []  # 参数错误不触数据源


def _tl_item(source: str, kind: str, title: str, ts: float, dedup: str) -> dict:
    """构造事实层时间线测试条目。

    参数：
        source: str，来源（jin10/blockbeats）
        kind: str，类型（flash/calendar/indicator）
        title: str，条目标题
        ts: float，发布时间戳
        dedup: str，去重键

    返回：
        dict：append_timeline_many 可消费的条目字典
    """
    return {
        "source": source,
        "kind": kind,
        "title": title,
        "url": "",
        "published_at": ts,
        "meta_json": "{}",
        "dedup_key": dedup,
        "fetched_at": ts,
    }


async def _seed_timeline(repo: Repo) -> None:
    """造四条事实层记录：窗口内三条（两来源两类型）+ 窗口外旧闻一条。

    参数：
        repo: Repo，测试数据库仓储

    返回：
        None，写入事实层时间线
    """
    await repo.research.append_timeline_many(
        [
            _tl_item("jin10", "flash", "旧闻美联储", 100.0, "k0"),
            _tl_item("jin10", "flash", "美联储降息", 1500.0, "k1"),
            _tl_item("blockbeats", "flash", "ETF 净流入", 1600.0, "k2"),
            _tl_item("jin10", "calendar", "CPI 公布", 1700.0, "k3"),
        ]
    )


async def test_read_timeline_explicit_window_and_filters(deps) -> None:
    """事实层精确窗口与 kind/source/keyword 过滤：窗口外剔除，过滤回显进头部。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：断言窗口半开区间、各过滤维度与回显文案
    """
    await _seed_timeline(deps.repo)
    window = {"start_ts": 1000, "end_ts": 2000}
    text = await _run(deps, "read_timeline", window)
    assert "3 条" in text and "旧闻美联储" not in text  # 窗口 [start, end) 外剔除

    by_kind = await _run(deps, "read_timeline", {**window, "kind": "flash"})
    assert "2 条" in by_kind and "CPI 公布" not in by_kind and "kind=flash" in by_kind

    by_source = await _run(deps, "read_timeline", {**window, "source": "blockbeats"})
    assert "1 条" in by_source and "ETF 净流入" in by_source

    by_keyword = await _run(deps, "read_timeline", {**window, "keyword": "美联储"})
    assert "1 条" in by_keyword and "美联储降息" in by_keyword


async def test_read_timeline_window_arg_validation(deps) -> None:
    """事实层窗口与过滤参数校验：非法 kind/source、end<=start 均返回参数错误。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：断言三类非法输入的参数错误文本
    """
    assert "kind 须为" in await _run(deps, "read_timeline", {"kind": "bad"})
    assert "source 须为" in await _run(deps, "read_timeline", {"source": "x"})
    assert "end_ts 须大于 start_ts" in await _run(
        deps, "read_timeline", {"start_ts": 2000, "end_ts": 1000}
    )


async def test_read_timeline_keyword_wildcard_escaped(deps) -> None:
    """事实层关键词按字面匹配：LIKE 通配符被转义，% 不会匹配全部记录。

    参数：
        deps: ResearchToolDeps，已组装的工具依赖

    返回：
        None：断言通配符关键词查不到任何记录
    """
    await _seed_timeline(deps.repo)
    text = await _run(deps, "read_timeline", {"start_ts": 1000, "end_ts": 2000, "keyword": "%"})
    assert "无符合条件的记录" in text  # % 被转义为字面字符，不匹配任何标题
