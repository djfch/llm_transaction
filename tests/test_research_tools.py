"""研报工具测试：10 个工具执行、参数校验、数据源失败哨兵、预注入组装。

数据源全部用假实现（不触网络）；repo 用 tmp_path SQLite。
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from src.memory import Database, Repo
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
async def repo(tmp_path) -> Repo:
    db = Database()
    await db.open(tmp_path / "research.db")
    return Repo(db)


class _FakeJin10:
    async def fetch_calendar(self):
        # 事件日期按北京时区动态生成（复审 #6 修复）：写死日期会在跨天后被
        # 今日/明日过滤器排空，测试次日必红
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


async def test_submit_causal_links_staged(deps) -> None:
    """回归（H1）：合法因果链校验通过即暂存（无需 report_id），不直接落库。

    本轮研报 id 在工具循环结束后才生成，LLM 无法预知；提交先暂存 deps，
    由 agent 落研报后用代码回填 report_id。版本化：topic 必填、默认待验证。
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
    assert staged["await_verification"] is True  # 默认待验证
    # 暂存 ≠ 落库：表内仍为空（等 agent 落研报后回填）
    assert await deps.repo.research.list_causal_links() == []


async def test_submit_causal_links_invalid(deps) -> None:
    """非法因果链：缺 topic/节点数/置信度校验返回错误文本，且不留暂存。"""
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
            "await_verification": False,  # 结论链须 2-6 节点
        },
    )
    assert "参数错误" in await _run(
        deps,
        "submit_causal_links",
        {"chain": [{"node": "a"}, {"node": "b"}], "confidence": 1.5, "topic": "关税"},
    )
    assert deps.pending_causal_links == []


async def test_submit_causal_links_evidence_not_list(deps) -> None:
    """T10：evidence 非 list 被拒绝，且不留暂存。"""
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
    """待验证中间态：1 节点半成品（事件未走完的观察）放行。"""
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
    assert deps.pending_causal_links[0]["await_verification"] is True


async def test_submit_causal_links_await_verification_parsing(deps) -> None:
    """await_verification 解析：false 字符串/数字 0 → 结论链；非法值报错。"""
    text = await _run(
        deps,
        "submit_causal_links",
        {
            "chain": [{"node": "a"}, {"node": "b"}],
            "confidence": 0.6,
            "topic": "关税",
            "await_verification": "false",
        },
    )
    assert "已暂存" in text and "结论" in text
    assert deps.pending_causal_links[-1]["await_verification"] is False
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
    assert "参数错误" in text


async def test_submit_causal_links_supersedes_validation(repo: Repo) -> None:
    """supersedes_id 校验：不存在/已被替代/主题不一致分别报错；合法替代通过。"""
    report = await repo.research.save_report(report_type="us", direction="偏多", confidence="高")
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
    """supersedes_id 输入形态：0/负数/浮点/布尔被拒；数字字符串容错接受。"""
    report = await repo.research.save_report(report_type="us", direction="偏多", confidence="高")
    target = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "a"}]', confidence=0.5, topic="非农"
    )
    deps = ResearchToolDeps(
        provider=ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb()),
        repo=repo,
        mode="paper",
    )

    def _args(**extra) -> dict:
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
    """同轮内两次声明替代同一旧链：第二次被拒（防双当前版进池）。"""
    report = await repo.research.save_report(report_type="us", direction="偏多", confidence="高")
    target = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "a"}]', confidence=0.5, topic="非农"
    )
    deps = ResearchToolDeps(
        provider=ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb()),
        repo=repo,
        mode="paper",
    )

    def _args(**extra) -> dict:
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
    """遗留链（topic=''，旧库迁移）可被新主题修正：空主题目标放行。"""
    report = await repo.research.save_report(report_type="us", direction="偏多", confidence="高")
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
    """read_causal_links 参数边界：days/limit 越界、非法类型（含布尔/浮点不截断）返回错误文本。"""
    assert "参数错误" in await _run(deps, "read_causal_links", {"days": 0})
    assert "参数错误" in await _run(deps, "read_causal_links", {"days": 31})
    assert "参数错误" in await _run(deps, "read_causal_links", {"limit": 0})
    assert "参数错误" in await _run(deps, "read_causal_links", {"limit": 51})
    assert "参数错误" in await _run(deps, "read_causal_links", {"days": "x"})
    assert "参数错误" in await _run(deps, "read_causal_links", {"days": True})  # 布尔拒绝不截断
    assert "参数错误" in await _run(deps, "read_causal_links", {"days": 2.7})  # 浮点拒绝不截断
    assert "无已提交因果链" in await _run(deps, "read_causal_links", {"days": 1, "limit": 50})


