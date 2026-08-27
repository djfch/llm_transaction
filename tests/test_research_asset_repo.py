"""研报 v2 报告头与逐标的结论的原子存取契约。"""

from __future__ import annotations
import asyncio

import json

import aiosqlite
import pytest

from src.memory.db import Database
from src.memory.repo import Repo


@pytest.fixture
async def repo(tmp_path):
    """构造指向临时数据库的 Repo 实例，测试结束后关闭数据库连接。

    参数：
        tmp_path: pytest 临时目录夹具，SQLite 数据库文件落在其中

    返回：
        AsyncIterator[Repo]，yield 已打开临时数据库的仓储对象，最终关闭数据库连接
    """
    db = Database()
    await db.open(tmp_path / "research-assets.db")
    try:
        yield Repo(db)
    finally:
        await db.close()


def _asset(contract: str) -> dict:
    """构造一条指定合约的逐标的结论文档字典，供 save_report_bundle 入参使用。

    参数：
        contract: str，合约代码（如 BTC_USDT），写入 contract 与 market_context_json 字段

    返回：
        dict：方向、信心、证据、风险与行情上下文等字段齐全的逐标的结论文档
    """
    return {
        "contract": contract,
        "direction": "偏多",
        "confidence": "高",
        "horizon": "3日",
        "market_regime": "上涨趋势",
        "technical_confirmation": "确认",
        "basis_type": "宏观驱动",
        "data_status": "完整",
        "evidence_json": '[{"point":"ETF流入","source":"快讯"}]',
        "risks_json": '["通胀反弹"]',
        "narrative": "资金面与结构共振。",
        "market_context_json": json.dumps({"contract": contract, "funding_rate": "0.0001"}),
    }


@pytest.mark.asyncio
async def test_save_report_bundle_and_query_latest_asset(repo: Repo) -> None:
    """校验报告头与逐标的结论原子落库，且能按报告与最新标的两个维度读回。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None，断言 schema_version=3、摘要与标的顺序一致，且 ETH 最新结论的行情上下文原样读回
    """
    report, views = await repo.research.save_report_bundle(
        report_type="us_open",
        summary="美盘逐标的研报",
        cross_market_view="BTC 强于 ETH",
        global_risks_json='["CPI"]',
        raw_json='{"asset_views":[]}',
        round_id="round-1",
        asset_views=[_asset("BTC_USDT"), _asset("ETH_USDT")],
    )

    assert report.schema_version == 3
    assert report.summary == "美盘逐标的研报"
    assert [view.contract for view in views] == ["BTC_USDT", "ETH_USDT"]
    stored = await repo.research.list_asset_views_by_report(report.id)
    assert [view.contract for view in stored] == ["BTC_USDT", "ETH_USDT"]
    latest = await repo.research.latest_asset_view("ETH_USDT")
    assert latest is not None
    assert latest.market_context_json == json.dumps(
        {"contract": "ETH_USDT", "funding_rate": "0.0001"}
    )


@pytest.mark.asyncio
async def test_save_report_bundle_rolls_back_header_when_asset_insert_fails(repo: Repo) -> None:
    """校验逐标的结论插入失败（重复合约触发唯一约束）时，报告头一并回滚不留脏数据。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None，断言 save_report_bundle 抛出 IntegrityError，且报告列表为空、总数为 0
    """
    duplicate = _asset("BTC_USDT")

    with pytest.raises(aiosqlite.IntegrityError):
        await repo.research.save_report_bundle(
            report_type="manual",
            summary="不应留下",
            cross_market_view="",
            global_risks_json="[]",
            raw_json="{}",
            round_id="round-bad",
            asset_views=[duplicate, duplicate],
        )

    reports, total = await repo.research.list_reports_page()
    assert reports == []
    assert total == 0


@pytest.mark.asyncio
async def test_bundle_transaction_cannot_be_committed_by_concurrent_repo_write(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """校验 bundle 事务挂起期间其他写入无法抢跑提交，最终失败回滚而并发写入正常落库。

    参数：
        repo: Repo，临时数据库仓储夹具
        monkeypatch: pytest.MonkeyPatch，用于将 _insert_asset_views 替换为可暂停版本

    返回：
        None，断言 bundle 任务抛 IntegrityError 后报告列表为空，并发 note 写入保留 1 条
    """
    started = asyncio.Event()
    resume = asyncio.Event()
    original = repo.research._insert_asset_views

    async def paused_insert(*args):
        """可暂停的 _insert_asset_views 替身：先通知已挂起，再等放行后才真正插入。

        参数：
            *args: 透传给原始 _insert_asset_views 的位置参数

        返回：
            原始 _insert_asset_views 的返回结果
        """
        started.set()
        await resume.wait()
        return await original(*args)

    monkeypatch.setattr(repo.research, "_insert_asset_views", paused_insert)
    duplicate = _asset("BTC_USDT")
    bundle_task = asyncio.create_task(
        repo.research.save_report_bundle(
            report_type="manual",
            summary="并发失败也不应留下",
            cross_market_view="",
            global_risks_json="[]",
            raw_json="{}",
            round_id="round-concurrent",
            asset_views=[duplicate, duplicate],
        )
    )
    await started.wait()
    note_task = asyncio.create_task(repo.add_note("other-round", "并发写入"))
    try:
        await asyncio.wait_for(asyncio.shield(note_task), timeout=0.1)
    except TimeoutError:
        pass
    resume.set()

    with pytest.raises(aiosqlite.IntegrityError):
        await bundle_task
    await note_task

    reports, total = await repo.research.list_reports_page()
    assert reports == []
    assert total == 0
    assert await repo.count_notes() == 1


@pytest.mark.asyncio
async def test_success_report_rejects_empty_asset_views(repo: Repo) -> None:
    """校验成功报告的逐标的结论列表为空时被拒绝，不写入任何数据。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None，断言 save_report_bundle 抛出 ValueError（至少包含一个逐标的结论）
    """
    with pytest.raises(ValueError, match="至少包含一个逐标的结论"):
        await repo.research.save_report_bundle(
            report_type="manual",
            summary="空报告",
            cross_market_view="",
            global_risks_json="[]",
            raw_json="{}",
            round_id="round-empty",
            asset_views=[],
        )


@pytest.mark.asyncio
async def test_failed_report_uses_current_schema_without_asset_views(repo: Repo) -> None:
    """校验失败报告按当前 schema 版本落库，不带逐标的结论，错误信息原样保存。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None，断言失败报告 schema_version=3、error 原样、summary 为空且逐标的结论列表为空
    """
    failed = await repo.research.save_failed_report(
        report_type="manual",
        error="ValueError: 输出无效",
        round_id="round-failed",
    )

    loaded = await repo.research.get_report(failed.id)
    assert loaded is not None
    assert loaded.schema_version == 3
    assert loaded.error == "ValueError: 输出无效"
    assert loaded.summary == ""
    assert await repo.research.list_asset_views_by_report(failed.id) == []
