"""复盘侧历史数据回看工具测试（issue #113 F9）：read_timeline / get_macro_series。

覆盖：未读案例拒绝、窗口越界（早于创建时间/晚于窗口终点/区间倒置）拒绝、
事实层正常回看与过滤、宏观序列未装配降级、窗口参数透传与数据源异常降级。
"""

from __future__ import annotations

import time

import pytest

from src.memory import Database, Repo
from src.review.strategy import StrategyStore
from src.review.tool_handlers import ReviewToolDeps
from src.review.tools import ReviewToolRegistry
from tests.research_helpers import save_report_fixture

_INIT = "初始策略书：" + "稳健交易，控制回撤。" * 10


class _FakeMacroProvider:
    """宏观序列桩：记录调用参数并返回固定文本（结构满足研报数据聚合器协议）。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        """保存可选的注入异常与调用记录列表。

        参数：
            error: Exception | None，非空时 get_macro_series 抛出该异常
        """
        self._error = error
        self.calls: list[tuple[str, int, float | None]] = []

    async def get_macro_series(
        self, indicator: str, look_back: int = 365, end_ts: float | None = None
    ) -> str:
        """记录调用参数；注入异常时抛出，否则返回固定序列文本。

        参数：
            indicator: str，宏观指标代码
            look_back: int，回溯天数
            end_ts: float | None，窗口终点 Unix 秒

        返回：
            str：固定宏观序列文本

        异常：
            Exception：初始化注入的异常（模拟数据源失败）
        """
        self.calls.append((indicator, look_back, end_ts))
        if self._error is not None:
            raise self._error
        return f"宏观序列 {indicator} 共 3 点"


@pytest.fixture
async def deps(tmp_path) -> ReviewToolDeps:
    """组装复盘工具依赖（临时数据库 + 策略 store；无 K 线源与数据源）。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        ReviewToolDeps：绑定临时资源的复盘工具依赖
    """
    db = Database()
    await db.open(tmp_path / "review_research_data.db")
    repo = Repo(db)
    prompt = tmp_path / "system_prompt.md"
    prompt.write_text(_INIT, encoding="utf-8")
    store = StrategyStore(prompt, repo)
    await store.seed_if_empty()
    return ReviewToolDeps(repo=repo, store=store, mode="paper")


@pytest.fixture
def registry(deps: ReviewToolDeps) -> ReviewToolRegistry:
    """组装复盘工具注册表。

    参数：
        deps: ReviewToolDeps，复盘工具依赖

    返回：
        ReviewToolRegistry：绑定依赖的注册表
    """
    return ReviewToolRegistry(deps)


async def _seed_loaded_case(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> tuple[int, float]:
    """造一份 horizon=当日、已到期（回拨 25 小时）的研报并读取案例登记窗口。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        tuple[int, float]：（研报编号, 案例创建时间戳）
    """
    report = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        direction="偏多",
        confidence="中",
        horizon="当日",
        narrative="结构向上。",
    )
    ts = time.time() - 25 * 3600
    await deps.repo._conn.execute(
        "UPDATE research_reports SET created_at=? WHERE id=?", (ts, report.id)
    )
    await deps.repo._conn.execute(
        "UPDATE research_asset_views SET created_at=? WHERE report_id=?", (ts, report.id)
    )
    await deps.repo._conn.commit()
    await registry.execute(
        "get_research_review_case", {"report_id": report.id, "contract": "BTC_USDT"}
    )
    return report.id, ts


async def test_read_timeline_requires_loaded_case(registry: ReviewToolRegistry) -> None:
    """未读案例直接回看被拒绝并提示先读案例。

    参数：
        registry: ReviewToolRegistry，工具注册表

    返回：
        None：断言拒绝文本
    """
    text = await registry.execute(
        "read_timeline",
        {"report_id": 1, "contract": "BTC_USDT", "start_ts": 1, "end_ts": 2},
    )
    assert "请先用 get_research_review_case 读取" in text

    text2 = await registry.execute(
        "get_macro_series", {"report_id": 1, "contract": "BTC_USDT", "indicator": "cpi"}
    )
    assert "请先用 get_research_review_case 读取" in text2


async def test_read_timeline_window_out_of_range(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """回看窗口越界（早于创建时间/晚于窗口终点/区间倒置）一律拒绝。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None：断言三种越界的拒绝文本
    """
    report_id, created = await _seed_loaded_case(deps, registry)
    upper = created + 86400  # horizon=当日
    base = {"report_id": report_id, "contract": "BTC_USDT"}

    early = await registry.execute(
        "read_timeline", {**base, "start_ts": created - 10, "end_ts": created + 100}
    )
    assert "start_ts 不得早于案例创建时间" in early

    late = await registry.execute(
        "read_timeline", {**base, "start_ts": created, "end_ts": upper + 10}
    )
    assert "end_ts 不得晚于案例窗口终点" in late

    inverted = await registry.execute(
        "read_timeline", {**base, "start_ts": created + 100, "end_ts": created + 100}
    )
    assert "end_ts 须大于 start_ts" in inverted


async def test_read_timeline_returns_rows_in_window(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """窗口内事实层记录按时间正序返回；窗口外记录与 kind/keyword 过滤生效。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None：断言返回内容、排序与过滤
    """
    report_id, created = await _seed_loaded_case(deps, registry)
    rows = [
        {
            "source": "jin10",
            "kind": "flash",
            "title": "窗口外旧闻",
            "url": "",
            "published_at": created - 3600,
            "meta_json": "{}",
            "dedup_key": "out",
            "fetched_at": created - 3600,
        },
        {
            "source": "jin10",
            "kind": "flash",
            "title": "加息预期降温",
            "url": "https://example.com/1",
            "published_at": created + 1800,
            "meta_json": "{}",
            "dedup_key": "in1",
            "fetched_at": created + 1800,
        },
        {
            "source": "blockbeats",
            "kind": "flash",
            "title": "ETF 净流入",
            "url": "",
            "published_at": created + 3600,
            "meta_json": "{}",
            "dedup_key": "in2",
            "fetched_at": created + 3600,
        },
    ]
    await deps.repo.research.append_timeline_many(rows)
    base = {
        "report_id": report_id,
        "contract": "BTC_USDT",
        "start_ts": created,
        "end_ts": created + 7200,
    }
    text = await registry.execute("read_timeline", base)
    assert "窗口外旧闻" not in text
    assert "加息预期降温" in text and "（https://example.com/1）" in text
    assert "[jin10/flash]" in text and "[blockbeats/flash]" in text
    assert text.index("加息预期降温") < text.index("ETF 净流入")  # 时间正序

    filtered = await registry.execute("read_timeline", {**base, "keyword": "加息"})
    assert "加息预期降温" in filtered and "ETF 净流入" not in filtered


async def test_macro_series_requires_provider(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """研报数据源未装配时返回中文降级提示。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（research_data_provider 默认 None）
        registry: ReviewToolRegistry，工具注册表

    返回：
        None：断言降级文本
    """
    report_id, _ = await _seed_loaded_case(deps, registry)
    text = await registry.execute(
        "get_macro_series",
        {"report_id": report_id, "contract": "BTC_USDT", "indicator": "cpi"},
    )
    assert "宏观序列数据不可用：研报数据源未装配" in text


async def test_macro_series_passes_case_window(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """宏观序列默认 end_ts 为窗口上界，look_back 由案例窗口跨度按天推导。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None：断言透传的 indicator/look_back/end_ts 与返回文本
    """
    provider = _FakeMacroProvider()
    deps.research_data_provider = provider
    report_id, created = await _seed_loaded_case(deps, registry)
    upper = created + 86400  # 窗口已过期，upper = window_end
    text = await registry.execute(
        "get_macro_series",
        {"report_id": report_id, "contract": "BTC_USDT", "indicator": "cpi"},
    )
    assert "宏观序列 cpi 共 3 点" in text
    assert provider.calls == [("cpi", 1, upper)]

    # 显式 end_ts 在窗口内：透传并按新跨度推导 look_back
    await registry.execute(
        "get_macro_series",
        {
            "report_id": report_id,
            "contract": "BTC_USDT",
            "indicator": "m2",
            "end_ts": created + 3600,
        },
    )
    assert provider.calls[-1] == ("m2", 1, created + 3600)


async def test_macro_series_end_ts_out_of_range(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """显式 end_ts 越界（晚于窗口上界/不晚于创建时间）被拒绝。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None：断言两种越界的拒绝文本
    """
    deps.research_data_provider = _FakeMacroProvider()
    report_id, created = await _seed_loaded_case(deps, registry)
    upper = created + 86400
    base = {"report_id": report_id, "contract": "BTC_USDT", "indicator": "cpi"}

    late = await registry.execute("get_macro_series", {**base, "end_ts": upper + 10})
    assert "end_ts 不得晚于案例窗口终点" in late

    early = await registry.execute("get_macro_series", {**base, "end_ts": created})
    assert "end_ts 须晚于案例创建时间" in early


async def test_macro_series_provider_error_degrades(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """数据源抛异常时以降级文本表达，不向 LLM 抛错。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None：断言降级文本含异常说明
    """
    deps.research_data_provider = _FakeMacroProvider(error=RuntimeError("FRED 限流"))
    report_id, _ = await _seed_loaded_case(deps, registry)
    text = await registry.execute(
        "get_macro_series",
        {"report_id": report_id, "contract": "BTC_USDT", "indicator": "cpi"},
    )
    assert "宏观序列数据不可用：FRED 限流" in text