async def test_submit_causal_links_await_verification_shapes(deps) -> None:
    """await_verification 全形态：数字 0/1、true/是/否 字符串均识别。"""
    for raw, expected in [(1, True), (0, False), ("true", True), ("是", True), ("否", False)]:
        text = await _run(
            deps,
            "submit_causal_links",
            {
                "chain": [{"node": "a"}, {"node": "b"}],
                "confidence": 0.6,
                "topic": "关税",
                "await_verification": raw,
            },
        )
        assert "已暂存" in text
        assert deps.pending_causal_links[-1]["await_verification"] is expected


async def test_read_causal_links_empty(deps) -> None:
    """read_causal_links：无提交过链时返回提示（含主题过滤提示）。"""
    assert "无已提交因果链" in await _run(deps, "read_causal_links")
    assert "无已提交因果链" in await _run(deps, "read_causal_links", {"topic": "非农"})


async def test_read_causal_links_lists_family(repo: Repo) -> None:
    """read_causal_links：列出链族（含历史版与状态标注、待验证/结论标记）。"""
    report = await repo.research.save_report(report_type="us", direction="偏多", confidence="高")
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
        await_verification=False,
    )
    deps = ResearchToolDeps(
        provider=ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb()),
        repo=repo,
        mode="paper",
    )
    text = await _run(deps, "read_causal_links", {"topic": "非农", "days": 7})
    assert "已提交因果链" in text
    assert f"[链#{v1.id}]" in text
    assert "[非农]" in text
    assert f"替代链#{v1.id}" in text  # 修正版标注替代目标（方向：本链替代了旧链）
    assert "[已被替代]" in text  # 被替代的旧链中文标注
    assert "[待验证]" in text
    text_all = await _run(deps, "read_causal_links")
    assert "[关税]" in text_all and "[结论]" in text_all  # 结论链标注


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
    """预注入六段齐全：日历/指标/快讯/时间线/判断史/未闭合因果链；快讯与日历已落事实层。"""
    text = await build_preinjection(deps, hours=24)
    assert "经济日历" in text
    assert "美国7月非农就业人口" in text
    assert "BTC ETF 净流入" in text  # 指标段内容
    assert "快讯" in text and "金十新闻" in text and "律动新闻" in text
    assert "事件时间线" in text and "暂无记录" in text
    assert "历史研报结论" in text and "首次研报" in text
    assert "未闭合因果链" in text and "（暂无）" in text  # 无未闭合链空态
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
    """预注入未闭合链段：带链 id/主题/节点链，且排除结论链与被替代链。"""
    report = await repo.research.save_report(report_type="us", direction="偏多", confidence="高")
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
        await_verification=False,  # 结论链不进池
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


async def test_preinject_dedup_key_second_normalized(repo: Repo) -> None:
    """回归（B 复审发现）：flash 落库 dedup_key 的时间戳按秒取整。

    与聚合器内存去重键 (int(published_at), title[:40]) 同口径；修复前
    dedup_key 带亚秒小数，commit 声称的"按秒归一化"未真正落地。
    """

    class _FracFlashJin10(_FakeJin10):
        async def fetch_flash(self, hours=24):
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
            return []

    provider = ResearchDataProvider(jin10=_FracFlashJin10(), blockbeats=_EmptyBb())
    deps = ResearchToolDeps(provider=provider, repo=repo, mode="paper")
    await build_preinjection(deps, hours=24)
    rows = [r for r in await deps.repo.research.list_timeline(0.0, None) if r.kind == "flash"]
    assert len(rows) == 1
    ts_part = rows[0].dedup_key.split("|")[1]
    assert "." not in ts_part  # 按秒取整，无小数
