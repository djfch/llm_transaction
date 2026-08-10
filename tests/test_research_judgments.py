"""研报判断史只按当前报告与合约分组。"""

from src.memory import Database, Repo
from src.research.judgments import render_judgments


async def test_render_judgments_groups_assets_by_report(tmp_path):
    db = Database()
    await db.open(tmp_path / "judgments.db")
    try:
        repo = Repo(db)
        await repo.research.save_report_bundle(
            report_type="manual",
            summary="分化",
            cross_market_view="BTC 强于 ETH",
            global_risks_json="[]",
            raw_json="{}",
            round_id="r-v2",
            asset_views=[
                {
                    "contract": "BTC_USDT",
                    "direction": "偏多",
                    "confidence": "高",
                    "horizon": "3日",
                    "market_regime": "上涨趋势",
                    "technical_confirmation": "确认",
                    "basis_type": "混合",
                    "data_status": "完整",
                    "evidence_json": "[]",
                    "risks_json": "[]",
                    "narrative": "BTC 研判",
                    "market_context_json": "{}",
                },
                {
                    "contract": "ETH_USDT",
                    "direction": "中性",
                    "confidence": "低",
                    "horizon": "3日",
                    "market_regime": "震荡",
                    "technical_confirmation": "不可用",
                    "basis_type": "结构延续",
                    "data_status": "不可用",
                    "evidence_json": "[]",
                    "risks_json": "[]",
                    "narrative": "ETH 研判",
                    "market_context_json": "{}",
                },
            ],
        )

        reports = await repo.research.list_reports(7)
        text = await render_judgments(repo.research, reports, "## 历史研报结论")

        assert "报告#" in text and "分化" in text
        assert "BTC_USDT：偏多/高" in text
        assert "ETH_USDT：中性/低" in text
        assert "旧版结构" not in text
    finally:
        await db.close()
