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
    db = Database()
    await db.open(tmp_path / "research-assets.db")
    try:
        yield Repo(db)
    finally:
        await db.close()


def _asset(contract: str) -> dict:
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
    report, views = await repo.research.save_report_bundle(
        report_type="us_open",
        summary="美盘逐标的研报",
        cross_market_view="BTC 强于 ETH",
        global_risks_json='["CPI"]',
        raw_json='{"asset_views":[]}',
        round_id="round-1",
        asset_views=[_asset("BTC_USDT"), _asset("ETH_USDT")],
    )

    assert report.schema_version == 2
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
    started = asyncio.Event()
    resume = asyncio.Event()
    original = repo.research._insert_asset_views

    async def paused_insert(*args):
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
    failed = await repo.research.save_failed_report(
        report_type="manual",
        error="ValueError: 输出无效",
        round_id="round-failed",
    )

    loaded = await repo.research.get_report(failed.id)
    assert loaded is not None
    assert loaded.schema_version == 2
    assert loaded.error == "ValueError: 输出无效"
    assert loaded.summary == ""
    assert await repo.research.list_asset_views_by_report(failed.id) == []
