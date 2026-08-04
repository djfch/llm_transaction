"""指标子系统装配测试：store 播种、on_change 广播、回调束形状、OI 缓存刷新与任务取消。

tmp_path 隔离配置文件与 DB；asyncio.Queue 断言广播载荷；setup_indicators 创建的
oi_task 由每个用例收尾取消（与主程序 shutdown 同一取消语义）。
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from src.config import DEFAULT_INDICATOR_SHORTLIST
from src.market.indicator_service import REGISTRY
from src.market.indicators_setup import IndicatorComponents, setup_indicators
from src.memory.db import Database
from src.memory.repo import Repo
from src.review.indicator_config import IndicatorConfigValidationError


class _FakeGateway:
    """OI 数据源假网关：固定返回持仓量 123.5。"""

    def fetch_open_interest(self, contract: str) -> Decimal:
        return Decimal("123.5")


class _FakeCandles:
    """K 线缓存假货：无 K 线（面板 time=None、序列为空，指标值走降级形状）。"""

    def get_recent(self, contract: str, interval: str, n: int) -> list:
        return []


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    db = Database()
    await db.open(tmp_path / "t.db")
    yield Repo(db)
    await db.close()


async def _setup(
    repo: Repo, tmp_path: Path, queue: asyncio.Queue | None = None
) -> IndicatorComponents:
    return await setup_indicators(
        repo,
        _FakeGateway(),
        _FakeCandles(),
        ["BTC_USDT"],
        queue or asyncio.Queue(),
        config_path=tmp_path / "indicator_config.yaml",
    )


async def _cancel(components: IndicatorComponents) -> None:
    """取消 OI 后台任务并消化 CancelledError（同主程序 shutdown 语义）。"""
    assert components.oi_task is not None
    components.oi_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await components.oi_task


async def test_seed_creates_file_and_v1(repo: Repo, tmp_path: Path):
    components = await _setup(repo, tmp_path)
    try:
        path = tmp_path / "indicator_config.yaml"
        assert path.exists()  # 播种原子写默认基线文件
        assert "ema20" in path.read_text(encoding="utf-8")
        versions = await repo.indicator_config.list_versions()
        assert len(versions) == 1
        assert versions[0].created_by == "human" and versions[0].reason == "初始基线"
        assert components.store.load_current().shortlist == DEFAULT_INDICATOR_SHORTLIST
        # 二次 setup 不重复播种（版本表非空即跳过）
        again = await _setup(repo, tmp_path)
        try:
            assert len(await repo.indicator_config.list_versions()) == 1
        finally:
            await _cancel(again)
    finally:
        await _cancel(components)


async def test_panel_series_config_get_shapes(repo: Repo, tmp_path: Path):
    components = await _setup(repo, tmp_path)
    try:
        await components.oi_cache.refresh_once()  # 确定性填充 OI 缓存（不依赖任务时序）
        panel = components.panel("BTC_USDT", "1h")
        assert panel["shortlist"] == DEFAULT_INDICATOR_SHORTLIST  # 面板合并当前短名单
        assert set(panel["indicators"]) == set(REGISTRY)  # 13 个注册指标全在
        series = components.series("BTC_USDT", "1h", None, 10)
        assert list(series["series"]) == DEFAULT_INDICATOR_SHORTLIST  # keys=None 用短名单
        assert series["series"]["oi"]["current"] == "123.5"  # oi 只有 current 无序列
        cfg = components.config_get()
        assert cfg["shortlist"] == DEFAULT_INDICATOR_SHORTLIST
        assert len(cfg["available"]) == len(REGISTRY)
        assert set(cfg["available"][0]) == {"key", "label", "kind", "fields"}
    finally:
        await _cancel(components)


async def test_on_change_broadcasts_indicator_config_updated(repo: Repo, tmp_path: Path):
    queue: asyncio.Queue = asyncio.Queue()
    components = await _setup(repo, tmp_path, queue)
    try:
        assert queue.empty()  # 播种不广播（仅 revise/rollback 触发）
        r = await components.config_revise(["ema9", "rsi14"], "人工调整")
        assert r == {"ok": True, "version_id": 2}  # v1 为播种版本
        assert queue.get_nowait() == {"type": "indicator_config_updated"}
        assert components.store.load_current().shortlist == ["ema9", "rsi14"]  # 文件已替换
        r2 = await components.config_rollback(1)
        assert r2 == {"rolled_back_to": 1, "version_id": 3}
        assert queue.get_nowait() == {"type": "indicator_config_updated"}
        assert components.store.load_current().shortlist == DEFAULT_INDICATOR_SHORTLIST  # 回到基线
    finally:
        await _cancel(components)


async def test_revise_validation_error_passthrough(repo: Repo, tmp_path: Path):
    components = await _setup(repo, tmp_path)
    try:
        with pytest.raises(IndicatorConfigValidationError):
            await components.config_revise(["not_a_key"], "人工调整")
    finally:
        await _cancel(components)


async def test_oi_task_runs_and_cancels_cleanly(repo: Repo, tmp_path: Path):
    components = await _setup(repo, tmp_path)
    assert components.oi_task is not None and not components.oi_task.done()
    await components.oi_cache.refresh_once()
    assert components.oi_cache.get("BTC_USDT") == Decimal("123.5")
    await _cancel(components)
    assert components.oi_task.done()  # 取消语义干净（sleep 中被打断，无残留）
