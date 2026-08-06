"""研报工具测试：10 个工具执行、参数校验、数据源失败哨兵、预注入组装。

数据源全部用假实现（不触网络）；repo 用 tmp_path SQLite。
"""

from __future__ import annotations

import time

import pytest

from src.memory import Database, Repo
from src.research.preinject import build_preinjection
from src.research.providers.base import (
    CalendarEvent,
    FlashItem,
    ResearchDataProvider,
    ResearchSourceError,
)
from src.research.tool_handlers import ResearchToolDeps
from src.research.tools import ResearchToolRegistry


@pytest.fixture
async def repo(tmp_path) -> Repo:
    db = Database()
    await db.open(tmp_path / "research.db")
    return Repo(db)


class _FakeJin10:
    async def fetch_calendar(self):
        return [
            CalendarEvent(
                title="美国7月非农就业人口",
                pub_time="2026-08-07 20:30",
                star=5,
                actual="",
                consensus="8.3",
                previous="5.7",
                affect_txt="未公布",
            ),
            CalendarEvent(
                title="低星事件",
                pub_time="2026-08-07 09:00",
                star=1,
                actual="",
                consensus="",
                previous="",
                affect_txt="",
            ),
        ]

    async def fetch_flash(self, hours=24):
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
        return "金十详情全文"

    async def search_news(self, keyword, limit=20):
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
        return "## BTC ETF 净流入\n+2.1 亿美元"

    async def search_news(self, keyword, limit=20):
        return []


@pytest.fixture
async def deps(repo: Repo) -> ResearchToolDeps:
    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    return ResearchToolDeps(provider=provider, repo=repo, mode="paper")


async def _run(deps, name: str, args: dict | None = None) -> str:
    return await ResearchToolRegistry(deps).execute(name, args)


# ---------- 只读工具 ----------


async def test_fetch_calendar_filters_star(deps) -> None:
    """日历：只保留 star≥3 且今日/明日的条目。"""
    text = await _run(deps, "fetch_calendar")
    assert "美国7月非农就业人口" in text
    assert "低星事件" not in text


async def test_fetch_flash_compact(deps) -> None:
    """快讯：紧凑格式含时间/来源/标题/摘要。"""
    text = await _run(deps, "fetch_flash")
    assert "[jin10] 金十新闻" in text
    assert "[blockbeats] 律动新闻" in text
    assert "摘要1" in text


async def test_fetch_indicators(deps) -> None:
    """指标快照直通。"""
    text = await _run(deps, "fetch_indicators")
    assert "BTC ETF 净流入" in text and "+2.1" in text


async def test_get_macro_series_arg_validation(deps) -> None:
    """FRED：缺参数返回错误文本；非法 look_back 返回错误文本。"""
    assert "必填" in await _run(deps, "get_macro_series", {})
    assert "参数错误" in await _run(deps, "get_macro_series", {"indicator": "cpi", "look_back": 1})


async def test_search_news(deps) -> None:
    """搜索合并去重。"""
    text = await _run(deps, "search_news", {"keyword": "美联储"})
    assert "搜到美联储" in text


async def test_read_timeline_empty(deps) -> None:
    """事实层无记录时返回提示。"""
    text = await _run(deps, "read_timeline")
    assert "无记录" in text


async def test_read_judgments_empty(deps) -> None:
    """判断层无记录时返回提示。"""
    text = await _run(deps, "read_judgments")
    assert "无研报记录" in text


async def test_unknown_tool_returns_error(deps) -> None:
    """未知工具：返回错误文本而非抛异常。"""
    text = await _run(deps, "not_a_tool")
    assert "未知工具" in text


# ---------- 写工具 ----------


