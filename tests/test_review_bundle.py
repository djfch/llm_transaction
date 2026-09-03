"""复盘 bundle 测试（issue #113 C4）。

覆盖：render_research_review_stats 代码计数口径；save_review_bundle 单事务
（成功全落 / 中途失败整体回滚无残留 / 共享连接外部 commit 不破坏整批回滚 /
无草稿退化为纯报告）；R5-2 增：草稿带授权内部键时同事务消费授权并绑定轮次、
中途失败回滚授权不被半消费。K 线窗口适配器（GatewayAsyncCandleSource）的测试在
tests/test_research_outcome.py。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

import src.memory.review_repo as review_repo_mod
from src.memory import Database, Repo
from src.review.bundle import render_research_review_stats, save_review_bundle


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
        "direction_relation": "realized",
        "direction_reason": "方向一致",
        "reasoning_quality": "sound",
        "reasoning_review": "推理完整",
        "evidence_reviews_json": json.dumps(
            [
                {
                    "evidence_index": 0,
                    "fact_status": "confirmed",
                    "reasoning_status": "supported",
                    "explanation": "快照核对成立",
                }
            ],
            ensure_ascii=False,
        ),
        "confidence_assessment": "appropriate",
        "confidence_reason": "合规",
        "improvement_advice": "无",
        "outcome_json": json.dumps({"data_status": status}, ensure_ascii=False),
    }


def _deps(pending_list: list[dict]) -> SimpleNamespace:
    """构造 bundle 落库所需的最小 deps 替身（暂存区 + 新建版本编号 + 扫描 lease）。

    参数：
        pending_list: list[dict]，本轮暂存的研报复盘草稿列表

    返回：
        SimpleNamespace：含 pending_research_reviews、created_version_id 与
        R6-1 扫描 lease 字段（scan_cursor_loaded/scan_tail/scan_log）的替身；
        默认未做候选扫描（scan_cursor_loaded=False）
    """
    return SimpleNamespace(
        pending_research_reviews={(d["report_id"], d["contract"]): d for d in pending_list},
        created_version_id=None,
        scan_cursor_loaded=False,
        scan_tail=False,
        scan_log=[],
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
    """计数口径：条数、涉及研报数、合约分布、数据状态与依据事实核对分布全部来自草稿字段。

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
    assert "依据事实核对：confirmed 3 条" in text


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


# ---------- R5-2：人工授权重评的授权消费（与批改同生共死） ----------


def _manual_pending(report_id: int, contract: str, request_id: int, previous_id: int) -> dict:
    """构造一条人工授权重评草稿（自动草稿字段 + manual 身份与授权内部键）。

    参数：
        report_id: int，被重评的研报编号
        contract: str，被重评的合约
        request_id: int，命中的授权编号（rereview_request_id 内部键，落库时被弹出消费）
        previous_id: int，被替代的上一条复盘记录 id（rereview_of_id）

    返回：
        dict：带 review_kind/rereview_reason/rereview_of_id/rereview_request_id 的草稿
    """
    return _pending(report_id, contract, "complete") | {
        "review_kind": "manual",
        "rereview_reason": "人工复核原结论",
        "rereview_of_id": previous_id,
        "rereview_request_id": request_id,
    }


async def test_bundle_consumes_rereview_request(repo: Repo) -> None:
    """草稿带授权内部键时：授权随 bundle 同事务被消费并绑定轮次，manual 三列落库。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None，断言复盘行 review_kind/rereview_reason/rereview_of_id 正确、
        授权 consumed_round_id 绑定 bundle 轮次且不再 pending
    """
    seeded = await repo.research_review.save_review(
        review_report_id=999, report_id=1, contract="BTC_USDT"
    )
    req, _ = await repo.research_review.create_rereview_request(1, "BTC_USDT", "人工复核原结论")

    draft = _manual_pending(1, "BTC_USDT", req.id, seeded.id)
    report = await save_review_bundle(
        repo,
        _deps([draft]),
        period_start=1000.0,
        period_end=2000.0,
        stats_json="{}",
        report_md="# 正文",
        strategy_action="none",
        round_id="rr-manual",
    )

    rows = await repo.research_review.list_reviews()
    manual_row = [r for r in rows if r.review_kind == "manual"]
    assert len(manual_row) == 1
    assert manual_row[0].review_report_id == report.id
    assert manual_row[0].rereview_reason == "人工复核原结论"
    assert manual_row[0].rereview_of_id == seeded.id
    assert await repo.research_review.get_pending_rereview_request(1, "BTC_USDT") is None
    cur = await repo._conn.execute(
        "SELECT consumed_round_id FROM research_rereview_requests WHERE id=?", (req.id,)
    )
    row = await cur.fetchone()
    assert row["consumed_round_id"] == "rr-manual"  # 授权与批改同事务绑定轮次


