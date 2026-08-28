"""复盘 bundle 与 K 线窗口适配器测试（issue #113 C4）。

覆盖：render_research_review_stats 代码计数口径；save_review_bundle 单事务
（成功全落 / 中途失败整体回滚无残留 / 共享连接外部 commit 不破坏整批回滚 /
无草稿退化为纯报告）；RecentWindowCandleSource 的 from/to 窗口过滤与 limit 直通。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.memory.review_repo as review_repo_mod
from src.gateway.base import Candle
from src.memory import Database, Repo
from src.review.bundle import render_research_review_stats, save_review_bundle
from src.review.research_outcome import RecentWindowCandleSource


@pytest.fixture
async def repo(tmp_path) -> AsyncIterator[Repo]:
    """创建隔离数据库的仓储夹具。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        AsyncIterator[Repo]：yield 连接 bundle.db 的仓储，并在夹具收尾关闭数据库
    """
    db = Database()
    await db.open(tmp_path / "bundle.db")
    try:
        yield Repo(db)
    finally:
        await db.close()


def _pending(report_id: int, contract: str, status: str) -> dict:
    """构造一条与 submit_research_review 暂存结构一致的研报复盘草稿。

    参数：
        report_id: int，被复盘的研报编号
        contract: str，被复盘的合约
        status: str，客观结果数据状态（写入 outcome_json.data_status）

    返回：
        dict：研报复盘草稿字典（不含 review_report_id/created_at）
    """
    return {
        "report_id": report_id,
        "contract": contract,
        "direction_relation": "方向一致",
        "reasoning_quality": "推理完整",
        "evidence_reviews_json": '[{"index":0,"comment":"成立"}]',
        "confidence_assessment": "合规",
        "improvement_advice": "无",
        "outcome_json": json.dumps({"data_status": status}, ensure_ascii=False),
    }


def _deps(pending_list: list[dict]) -> SimpleNamespace:
    """构造 bundle 落库所需的最小 deps 替身（暂存区 + 新建版本编号）。

    参数：
        pending_list: list[dict]，本轮暂存的研报复盘草稿列表

    返回：
        SimpleNamespace：含 pending_research_reviews 与 created_version_id 的替身
    """
    return SimpleNamespace(
        pending_research_reviews={(d["report_id"], d["contract"]): d for d in pending_list},
        created_version_id=None,
    )


# ---------- render_research_review_stats（代码确定性计数） ----------


def test_render_stats_empty() -> None:
    """无草稿时返回空串（报告不追加统计段）。

    参数：无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert render_research_review_stats([]) == ""


def test_render_stats_counts() -> None:
    """计数口径：条数、涉及研报数、合约分布、客观结果数据状态分布全部来自草稿字段。

    参数：无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    pending = [
        _pending(1, "BTC_USDT", "complete"),
        _pending(1, "ETH_USDT", "partial"),
        _pending(2, "BTC_USDT", "unavailable"),
    ]
    text = render_research_review_stats(pending)
    assert text.startswith("## 研报复盘统计")
    assert "批改条数：3（涉及研报 2 份）" in text
    assert "BTC_USDT 2 条" in text and "ETH_USDT 1 条" in text
    assert "complete 1 条" in text and "partial 1 条" in text and "unavailable 1 条" in text


# ---------- save_review_bundle（单事务落库） ----------


async def test_bundle_saves_report_and_reviews(repo: Repo) -> None:
    """成功路径：报告（含统计段）与两条研报复盘同事务落库，review_report_id 关联正确。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    pending = [
        _pending(1, "BTC_USDT", "complete"),
        _pending(2, "ETH_USDT", "partial"),
    ]
    report = await save_review_bundle(
        repo,
        _deps(pending),
        period_start=1000.0,
        period_end=2000.0,
        stats_json="{}",
        report_md="# 正文",
        strategy_action="none",
        round_id="rr-1",
    )
    stored = await repo.review.get_review_report(report.id)
    assert stored is not None and stored.round_id == "rr-1"
    assert stored.report_md.startswith("# 正文")  # LLM 正文在前
    assert "批改条数：2（涉及研报 2 份）" in stored.report_md  # 统计段由代码追加
    rows = await repo.research_review.list_reviews()
    assert [(r.report_id, r.contract, r.review_report_id) for r in rows] == [
        (1, "BTC_USDT", report.id),
        (2, "ETH_USDT", report.id),
    ]


