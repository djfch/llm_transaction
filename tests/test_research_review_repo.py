"""研报复盘子仓库测试：候选到期过滤、已复盘排除、案例取数与复盘记录 CRUD。

数据库用 tmp_path 隔离，不触真实数据文件；created_at 回拨模拟到期场景。
R5-2 增：人工重评授权登记幂等、待消费生命周期与 manual 三列落库读回。
"""

from __future__ import annotations

import time

import pytest

from src.memory import Database, Repo
from tests.research_helpers import save_report_fixture


@pytest.fixture
async def repo(tmp_path) -> Repo:
    """创建测试数据库仓库（连接随临时目录销毁）。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        Repo：绑定临时数据库的存取层
    """
    db = Database()
    await db.open(tmp_path / "research_review.db")
    return Repo(db)


async def _backdate(repo: Repo, report_id: int, seconds_ago: float) -> None:
    """把研报及其逐标的结论的创建时间回拨到过去（模拟窗口到期）。

    参数：
        repo: Repo，存取层
        report_id: int，研报编号
        seconds_ago: float，回拨秒数

    返回：
        None，就地更新数据库两行 created_at
    """
    ts = time.time() - seconds_ago
    await repo._conn.execute("UPDATE research_reports SET created_at=? WHERE id=?", (ts, report_id))
    await repo._conn.execute(
        "UPDATE research_asset_views SET created_at=? WHERE report_id=?", (ts, report_id)
    )
    await repo._conn.commit()


async def test_candidates_filter_by_horizon_maturity(repo: Repo) -> None:
    """候选只含 horizon 合法且窗口已到期的逐标的结论，按到期时刻升序。

    参数：
        repo: Repo，测试仓库

    返回：
        None，断言当日已到期/3日未到期/周已到期/非法 horizon/失败研报五种
        情形的入选与排序
    """
    day_old = await save_report_fixture(repo, report_type="us_open", horizon="当日")
    fresh_3d = await save_report_fixture(repo, report_type="us_open", horizon="3日")
    old_week = await save_report_fixture(repo, report_type="us_open", horizon="周")
    bad_horizon = await save_report_fixture(repo, report_type="manual", horizon="24h")
    await save_report_fixture(repo, report_type="manual", error="LLM 故障")
    await _backdate(repo, day_old.id, 25 * 3600)  # 当日窗口已到期 1 小时
    await _backdate(repo, fresh_3d.id, 25 * 3600)  # 3日窗口还剩 47 小时
    await _backdate(repo, old_week.id, 8 * 86400)  # 周窗口已到期 1 天
    await _backdate(repo, bad_horizon.id, 25 * 3600)  # 非法 horizon 存量行

    candidates = await repo.research_review.list_review_candidates(time.time())

    got = {(c.report_id, c.contract) for c in candidates}
    assert got == {(day_old.id, "BTC_USDT"), (old_week.id, "BTC_USDT")}
    # 周候选到期更早，排在前面
    assert [c.report_id for c in candidates] == [old_week.id, day_old.id]
    assert candidates[0].due_at <= candidates[1].due_at <= time.time()
    assert {c.horizon for c in candidates} == {"周", "当日"}


async def test_candidates_exclude_already_reviewed(repo: Repo) -> None:
    """已被任何正式复盘批改过的 (report_id, contract) 不再进入候选。

    参数：
        repo: Repo，测试仓库

    返回：
        None，断言复盘落库前后候选集变化
    """
    report = await save_report_fixture(repo, report_type="us_open", horizon="当日")
    await _backdate(repo, report.id, 25 * 3600)
    assert await repo.research_review.list_review_candidates(time.time()) != []

    await repo.research_review.save_review(
        review_report_id=1, report_id=report.id, contract="BTC_USDT"
    )

    assert await repo.research_review.list_review_candidates(time.time()) == []


