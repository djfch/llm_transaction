"""研报子仓库测试：三表读写、dedup 幂等、时间过滤、失败研报过滤。

数据库用 tmp_path 隔离，不触真实数据文件。
"""

from __future__ import annotations

from datetime import date

import pytest

from src.memory import Database, Repo
from tests.research_helpers import save_report_fixture


@pytest.fixture
async def repo(tmp_path) -> Repo:
    """创建测试数据库仓库并在用例结束后关闭连接。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        Repo，返回该测试辅助函数构造或记录的结果
    """
    db = Database()
    await db.open(tmp_path / "research.db")
    return Repo(db)


def _item(source: str, title: str, ts: float, dedup: str) -> dict:
    """构造研报时间线测试条目。

    参数：
        source: str，唤醒或成交来源
        title: str，时间线条目标题
        ts: float，事件时间戳
        dedup: str，时间线去重键
    返回：
        dict，返回该测试辅助函数构造或记录的结果
    """
    return {
        "source": source,
        "kind": "flash",
        "title": title,
        "url": "",
        "published_at": ts,
        "meta_json": "{}",
        "dedup_key": dedup,
        "fetched_at": ts,
    }


async def test_append_timeline_dedup_idempotent(repo: Repo) -> None:
    """同 dedup_key 重复追加只插入一次（幂等），返回新插入条数正确。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    item = _item("jin10", "美联储决议", 1000.0, "k1")
    inserted1 = await repo.research.append_timeline_many([item])
    inserted2 = await repo.research.append_timeline_many([item])
    assert inserted1 == 1
    assert inserted2 == 0
    rows = await repo.research.list_timeline(0.0, None)
    assert len(rows) == 1
    assert rows[0].title == "美联储决议"


async def test_append_timeline_batch_partial_dup(repo: Repo) -> None:
    """批量中部分重复：只插入新的，重复的跳过。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    items = [
        _item("jin10", "A", 1000.0, "k1"),
        _item("blockbeats", "B", 2000.0, "k2"),
        _item("jin10", "A'", 3000.0, "k1"),  # dedup 相同（视为同一条）
    ]
    inserted = await repo.research.append_timeline_many(items)
    assert inserted == 2
    rows = await repo.research.list_timeline(0.0, None)
    assert [r.title for r in rows] == ["A", "B"]


async def test_list_timeline_window(repo: Repo) -> None:
    """时间窗口过滤 [start, end)：半开区间，边界外不返回。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    await repo.research.append_timeline_many(
        [
            _item("jin10", "旧", 100.0, "k1"),
            _item("jin10", "中", 200.0, "k2"),
            _item("jin10", "新", 300.0, "k3"),
        ]
    )
    rows = await repo.research.list_timeline(200.0, 300.0)
    assert [r.title for r in rows] == ["中"]
    rows = await repo.research.list_timeline(200.0, 300.0, limit=0)
    assert rows == []


async def test_latest_dedup_keys(repo: Repo) -> None:
    """增量定位：latest_dedup_keys 返回最近插入的 dedup 集合。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    await repo.research.append_timeline_many(
        [
            _item("jin10", "A", 1000.0, "k1"),
            _item("jin10", "B", 2000.0, "k2"),
        ]
    )
    keys = await repo.research.latest_dedup_keys()
    assert keys == {"k1", "k2"}


async def test_save_and_list_reports(repo: Repo) -> None:
    """研报落库与按天查询；失败研报（error 非空）不进列表。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    ok = await save_report_fixture(
        repo,
        report_type="us",
        direction="偏多",
        confidence="高",
        horizon="当日",
        evidence_json='[{"point": "ETF 流入"}]',
        narrative="美盘前瞻",
    )
    fail = await save_report_fixture(
        repo, report_type="asia", direction="中性", confidence="中", error="解析失败"
    )
    assert ok.id > 0
    assert fail.id > ok.id
    reports = await repo.research.list_reports(days=7)
    assert len(reports) == 1
    assert reports[0].id == ok.id
    views = await repo.research.list_asset_views_by_report(ok.id)
    assert len(views) == 1 and views[0].direction == "偏多"
    latest = await repo.research.latest_report()
    assert latest is not None and latest.id == ok.id


async def test_save_and_list_causal_links(repo: Repo) -> None:
    """因果链落库：默认 pending 状态，按 report_id 关联。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    report = await save_report_fixture(repo, report_type="us", direction="看空", confidence="高")
    link = await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "油价上涨", "kind": "事件"}]',
        confidence=0.7,
        evidence_json='["金十"]',
    )
    assert link.status == "pending"
    links = await repo.research.list_causal_links(days=7)
    assert len(links) == 1
    assert links[0].report_id == report.id
    assert "油价上涨" in links[0].chain_json