async def test_bundle_rollback_on_mid_failure(repo: Repo, monkeypatch) -> None:
    """原子性：第二条批改插入失败时整体回滚——报告与批改都不残留。

    参数：
        repo: Repo，临时数据库仓储夹具
        monkeypatch: pytest.MonkeyPatch，替换 _insert_review 注入中途失败

    返回：
        None：通过断言校验目标场景，无返回值
    """
    real_insert = review_repo_mod._insert_review
    state = {"n": 0}

    async def fail_on_second(*args, **kwargs):
        """第二条插入抛错（模拟约束冲突/磁盘故障）。

        参数：
            args: tuple，_insert_review 的位置参数，原样透传真实函数
            kwargs: dict，_insert_review 的关键字参数，原样透传真实函数

        返回：
            int：第一条调用返回的真实插入行 id

        异常：
            RuntimeError：第二条调用固定抛出，模拟中途失败
        """
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("模拟中途失败")
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(review_repo_mod, "_insert_review", fail_on_second)
    pending = [_pending(1, "BTC_USDT", "complete"), _pending(2, "ETH_USDT", "partial")]
    with pytest.raises(RuntimeError):
        await save_review_bundle(
            repo,
            _deps(pending),
            period_start=1000.0,
            period_end=2000.0,
            stats_json="{}",
            report_md="# 正文",
            strategy_action="none",
            round_id="rr-x",
        )
    _, total = await repo.review.list_review_reports_page(10, 0)
    assert total == 0  # 报告未残留
    assert await repo.research_review.list_reviews() == []  # 批改未残留


async def test_bundle_immune_to_external_commit_on_shared_conn(repo: Repo, monkeypatch) -> None:
    """回归（审查 P1-2）：bundle 中途共享连接被外部 commit，失败后仍整体回滚无残留。

    旧实现在共享连接上顺序写报告与批改：外部协程的 commit 会把已写行提前提交，
    rollback 只剩最后一段可回滚，残留「有报告无批改」。现行为独立连接事务，
    共享连接上的提交无法触及本批。

    参数：
        repo: Repo，临时数据库仓储夹具
        monkeypatch: pytest.MonkeyPatch，替换 _insert_review 注入外部提交与中途失败

    返回：
        None：通过断言校验目标场景，无返回值
    """
    real_insert = review_repo_mod._insert_review
    state = {"n": 0}

    async def external_commit_then_fail(*args, **kwargs):
        """第二条插入前先在共享连接上做无关写入并 commit，随后抛错。

        旧实现下共享连接的 commit 会把 bundle 已写行提前提交（残留「有报告无批改」）；
        独立连接 BEGIN IMMEDIATE 持写锁，此外部写入只会撞锁失败，无法穿插进本批。

        参数：
            args: tuple，_insert_review 的位置参数，原样透传真实函数
            kwargs: dict，_insert_review 的关键字参数，原样透传真实函数

        返回：
            int：第一条调用返回的真实插入行 id

        异常：
            RuntimeError：第二条调用固定抛出，模拟中途失败
        """
        state["n"] += 1
        if state["n"] == 2:
            try:
                await repo._db.conn.execute(
                    "INSERT INTO notes(round_id,content,created_at) VALUES('ext','外部写入',1)"
                )
                await repo._db.conn.commit()
                state["external"] = "committed"  # 旧实现的破坏路径：穿插成功
            except Exception:
                state["external"] = "locked"  # 独立连接持写锁：外部写入撞锁
            raise RuntimeError("模拟中途失败")
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(review_repo_mod, "_insert_review", external_commit_then_fail)
    pending = [_pending(1, "BTC_USDT", "complete"), _pending(2, "ETH_USDT", "partial")]
    with pytest.raises(RuntimeError):
        await save_review_bundle(
            repo,
            _deps(pending),
            period_start=1000.0,
            period_end=2000.0,
            stats_json="{}",
            report_md="# 正文",
            strategy_action="none",
            round_id="rr-ext",
        )
    # 不变量：外部写入无论撞锁还是提交，bundle 都不得残留任何行
    _, total = await repo.review.list_review_reports_page(10, 0)
    assert total == 0  # 报告未残留
    assert await repo.research_review.list_reviews() == []  # 批改未残留
    # 外部无关写入不被 bundle 回滚牵连：提交则留存、撞锁则缺席，两种都一致
    cur = await repo._db.conn.execute("SELECT COUNT(*) AS n FROM notes WHERE round_id='ext'")
    row = await cur.fetchone()
    assert row["n"] == (1 if state["external"] == "committed" else 0)