async def test_get_case_returns_report_and_view(repo: Repo) -> None:
    """案例取数返回研报头与目标逐标的结论；任一缺失返回 None。

    参数：
        repo: Repo，测试仓库

    返回：
        None，断言正常取数与两种缺失路径
    """
    report = await save_report_fixture(
        repo, report_type="asia_open", direction="偏多", horizon="3日", narrative="结构向上"
    )

    case = await repo.research_review.get_case(report.id, "BTC_USDT")
    assert case is not None
    got_report, got_view = case
    assert got_report.id == report.id and got_report.report_type == "asia_open"
    assert got_view.direction == "偏多" and got_view.horizon == "3日"
    assert got_view.narrative == "结构向上"

    assert await repo.research_review.get_case(999999, "BTC_USDT") is None
    assert await repo.research_review.get_case(report.id, "ETH_USDT") is None


async def test_save_and_list_reviews_with_filters(repo: Repo) -> None:
    """复盘记录按时间窗/合约/limit 过滤，倒序取后按 id 正序返回。

    参数：
        repo: Repo，测试仓库

    返回：
        None，断言三种过滤与排序口径
    """
    now = time.time()
    for idx, contract in enumerate(("BTC_USDT", "ETH_USDT", "BTC_USDT")):
        await repo.research_review.save_review(
            review_report_id=1,
            report_id=100 + idx,
            contract=contract,
            direction_relation=f"评价{idx}",
            outcome_json='{"data_status":"complete"}',
            created_at=now - (3 - idx) * 3600,  # idx 越大越新
        )

    all_rows = await repo.research_review.list_reviews()
    assert [r.contract for r in all_rows] == ["BTC_USDT", "ETH_USDT", "BTC_USDT"]
    assert [r.id for r in all_rows] == sorted(r.id for r in all_rows)

    btc_only = await repo.research_review.list_reviews(contract="BTC_USDT")
    assert [r.direction_relation for r in btc_only] == ["评价0", "评价2"]

    window = await repo.research_review.list_reviews(start_ts=now - 2.5 * 3600)
    assert [r.direction_relation for r in window] == ["评价1", "评价2"]

    latest_only = await repo.research_review.list_reviews(limit=1)
    assert [r.direction_relation for r in latest_only] == ["评价2"]

    assert await repo.research_review.has_review(100, "BTC_USDT") is True
    assert await repo.research_review.has_review(100, "SOL_USDT") is False


async def test_candidates_keyset_pagination_no_repeat_no_skip(repo: Repo) -> None:
    """R5-3：keyset 游标分页遍历候选，页间不重复不遗漏；扫描期间候选集收缩不跳行。

    参数：
        repo: Repo，测试仓库

    返回：
        None，断言续扫页严格从游标之后开始：页首候选在扫描期间被复盘落库
        （候选集收缩）后，游标续扫不重不漏（offset 分页在同场景会跳过候选）
    """
    reports = [
        await save_report_fixture(repo, report_type="us_open", contract=c, horizon="当日")
        for c in ("BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT")
    ]
    # 回拨越久创建越早、到期越早：到期升序为 reports[3] → reports[0]
    for idx, r in enumerate(reports):
        await _backdate(repo, r.id, (25 + idx) * 3600)
    now = time.time()

    page1 = await repo.research_review.list_review_candidates(now, limit=2)
    assert [c.report_id for c in page1] == [reports[3].id, reports[2].id]
    cursor = (page1[-1].due_at, page1[-1].report_id, page1[-1].contract)
    # 模拟扫描期间候选集收缩：页首候选被复盘落库（offset 分页此时会跳过一条）
    await repo.research_review.save_review(
        review_report_id=1, report_id=page1[0].report_id, contract=page1[0].contract
    )

    page2 = await repo.research_review.list_review_candidates(now, limit=2, cursor=cursor)
    assert [c.report_id for c in page2] == [reports[1].id, reports[0].id]
    cursor = (page2[-1].due_at, page2[-1].report_id, page2[-1].contract)
    page3 = await repo.research_review.list_review_candidates(now, limit=2, cursor=cursor)
    assert page3 == []  # 游标之后无更多候选（扫描到尾部）