async def test_save_causal_link_versioning_fields(repo: Repo) -> None:
    """版本化字段落库：topic/supersedes_id/await_verification 存取一致。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    report = await save_report_fixture(repo, report_type="us", direction="看空", confidence="高")
    link = await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "a"}]',
        confidence=0.6,
        topic="关税",
        supersedes_id=3,
        await_verification=False,
    )
    assert link.topic == "关税"
    assert link.supersedes_id == 3
    assert link.await_verification is False
    got = await repo.research.get_causal_link(link.id)
    assert got is not None
    assert got.topic == "关税" and got.supersedes_id == 3 and got.await_verification is False
    # 缺省口径：无 supersedes_id / 未传 await_verification → 非修正版、按待验证
    plain = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "b"}]', confidence=0.5, topic="关税"
    )
    assert plain.supersedes_id is None
    assert plain.await_verification is True


async def test_save_causal_link_supersede_marks_old(repo: Repo) -> None:
    """版本化事务：新链替代旧链时，同一次落库把旧链 status 标记 superseded。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    report = await save_report_fixture(repo, report_type="us", direction="看空", confidence="高")
    v1 = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "旧推断"}]', confidence=0.5, topic="非农"
    )
    v2 = await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "修正后推断"}]',
        confidence=0.7,
        topic="非农",
        supersedes_id=v1.id,
    )
    old = await repo.research.get_causal_link(v1.id)
    assert old is not None and old.status == "superseded"  # 旧链已盖章
    assert v2.supersedes_id == v1.id
    # 族谱：两条都在，旧链保留留档
    links = await repo.research.list_causal_links(days=7, topic="非农")
    assert [link.id for link in links] == [v1.id, v2.id]


async def test_get_causal_link_missing(repo: Repo) -> None:
    """get_causal_link：不存在的 id 返回 None。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    assert await repo.research.get_causal_link(999) is None


async def test_list_pending_causal_links(repo: Repo) -> None:
    """未闭合池口径：只收 待验证声明 + 未被替代；排除 结论链/已被替代；按时间正序取前 N。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    report = await save_report_fixture(repo, report_type="us", direction="看空", confidence="高")
    p1 = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "a"}]', confidence=0.6, topic="关税"
    )  # 待验证（默认）
    await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "b"}]',
        confidence=0.7,
        topic="关税",
        await_verification=False,  # 结论链 → 不进池
    )
    p2 = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "c"}]', confidence=0.8, topic="非农"
    )
    superseded = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "d"}]', confidence=0.4, topic="关税"
    )
    replacer = await repo.research.save_causal_link(
        report_id=report.id,
        chain_json='[{"node": "e"}]',
        confidence=0.9,
        topic="关税",
        supersedes_id=superseded.id,  # 替代 → superseded 不进池，新链进池
    )
    pending = await repo.research.list_pending_causal_links(limit=10)
    assert [link.id for link in pending] == [p1.id, p2.id, replacer.id]
    assert all(link.status == "pending" for link in pending)
    assert all(link.await_verification for link in pending)
    assert superseded.id not in [link.id for link in pending]
    # limit 截取：只取最新 2 条（正序返回）
    top2 = await repo.research.list_pending_causal_links(limit=2)
    assert [link.id for link in top2] == [p2.id, replacer.id]


