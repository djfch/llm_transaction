"""研报子仓库测试：三表读写、dedup 幂等、时间过滤、失败研报过滤。

数据库用 tmp_path 隔离，不触真实数据文件。
"""

from __future__ import annotations

import pytest

from src.memory import Database, Repo


@pytest.fixture
async def repo(tmp_path) -> Repo:
    db = Database()
    await db.open(tmp_path / "research.db")
    return Repo(db)


def _item(source: str, title: str, ts: float, dedup: str) -> dict:
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
    """同 dedup_key 重复追加只插入一次（幂等），返回新插入条数正确。"""
    item = _item("jin10", "美联储决议", 1000.0, "k1")
    inserted1 = await repo.research.append_timeline_many([item])
    inserted2 = await repo.research.append_timeline_many([item])
    assert inserted1 == 1
    assert inserted2 == 0
    rows = await repo.research.list_timeline(0.0, None)
    assert len(rows) == 1
    assert rows[0].title == "美联储决议"


async def test_append_timeline_batch_partial_dup(repo: Repo) -> None:
    """批量中部分重复：只插入新的，重复的跳过。"""
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
    """时间窗口过滤 [start, end)：半开区间，边界外不返回。"""
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
    """增量定位：latest_dedup_keys 返回最近插入的 dedup 集合。"""
    await repo.research.append_timeline_many(
        [
            _item("jin10", "A", 1000.0, "k1"),
            _item("jin10", "B", 2000.0, "k2"),
        ]
    )
    keys = await repo.research.latest_dedup_keys()
    assert keys == {"k1", "k2"}


async def test_save_and_list_reports(repo: Repo) -> None:
    """研报落库与按天查询；失败研报（error 非空）不进列表。"""
    ok = await repo.research.save_report(
        report_type="us",
        direction="偏多",
        confidence="高",
        horizon="当日",
        evidence_json='[{"point": "ETF 流入"}]',
        narrative="美盘前瞻",
    )
    fail = await repo.research.save_report(
        report_type="asia", direction="中性", confidence="中", error="解析失败"
    )
    assert ok.id > 0
    assert fail.id > ok.id
    reports = await repo.research.list_reports(days=7)
    assert len(reports) == 1
    assert reports[0].id == ok.id
    assert reports[0].direction == "偏多"
    latest = await repo.research.latest_report()
    assert latest is not None and latest.id == ok.id


async def test_save_and_list_causal_links(repo: Repo) -> None:
    """因果链落库：默认 pending 状态，按 report_id 关联。"""
    report = await repo.research.save_report(report_type="us", direction="看空", confidence="高")
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


async def test_list_reports_page(repo: Repo) -> None:
    """分页：最新在前、含失败记录、越界页 items 空但 total 准确。"""
    ids = []
    for i in range(3):
        r = await repo.research.save_report(
            report_type="us", direction="偏多", confidence="高", narrative=f"第{i}份"
        )
        ids.append(r.id)
    fail = await repo.research.save_report(
        report_type="asia", direction="中性", confidence="中", error="解析失败"
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
    """按研报取因果链：id 正序，只返回该研报的。"""
    r1 = await repo.research.save_report(report_type="us", direction="看空", confidence="高")
    r2 = await repo.research.save_report(report_type="asia", direction="中性", confidence="中")
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
    """幂等判定：恰好等于 since_ts 算有；成功或失败都算已跑（失败不自动重试）；类型不匹配不算。"""
    ok = await repo.research.save_report(report_type="us", direction="偏多", confidence="高")
    fail = await repo.research.save_report(
        report_type="asia", direction="中性", confidence="中", error="解析失败"
    )
    assert await repo.research.has_report_since("us", ok.created_at) is True  # 恰好等于
    assert await repo.research.has_report_since("us", ok.created_at + 1) is False  # 之后无
    assert await repo.research.has_report_since("asia", fail.created_at) is True  # 失败也算已跑
    assert await repo.research.has_report_since("europe", ok.created_at) is False  # 类型不匹配


async def test_latest_research_audit_round(repo: Repo) -> None:
    """latest_research_audit_round：只取 wake_source='research' 的最新一轮，交易轮不参与。"""
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