async def test_scan_cursor_roundtrip_and_reset(repo: Repo) -> None:
    """R5-3：扫描游标读写往返；None 重置后读回 None（下轮从头重扫）。

    参数：
        repo: Repo，测试仓库

    返回：
        None，断言初始为 None、写入后可读回、重置后为 None
    """
    assert await repo.research_review.get_scan_cursor() is None
    await repo.research_review.save_scan_cursor((1700000000.5, 42, "BTC_USDT"))
    assert await repo.research_review.get_scan_cursor() == (1700000000.5, 42, "BTC_USDT")
    await repo.research_review.save_scan_cursor(None)
    assert await repo.research_review.get_scan_cursor() is None


# ---------- R5-2：人工重评授权（research_rereview_requests） ----------


async def test_rereview_request_create_idempotent(repo: Repo) -> None:
    """授权登记幂等：同目标重复登记返回既有记录（reused=True），不同合约各自新建。

    参数：
        repo: Repo，测试仓库

    返回：
        None，断言首次新建、重复登记幂等命中（理由保持首次值）、跨合约互不影响
    """
    report = await save_report_fixture(repo, report_type="us_open", horizon="当日")

    req, reused = await repo.research_review.create_rereview_request(
        report.id, "BTC_USDT", "首次理由"
    )
    assert reused is False
    assert req.id > 0 and req.reason == "首次理由"
    assert req.requested_by == "human" and req.consumed_round_id == ""

    again, reused2 = await repo.research_review.create_rereview_request(
        report.id, "BTC_USDT", "换个理由"
    )
    assert reused2 is True and again.id == req.id
    assert again.reason == "首次理由"  # 幂等命中：既有授权不被覆盖

    other, reused3 = await repo.research_review.create_rereview_request(
        report.id, "ETH_USDT", "另一合约"
    )
    assert reused3 is False and other.id != req.id


async def test_rereview_request_pending_lifecycle(repo: Repo) -> None:
    """待消费查询随登记/消费变化；消费后同目标可再次登记（部分唯一索引只约束未消费行）。

    参数：
        repo: Repo，测试仓库

    返回：
        None，断言空态、登记后可见、消费后不可见、再登记新建成功
    """
    report = await save_report_fixture(repo, report_type="us_open", horizon="当日")
    assert await repo.research_review.get_pending_rereview_request(report.id, "BTC_USDT") is None
    assert await repo.research_review.list_pending_rereview_requests() == []

    req, _ = await repo.research_review.create_rereview_request(report.id, "BTC_USDT", "复核")
    got = await repo.research_review.get_pending_rereview_request(report.id, "BTC_USDT")
    assert got is not None and got.id == req.id
    assert [r.id for r in await repo.research_review.list_pending_rereview_requests()] == [req.id]

    await repo._conn.execute(
        "UPDATE research_rereview_requests SET consumed_round_id='rr-1' WHERE id=?", (req.id,)
    )
    await repo._conn.commit()
    assert await repo.research_review.get_pending_rereview_request(report.id, "BTC_USDT") is None
    assert await repo.research_review.list_pending_rereview_requests() == []

    req2, reused = await repo.research_review.create_rereview_request(
        report.id, "BTC_USDT", "再次复核"
    )
    assert reused is False and req2.id != req.id


async def test_save_review_manual_columns_roundtrip(repo: Repo) -> None:
    """复盘记录三新列（review_kind/rereview_reason/rereview_of_id）落库读回与替代指向查询。

    参数：
        repo: Repo，测试仓库

    返回：
        None，断言默认 auto 行三列为空、manual 行三列正确、latest_review_id 指向最新记录
    """
    first = await repo.research_review.save_review(
        review_report_id=1, report_id=100, contract="BTC_USDT"
    )
    manual = await repo.research_review.save_review(
        review_report_id=2,
        report_id=100,
        contract="BTC_USDT",
        review_kind="manual",
        rereview_reason="人工复核",
        rereview_of_id=first.id,
    )

    rows = await repo.research_review.list_reviews()
    assert [(r.review_kind, r.rereview_reason, r.rereview_of_id) for r in rows] == [
        ("auto", "", None),
        ("manual", "人工复核", first.id),
    ]
    assert await repo.research_review.latest_review_id(100, "BTC_USDT") == manual.id
    assert await repo.research_review.latest_review_id(100, "SOL_USDT") is None