async def test_list_causal_links_topic_filter(repo: Repo) -> None:
    """按主题过滤：只返回该主题链；limit 截取最新 N 条按时间正序。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    report = await save_report_fixture(repo, report_type="us", direction="看空", confidence="高")
    a1 = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "a1"}]', confidence=0.6, topic="关税"
    )
    b1 = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "b1"}]', confidence=0.6, topic="非农"
    )
    a2 = await repo.research.save_causal_link(
        report_id=report.id, chain_json='[{"node": "a2"}]', confidence=0.7, topic="关税"
    )
    all_links = await repo.research.list_causal_links(days=7)
    assert [link.id for link in all_links] == [a1.id, b1.id, a2.id]
    tariff = await repo.research.list_causal_links(days=7, topic="关税")
    assert [link.id for link in tariff] == [a1.id, a2.id]
    # limit 截取：最新 1 条
    top1 = await repo.research.list_causal_links(days=7, limit=1)
    assert [link.id for link in top1] == [a2.id]


async def test_list_reports_page(repo: Repo) -> None:
    """分页：最新在前、含失败记录、越界页 items 空但 total 准确。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    ids = []
    for i in range(3):
        r = await save_report_fixture(
            repo, report_type="us", direction="偏多", confidence="高", narrative=f"第{i}份"
        )
        ids.append(r.id)
    fail = await save_report_fixture(
        repo, report_type="asia", direction="中性", confidence="中", error="解析失败"
    )
    # 最新在前：失败记录 id 最大，第一页第一条就是它
    page1, total = await repo.research.list_reports_page(limit=2, offset=0)
    assert [r.id for r in page1] == [fail.id, ids[2]]
    assert total == 4
    page2, total = await repo.research.list_reports_page(limit=2, offset=2)
    assert [r.id for r in page2] == [ids[1], ids[0]]
    assert total == 4
    page3, total = await repo.research.list_reports_page(limit=2, offset=4)
    assert page3 == []
    assert total == 4


async def test_list_causal_links_by_report(repo: Repo) -> None:
    """按研报取因果链：id 正序，只返回该研报的。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    r1 = await save_report_fixture(repo, report_type="us", direction="看空", confidence="高")
    r2 = await save_report_fixture(repo, report_type="asia", direction="中性", confidence="中")
    for i in range(2):
        await repo.research.save_causal_link(
            report_id=r1.id, chain_json=f'[{{"node": "A{i}", "kind": "事件"}}]', confidence=0.6
        )
    await repo.research.save_causal_link(
        report_id=r2.id, chain_json='[{"node": "B", "kind": "事件"}]', confidence=0.6
    )
    links = await repo.research.list_causal_links_by_report(r1.id)
    assert [link.report_id for link in links] == [r1.id, r1.id]
    assert [link.id for link in links] == sorted(link.id for link in links)  # id 正序
    assert all("A" in link.chain_json for link in links)


async def test_has_report_since(repo: Repo) -> None:
    """幂等判定：恰好等于 since_ts 算有；成功或失败都算已跑（失败不自动重试）；类型不匹配不算。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    ok = await save_report_fixture(repo, report_type="us", direction="偏多", confidence="高")
    fail = await save_report_fixture(
        repo, report_type="asia", direction="中性", confidence="中", error="解析失败"
    )
    assert await repo.research.has_report_since("us", ok.created_at) is True  # 恰好等于
    assert await repo.research.has_report_since("us", ok.created_at + 1) is False  # 之后无
    assert await repo.research.has_report_since("asia", fail.created_at) is True  # 失败也算已跑
    assert await repo.research.has_report_since("europe", ok.created_at) is False  # 类型不匹配


async def test_claim_schedule_run_is_unique_per_scheduled_date(repo: Repo) -> None:
    """同一调度同一计划日期只能认领一次，不同日期可分别认领。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言数据库唯一约束提供调度幂等语义
    """
    scheduled = date(2026, 8, 17)
    assert await repo.research.claim_schedule_run("asia_open", scheduled) is True
    assert await repo.research.claim_schedule_run("asia_open", scheduled) is False
    assert await repo.research.claim_schedule_run("asia_open", date(2026, 8, 18)) is True


async def test_latest_research_audit_round(repo: Repo) -> None:
    """latest_research_audit_round：只取 wake_source='research' 的最新一轮，交易轮不参与。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    assert await repo.research.latest_research_audit_round("paper") is None  # 空表
    await repo.start_audit_round("r-t1", "paper", wake_source="timer", started_at=1000.0)
    await repo.start_audit_round("r-r1", "paper", wake_source="research", started_at=2000.0)
    await repo.start_audit_round("r-r2", "paper", wake_source="research", started_at=3000.0)
    await repo.start_audit_round("r-t2", "paper", wake_source="timer", started_at=4000.0)
    latest = await repo.research.latest_research_audit_round("paper")
    assert latest is not None and latest.round_id == "r-r2"
    assert latest.wake_source == "research"
    await repo.start_audit_round("r-t3", "testnet", wake_source="timer", started_at=5000.0)
    assert await repo.research.latest_research_audit_round("testnet") is None  # 只有交易轮 → None
    assert await repo.research.latest_research_audit_round("live") is None  # 该模式无记录
