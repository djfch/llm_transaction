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
        """固定返回持仓量 123.5，模拟 OI 数据源查询。

        参数：
            contract: str，合约代码（假货不区分合约，恒返回同一定值）

        返回：
            Decimal：固定持仓量 123.5
        """
        return Decimal("123.5")


class _FakeCandles:
    """K 线缓存假货：无 K 线（面板 time=None、序列为空，指标值走降级形状）。"""

    def get_recent(self, contract: str, interval: str, n: int) -> list:
        """模拟无 K 线的缓存查询：恒返回空列表。

        参数：
            contract: str，合约代码（假货忽略）
            interval: str，K 线周期（假货忽略）
            n: int，请求的 K 线条数（假货忽略）

        返回：
            list：空列表，表示缓存中没有任何 K 线
        """
        return []


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    """构造指向临时数据库的 Repo 实例，用例结束后关闭数据库。

    参数：
        tmp_path: Path，pytest 临时目录夹具，数据库文件 t.db 落在其中

    返回：
        AsyncIterator[Repo]：已打开临时数据库的仓储对象（yield 后负责关闭连接）
    """
    db = Database()
    await db.open(tmp_path / "t.db")
    yield Repo(db)
    await db.close()


async def _setup(
    repo: Repo, tmp_path: Path, queue: asyncio.Queue | None = None
) -> IndicatorComponents:
    """用假网关与空 K 线缓存装配指标组件，配置文件写入临时目录。

    参数：
        repo: Repo，仓储夹具，指标配置版本表落在其临时数据库中
        tmp_path: Path，pytest 临时目录夹具，indicator_config.yaml 落在其中
        queue: asyncio.Queue | None，on_change 广播队列；为 None 时新建空队列

    返回：
        IndicatorComponents：setup_indicators 装配好的指标组件束
        （watchlist 固定为 ["BTC_USDT"]）
    """
    return await setup_indicators(
        repo,
        _FakeGateway(),
        _FakeCandles(),
        ["BTC_USDT"],
        queue or asyncio.Queue(),
        config_path=tmp_path / "indicator_config.yaml",
    )


async def _cancel(components: IndicatorComponents) -> None:
    """取消 OI 后台任务并消化 CancelledError（同主程序 shutdown 语义）。

    参数：
        components: IndicatorComponents，待关闭的指标组件集合

    返回：
        None，执行上述模拟操作或副作用，无返回值
    """
    assert components.oi_task is not None
    components.oi_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await components.oi_task


async def test_seed_creates_file_and_v1(repo: Repo, tmp_path: Path):
    """校验首次装配播种默认配置文件与 v1 版本，二次装配不重复播种。

    参数：
        repo: Repo，指向临时数据库的仓储夹具
        tmp_path: Path，pytest 临时目录夹具，隔离指标配置文件

    返回：
        None，断言配置文件原子落盘且含默认基线指标、版本表恰有 created_by="human"
        且 reason 为「初始基线」的 v1、store 短名单等于默认短名单；二次 setup
        时版本表非空即跳过，不再新增版本
    """
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
    """校验面板/序列/配置查询三个出口的载荷形状与空 K 线时的降级行为。

    参数：
        repo: Repo，指向临时数据库的仓储夹具
        tmp_path: Path，pytest 临时目录夹具，隔离指标配置文件

    返回：
        None，断言面板合并当前短名单且覆盖全部注册指标、series 默认按短名单返回
        且 oi 只有 current 值 123.5 无序列、config_get 返回短名单与每个条目均含
        key/label/kind/fields 四键的可用指标清单
    """
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
    """验证指标配置变化后广播更新事件。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """验证指标配置修订的校验错误原样返回。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    components = await _setup(repo, tmp_path)
    try:
        with pytest.raises(IndicatorConfigValidationError):
            await components.config_revise(["not_a_key"], "人工调整")
    finally:
        await _cancel(components)


async def test_oi_task_runs_and_cancels_cleanly(repo: Repo, tmp_path: Path):
    """验证持仓量后台任务可运行并干净取消。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    components = await _setup(repo, tmp_path)
    assert components.oi_task is not None and not components.oi_task.done()
    await components.oi_cache.refresh_once()
    assert components.oi_cache.get("BTC_USDT") == Decimal("123.5")
    await _cancel(components)
    assert components.oi_task.done()  # 取消语义干净（sleep 中被打断，无残留）
