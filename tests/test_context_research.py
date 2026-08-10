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
    """带研报种子构建上下文；seed 为 async 回调 (repo, db)，用于落库/篡改。"""
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
    """落库一份研报；hours_ago>0 时把 created_at 改写为 n 小时前（模拟过期）。"""
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
    """有研报：方向/置信度/周期/创建时间/正文摘要齐全，narrative 超 500 截断。"""
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
    """高置信未过期且 gate_enabled：标注硬约束行。"""
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(),
        seed=lambda repo, db: _seed_report(repo, db),
    )

    assert GATE_LINE in text


async def test_research_no_annotation_mid_confidence(tmp_path):
    """中置信：不标注（gate 只认高置信）。"""
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(),
        seed=lambda repo, db: _seed_report(repo, db, confidence="中"),
    )

    assert "## 研报前瞻" in text
    assert GATE_LINE not in text


async def test_research_no_annotation_stale_report(tmp_path):
    """created_at 在 14h 前（gate 有效期 13h）：过期不标注。"""
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(),
        seed=lambda repo, db: _seed_report(repo, db, hours_ago=14),
    )

    assert "## 研报前瞻" in text
    assert GATE_LINE not in text


async def test_research_no_annotation_config_none(tmp_path):
    """research_config 未装配（None）：不标注，但研报正文照常注入。"""
    text = await _build_text(
        tmp_path,
        seed=lambda repo, db: _seed_report(repo, db),
    )

    assert "方向：偏多 · 置信度：高" in text
    assert GATE_LINE not in text


async def test_research_no_annotation_gate_disabled(tmp_path):
    """gate_enabled=False：不标注。"""
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(gate_enabled=False),
        seed=lambda repo, db: _seed_report(repo, db),
    )

    assert "## 研报前瞻" in text
    assert GATE_LINE not in text


async def test_research_no_annotation_neutral_direction(tmp_path):
    """高置信但方向中性：不标注（gate 只约束多/空结论）。"""
    text = await _build_text(
        tmp_path,
        research_config=ResearchConfig(),
        seed=lambda repo, db: _seed_report(repo, db, direction="中性"),
    )

    assert "方向：中性 · 置信度：高" in text
    assert GATE_LINE not in text


async def test_research_section_omitted_without_report(tmp_path):
    """无研报：整段省略不留痕迹，其余 section 照常。"""
    text = await _build_text(tmp_path, research_config=ResearchConfig())

    assert "研报前瞻" not in text
    assert "## 交易计划" in text
    assert "## 价格预警线" in text


async def test_research_section_degrades_on_repo_error(tmp_path):
    """latest_asset_view 抛异常：build() 正常完成，研报段降级为「暂不可用」。"""

    async def _seed_broken(repo, db):
        repo.research.latest_asset_view = AsyncMock(side_effect=RuntimeError("研报库不可用"))

    text = await _build_text(tmp_path, research_config=ResearchConfig(), seed=_seed_broken)

    assert "## 研报前瞻（宏观与消息面）\n暂不可用" in text
    assert "## 账户" in text  # 其余 section 不拖垮