async def test_submit_causal_links_valid(deps) -> None:
    """合法因果链落库成功。"""
    report = await deps.repo.research.save_report(
        report_type="us", direction="看空", confidence="高"
    )
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "report_id": report.id,
            "chain": [
                {"node": "油价上涨", "kind": "事件"},
                {"node": "BTC 承压", "kind": "标的结论"},
            ],
            "confidence": 0.7,
            "evidence": ["金十快讯"],
        },
    )
    assert "已提交" in text
    links = await deps.repo.research.list_causal_links()
    assert len(links) == 1
    assert links[0].status == "pending"


async def test_submit_causal_links_invalid(deps) -> None:
    """非法因果链：节点数/置信度/类型校验返回错误文本。"""
    assert "参数错误" in await _run(
        deps, "submit_causal_links", {"report_id": 1, "chain": [{"node": "x"}], "confidence": 0.5}
    )
    assert "参数错误" in await _run(
        deps,
        "submit_causal_links",
        {"report_id": 1, "chain": [{"node": "a"}, {"node": "b"}], "confidence": 1.5},
    )


async def test_submit_causal_links_orphan_report(deps) -> None:
    """T10：悬空 report_id（不存在）被拒绝，不落库。"""
    text = await _run(
        deps,
        "submit_causal_links",
        {"report_id": 9999, "chain": [{"node": "a"}, {"node": "b"}], "confidence": 0.5},
    )
    assert "不存在" in text
    assert await deps.repo.research.list_causal_links() == []


async def test_submit_causal_links_evidence_not_list(deps) -> None:
    """T10：evidence 非 list 被拒绝。"""
    report = await deps.repo.research.save_report(
        report_type="us", direction="看空", confidence="高"
    )
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "report_id": report.id,
            "chain": [{"node": "a"}, {"node": "b"}],
            "confidence": 0.5,
            "evidence": "不是列表",
        },
    )
    assert "参数错误" in text


# ---------- 审查补齐：T9 参数边界 ----------


async def test_fetch_flash_hours_boundaries(deps) -> None:
    """T9：hours 边界——0 与 49 被拒，1 与 48 接受。"""
    assert "参数错误" in await _run(deps, "fetch_flash", {"hours": 0})
    assert "参数错误" in await _run(deps, "fetch_flash", {"hours": 49})
    assert "参数错误" in await _run(deps, "fetch_flash", {"hours": "abc"})
    text = await _run(deps, "fetch_flash", {"hours": 1})
    assert "[jin10]" in text  # 1h 窗口内假数据（100 秒前）仍在


async def test_macro_series_boundaries(deps) -> None:
    """T9：look_back 边界与非法值。"""
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
        raise ResearchSourceError("连接超时")


async def test_source_failure_returns_sentinel(repo: Repo) -> None:
    """数据源失败：返回中文哨兵（不编造、不中断）。"""
    deps = ResearchToolDeps(provider=_BrokenProvider(), repo=repo, mode="paper")
    text = await _run(deps, "fetch_calendar")
    assert "数据不可用" in text and "连接超时" in text


# ---------- 预注入组装 ----------


async def test_build_preinjection_sections(deps) -> None:
    """预注入五段齐全：日历/指标/快讯/时间线/判断史；快讯与日历已落事实层。"""
    text = await build_preinjection(deps, hours=24)
    assert "经济日历" in text
    assert "美国7月非农就业人口" in text
    assert "BTC ETF 净流入" in text  # 指标段内容
    assert "快讯" in text and "金十新闻" in text and "律动新闻" in text
    assert "事件时间线" in text and "暂无记录" in text
    assert "历史研报结论" in text and "首次研报" in text
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


async def test_build_preinjection_partial_failure(repo: Repo) -> None:
    """预注入单段失败：标注不可用，其余段正常。"""
    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    deps = ResearchToolDeps(provider=provider, repo=repo, mode="paper")

    async def broken_calendar():
        raise ResearchSourceError("日历接口挂了")

    provider._jin10.fetch_calendar = broken_calendar  # type: ignore[method-assign]
    text = await build_preinjection(deps, hours=24)
    assert "日历接口挂了" in text  # 失败段标注
    assert "金十新闻" in text  # 快讯段仍正常
