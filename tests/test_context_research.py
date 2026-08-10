"""ContextBuilder 研报前瞻段单元测试：最新成功研报注入执行上下文（软约束）。

段标题「研报前瞻（宏观与消息面）」；无研报整段省略；读取异常降级为提示段不拖垮
其余 section。高置信（偏多/偏空）且未过 gate 有效期时标注风控硬约束行，中/低置信、
过期、gate 关闭或未装配配置均不标注。
"""

import time
from unittest.mock import AsyncMock

from src.agent.context import ContextBuilder
from src.config import ResearchConfig
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo

GATE_LINE = "⚠ 高置信结论有效期内：反向开仓已被风控硬约束"


async def _build_text(tmp_path, research_config=None, seed=None) -> str:
    """在临时数据库中执行可选研报种子回调并构建完整决策上下文文本。

    参数：
        tmp_path: Path，pytest 临时目录，用于隔离上下文数据库
        research_config: ResearchConfig | None，研报方向闸门配置
        seed: Callable | None，接收 repo(仓储)与 db(数据库)的异步种子回调

    返回：
        str，ContextBuilder 生成的决策上下文正文
    """
    db = Database()
    await db.open(tmp_path / "agent.db")
    try:
        repo = Repo(db)
        if seed is not None:
            await seed(repo, db)
        builder = ContextBuilder(
            MockGateway(),
            repo,
            CandleCache(MockGateway(), ManualPriceSource()),
            TriggerManager(lambda t, p: None),
            ["BTC_USDT"],
            research_config=research_config,
        )
        return (await builder.build("timer")).text
    finally:
        await db.close()


async def _seed_report(
    repo, db, *, direction="偏多", confidence="高", narrative="n" * 600, hours_ago=0.0
) -> None:
    """落库一份研报观点，并可回拨创建时间以模拟过期数据。

    参数：
        repo: Repo，提供研报持久化接口的仓储对象
        db: Database，用于在需要时直接回拨研报时间戳
        direction: str，逐标的方向，默认偏多
        confidence: str，观点置信度，默认高
        narrative: str，观点正文，默认生成 600 字长文本
        hours_ago: float，创建时间回拨小时数，0 表示保持当前时间

    返回：
        None，副作用为写入研报及其逐标的观点，并可更新创建时间
    """
    r, _ = await repo.research.save_report_bundle(
        report_type="us",
        summary="市场总览",
        cross_market_view="",
        global_risks_json="[]",
        raw_json="{}",
        round_id="r-research",
        asset_views=[
            {
                "contract": "BTC_USDT",
                "direction": direction,
                "confidence": confidence,
                "horizon": "24h",
                "market_regime": "上涨趋势",
                "technical_confirmation": "确认",
                "basis_type": "混合",
                "data_status": "完整",
                "evidence_json": "[]",
                "risks_json": "[]",
                "narrative": narrative,
                "market_context_json": "{}",
            }
        ],
    )
    if hours_ago:
        await db.conn.execute(
            "UPDATE research_reports SET created_at=? WHERE id=?",
            (time.time() - hours_ago * 3600, r.id),
        )
        await db.conn.execute(
            "UPDATE research_asset_views SET created_at=? WHERE report_id=?",
            (time.time() - hours_ago * 3600, r.id),
        )
        await db.conn.commit()


async def test_research_section_renders_report(tmp_path):
    """验证有研报时上下文完整展示观点元数据并把超长正文截断到 500 字。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证研报段标题、字段、时间和摘要截断
    """
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(),
        seed=lambda repo, db: _seed_report(repo, db, narrative="N" * 600),
    )

    assert "## 研报前瞻（宏观与消息面）" in text
    assert "方向：偏多 · 置信度：高 · 周期：24h" in text
    assert "创建时间：" in text
    assert f"正文摘要：{'N' * 500}…" in text
    assert "N" * 501 not in text


async def test_research_gate_annotation_high_confidence_fresh(tmp_path):
    """验证高置信且未过期的研报观点会在闸门开启时标注风控硬约束。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证硬约束提示行已注入上下文
    """
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(),
        seed=lambda repo, db: _seed_report(repo, db),
    )

    assert GATE_LINE in text


async def test_research_no_annotation_mid_confidence(tmp_path):
    """验证中置信研报只展示正文而不会标注方向硬约束。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证研报段存在而硬约束提示不存在
    """
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(),
        seed=lambda repo, db: _seed_report(repo, db, confidence="中"),
    )

    assert "## 研报前瞻" in text
    assert GATE_LINE not in text


async def test_research_no_annotation_stale_report(tmp_path):
    """验证超过研报闸门有效期的高置信观点不会标注硬约束。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证 14 小时前的研报仍展示但不参与硬约束
    """
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(),
        seed=lambda repo, db: _seed_report(repo, db, hours_ago=14),
    )

    assert "## 研报前瞻" in text
    assert GATE_LINE not in text


async def test_research_no_annotation_config_none(tmp_path):
    """验证未装配研报配置时仍注入正文但不附加方向硬约束。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证观点可见且硬约束提示缺失
    """
    text = await _build_text(
        tmp_path,
        seed=lambda repo, db: _seed_report(repo, db),
    )

    assert "方向：偏多 · 置信度：高" in text
    assert GATE_LINE not in text


async def test_research_no_annotation_gate_disabled(tmp_path):
    """验证显式关闭研报闸门时只展示研报而不标注硬约束。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证研报段存在且硬约束提示不存在
    """
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(gate_enabled=False),
        seed=lambda repo, db: _seed_report(repo, db),
    )

    assert "## 研报前瞻" in text
    assert GATE_LINE not in text


async def test_research_no_annotation_neutral_direction(tmp_path):
    """验证高置信中性观点不触发只面向偏多或偏空结论的硬约束。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证中性方向可见且硬约束提示不存在
    """
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(),
        seed=lambda repo, db: _seed_report(repo, db, direction="中性"),
    )

    assert "方向：中性 · 置信度：高" in text
    assert GATE_LINE not in text


async def test_research_section_omitted_without_report(tmp_path):
    """验证没有研报时省略整个前瞻段且不影响其他上下文区块。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证研报段缺失而交易计划和价格预警段仍存在
    """
    text = await _build_text(tmp_path, research_config=ResearchConfig())

    assert "研报前瞻" not in text
    assert "## 交易计划" in text
    assert "## 价格预警线" in text


async def test_research_section_degrades_on_repo_error(tmp_path):
    """验证研报仓储查询失败时上下文降级提示且其余区块继续构建。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证研报段显示暂不可用且账户段仍存在
    """

    async def _seed_broken(repo, db):
        """把研报查询方法替换为抛异常的 mock，模拟研报库故障。

        参数：
            repo: Repo，仓储对象，其 research.latest_asset_view 被就地替换为
                抛 RuntimeError 的 AsyncMock
            db: Database，数据库连接对象，本回调未使用（仅保持 seed 回调统一签名）

        返回：
            None，副作用：就地篡改 repo.research.latest_asset_view 使其调用即抛异常
        """
        repo.research.latest_asset_view = AsyncMock(side_effect=RuntimeError("研报库不可用"))

    text = await _build_text(tmp_path, research_config=ResearchConfig(), seed=_seed_broken)

    assert "## 研报前瞻（宏观与消息面）\n暂不可用" in text
    assert "## 账户" in text  # 其余 section 不拖垮