async def test_bundle_failure_keeps_rereview_request_pending(repo: Repo, monkeypatch) -> None:
    """bundle 中途失败整体回滚时，授权消费一并回滚：授权保持待处理（不被半消费）。

    参数：
        repo: Repo，临时数据库仓储夹具
        monkeypatch: pytest.MonkeyPatch，替换 _insert_review 注入中途失败

    返回：
        None，断言失败后复盘行无残留且授权仍未消费
    """
    req, _ = await repo.research_review.create_rereview_request(1, "BTC_USDT", "人工复核原结论")
    real_insert = review_repo_mod._insert_review
    state = {"n": 0}

    async def fail_on_second(*args, **kwargs):
        """第二条插入抛错（第一条为带授权的 manual 草稿，其后失败触发整批回滚）。

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
    pending = [
        _manual_pending(1, "BTC_USDT", req.id, 7),
        _pending(2, "ETH_USDT", "partial"),
    ]
    with pytest.raises(RuntimeError):
        await save_review_bundle(
            repo,
            _deps(pending),
            period_start=1000.0,
            period_end=2000.0,
            stats_json="{}",
            report_md="# 正文",
            strategy_action="none",
            round_id="rr-fail",
        )

    assert await repo.research_review.list_reviews() == []  # 批改无残留
    pending_req = await repo.research_review.get_pending_rereview_request(1, "BTC_USDT")
    assert pending_req is not None and pending_req.id == req.id  # 授权未被消费


# ---------- R6-1：候选扫描游标 lease + 随 bundle 事务 ack ----------


def _scanning_deps(
    pending_list: list[dict],
    scan_log: list[tuple[float, int, str, bool]],
    *,
    tail: bool,
) -> SimpleNamespace:
    """构造做过候选扫描的 deps 替身（scan_cursor_loaded=True + 指定扫描日志）。

    参数：
        pending_list: list[dict]，本轮暂存的研报复盘草稿列表
        scan_log: list[tuple[float, int, str, bool]]，本轮扫描日志
        tail: bool，本轮最后一次扫描是否到候选集尾部

    返回：
        SimpleNamespace：带扫描 lease 状态的 deps 替身
    """
    deps = _deps(pending_list)
    deps.scan_cursor_loaded = True
    deps.scan_tail = tail
    deps.scan_log = scan_log
    return deps


async def test_bundle_acks_scan_cursor_with_report(repo: Repo) -> None:
    """报告成功时游标随同事务 ack：越过已跳过/已复盘候选，停在首个未复盘可用候选之前。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None，断言库中游标推进到已复盘候选处（未复盘的 SOL_USDT 不被越过）
    """
    pending = [_pending(2, "ETH_USDT", "complete")]
    deps = _scanning_deps(
        pending,
        [
            (100.0, 1, "BTC_USDT", False),  # 数据不足被跳过：可越过
            (200.0, 2, "ETH_USDT", True),  # 可用且本轮已复盘：可越过
            (300.0, 3, "SOL_USDT", True),  # 可用但本轮未复盘：ack 停在它之前
        ],
        tail=False,
    )
    await save_review_bundle(
        repo,
        deps,
        period_start=1000.0,
        period_end=2000.0,
        stats_json="{}",
        report_md="# 正文",
        strategy_action="none",
        round_id="rr-scan",
    )
    assert await repo.research_review.get_scan_cursor() == (200.0, 2, "ETH_USDT")


async def test_bundle_scan_tail_resets_cursor(repo: Repo) -> None:
    """扫到候选集尾部时 ack 落 NULL：库中旧游标被重置，下轮从头重扫。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None，断言 bundle 后库中游标读回 None
    """
    await repo.research_review.save_scan_cursor((50.0, 9, "BTC_USDT"))  # 上轮残留游标
    deps = _scanning_deps([], [(100.0, 1, "BTC_USDT", False)], tail=True)
    await save_review_bundle(
        repo,
        deps,
        period_start=1000.0,
        period_end=2000.0,
        stats_json="{}",
        report_md="# 正文",
        strategy_action="none",
        round_id="rr-tail",
    )
    assert await repo.research_review.get_scan_cursor() is None


async def test_bundle_without_scan_keeps_cursor(repo: Repo) -> None:
    """本轮未做候选扫描（未调用列出工具）时，库中已有游标不被 bundle 触碰。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None，断言库中游标保持原值
    """
    await repo.research_review.save_scan_cursor((50.0, 9, "BTC_USDT"))
    await save_review_bundle(
        repo,
        _deps([]),
        period_start=1000.0,
        period_end=2000.0,
        stats_json="{}",
        report_md="# 正文",
        strategy_action="none",
        round_id="rr-noscan",
    )
    assert await repo.research_review.get_scan_cursor() == (50.0, 9, "BTC_USDT")


async def test_bundle_failure_rolls_back_scan_cursor(repo: Repo, monkeypatch) -> None:
    """bundle 中途失败整体回滚时，游标 ack 一并回滚：库中游标保持上轮位置。

    参数：
        repo: Repo，临时数据库仓储夹具
        monkeypatch: pytest.MonkeyPatch，替换 _insert_review 注入中途失败

    返回：
        None，断言失败后游标未推进（仍为上轮 ack 的位置）
    """
    await repo.research_review.save_scan_cursor((50.0, 9, "BTC_USDT"))

    async def fail_insert(*args, **kwargs):
        """插入固定抛错（模拟约束冲突/磁盘故障），触发整批回滚。

        参数：
            args: tuple，_insert_review 的位置参数（不消费）
            kwargs: dict，_insert_review 的关键字参数（不消费）

        返回：
            int：永不返回

        异常：
            RuntimeError：固定抛出，模拟中途失败
        """
        raise RuntimeError("模拟中途失败")

    monkeypatch.setattr(review_repo_mod, "_insert_review", fail_insert)
    deps = _scanning_deps(
        [_pending(2, "ETH_USDT", "complete")],
        [(200.0, 2, "ETH_USDT", True)],
        tail=False,
    )
    with pytest.raises(RuntimeError):
        await save_review_bundle(
            repo,
            deps,
            period_start=1000.0,
            period_end=2000.0,
            stats_json="{}",
            report_md="# 正文",
            strategy_action="none",
            round_id="rr-scan-fail",
        )
    assert await repo.research_review.get_scan_cursor() == (50.0, 9, "BTC_USDT")  # 未推进


# ---------- R6-2：授权消费改条件更新，防并发二次消费 ----------


async def test_bundle_rejects_double_consumed_rereview_request(repo: Repo) -> None:
    """同一授权被第二个 bundle 再消费时条件更新落空：抛错且整体回滚，授权仍绑定首轮。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None，断言第二次 bundle 抛 RuntimeError、其报告与批改无残留、
        授权 consumed_round_id 仍绑定第一轮轮次
    """
    seeded = await repo.research_review.save_review(
        review_report_id=999, report_id=1, contract="BTC_USDT"
    )
    req, _ = await repo.research_review.create_rereview_request(1, "BTC_USDT", "人工复核原结论")

    first = await save_review_bundle(
        repo,
        _deps([_manual_pending(1, "BTC_USDT", req.id, seeded.id)]),
        period_start=1000.0,
        period_end=2000.0,
        stats_json="{}",
        report_md="# 第一轮",
        strategy_action="none",
        round_id="rr-first",
    )
    # 第二个 bundle 携带同一授权（并发轮/重放）：insert 批改可写，但授权消费
    # 条件更新落空 → 抛错回滚，整条 bundle 不残留
    with pytest.raises(RuntimeError, match="拒绝重复消费"):
        await save_review_bundle(
            repo,
            _deps([_manual_pending(1, "BTC_USDT", req.id, seeded.id)]),
            period_start=1000.0,
            period_end=2000.0,
            stats_json="{}",
            report_md="# 第二轮",
            strategy_action="none",
            round_id="rr-second",
        )

    _, total = await repo.review.list_review_reports_page(10, 0)
    assert total == 1  # 第二轮报告未残留
    rows = await repo.research_review.list_reviews()
    manual_rows = [r for r in rows if r.review_kind == "manual"]
    assert len(manual_rows) == 1 and manual_rows[0].review_report_id == first.id
    cur = await repo._conn.execute(
        "SELECT consumed_round_id FROM research_rereview_requests WHERE id=?", (req.id,)
    )
    row = await cur.fetchone()
    assert row["consumed_round_id"] == "rr-first"  # 授权仍绑定第一轮