async def test_bundle_without_reviews_keeps_plain_report(repo: Repo) -> None:
    """无研报复盘草稿：退化为纯报告落库，正文不追加统计段。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    report = await save_review_bundle(
        repo,
        _deps([]),
        period_start=1000.0,
        period_end=2000.0,
        stats_json="{}",
        report_md="# 仅正文",
        strategy_action="none",
        round_id="rr-2",
    )
    assert report.report_md == "# 仅正文"
    assert await repo.research_review.list_reviews() == []


# ---------- RecentWindowCandleSource（from/to 窗口 → limit 路径适配） ----------


def _candle(t: int) -> Candle:
    """构造一根只有时间戳有意义的测试 K 线。

    参数：
        t: int，K 线秒级时间戳

    返回：
        Candle：价格字段全为占位值的 K 线
    """
    return Candle(t=t, o=Decimal(1), h=Decimal(2), l=Decimal(0), c=Decimal(1), v=Decimal(0))


class _FakeGateway:
    """记录调用参数并回放固定 K 线列表的网关替身。"""

    def __init__(self, candles: list[Candle]) -> None:
        """保存回放数据并初始化调用记录。

        参数：
            candles: list[Candle]，get_candlesticks 固定回放的 K 线列表

        返回：
            None，仅初始化实例属性
        """
        self._candles = candles
        self.calls: list[dict] = []

    def get_candlesticks(self, contract, interval="1m", limit=None, from_ts=None, to_ts=None):
        """记录参数并回放 K 线列表。

        参数：
            contract: str，合约名（仅对齐接口签名）
            interval: str，K 线周期（仅对齐接口签名）
            limit: int | None，最近 N 根
            from_ts: int | None，窗口起点
            to_ts: int | None，窗口终点

        返回：
            list[Candle]：构造时注入的固定 K 线列表副本
        """
        self.calls.append({"limit": limit, "from_ts": from_ts, "to_ts": to_ts})
        return list(self._candles)


def test_adapter_window_filters_client_side() -> None:
    """from/to 查询：底层只走 limit 路径（网关 from/to 不可用），窗口 [from,to) 客户端过滤。

    参数：无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    gateway = _FakeGateway([_candle(t) for t in (90, 100, 110, 200)])
    source = RecentWindowCandleSource(gateway, recent_limit=300)
    got = source.get_candlesticks("BTC_USDT", interval="1h", from_ts=100, to_ts=200)
    assert [c.t for c in got] == [100, 110]  # 90 在窗外、200 为不含端点
    assert gateway.calls == [{"limit": 300, "from_ts": None, "to_ts": None}]


def test_adapter_passthrough_without_window() -> None:
    """纯 limit 查询直通底层（不参与窗口过滤）。

    参数：无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    gateway = _FakeGateway([_candle(1), _candle(2)])
    source = RecentWindowCandleSource(gateway)
    got = source.get_candlesticks("BTC_USDT", interval="1h", limit=10)
    assert [c.t for c in got] == [1, 2]
    assert gateway.calls == [{"limit": 10, "from_ts": None, "to_ts": None}]


def test_adapter_returns_covered_part_when_window_exceeds_recent() -> None:
    """窗口早于回拉范围时只返回实际覆盖部分（由上层以 partial/unavailable 表达）。

    参数：无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    gateway = _FakeGateway([_candle(t) for t in (500, 600)])
    source = RecentWindowCandleSource(gateway)
    assert source.get_candlesticks("BTC_USDT", from_ts=100, to_ts=200) == []  # 完全窗外
    got = source.get_candlesticks("BTC_USDT", from_ts=100, to_ts=550)  # 部分覆盖
    assert [c.t for c in got] == [500]
