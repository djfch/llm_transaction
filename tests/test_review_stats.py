"""src/review/stats.py 统计口径测试：纯函数，Trade 直接构造（不入库）。

口径（设计 spec §6）：样本=平仓来源成交；胜率=pnl>0 笔数/样本笔数（样本 0 → None）；
盈亏比=总盈利/|总亏损|（总亏损 0 → None）；全 Decimal。
"""

from decimal import Decimal

from src.memory.models import Trade
from src.review.stats import compute_review_stats, format_stats_text


def _trade(pnl: str, source: str = "llm_close", contract: str = "BTC_USDT") -> Trade:
    return Trade(
        id=0,
        round_id="r1",
        mode="paper",
        contract=contract,
        size=Decimal(1),
        price=Decimal("50000"),
        fee=Decimal("0.1"),
        pnl=Decimal(pnl),
        source=source,
        created_at=1000.0,
    )


def test_empty_sample():
    stats = compute_review_stats([])
    assert stats.close_count == 0
    assert stats.total_pnl == Decimal(0)
    assert stats.win_rate is None  # 样本 0 → 数据不足而非 0
    assert stats.profit_factor is None
    assert stats.avg_win is None and stats.avg_loss is None and stats.max_loss is None
    assert stats.per_contract == {}
    text = format_stats_text(stats)
    assert "平仓笔数：0" in text and "数据不足" in text and "无平仓样本" in text


def test_all_losses():
    """全亏：总亏损非 0 → 盈亏比为 0（不是 None）；胜率 0。"""
    stats = compute_review_stats([_trade("-10"), _trade("-5")])
    assert stats.close_count == 2
    assert stats.win_count == 0
    assert stats.win_rate == Decimal(0)
    assert stats.total_profit == Decimal(0)
    assert stats.total_loss == Decimal("-15")
    assert stats.profit_factor == Decimal(0)
    assert stats.avg_win is None  # 无盈利笔
    assert stats.avg_loss == Decimal("-7.5")
    assert stats.max_loss == Decimal("-10")


def test_all_wins_profit_factor_none():
    """全盈：总亏损为 0 → 盈亏比 None（spec §6 唯一 null 条件）。"""
    stats = compute_review_stats([_trade("10"), _trade("20")])
    assert stats.win_rate == Decimal(1)
    assert stats.profit_factor is None
    assert stats.avg_win == Decimal(15)
    assert stats.avg_loss is None and stats.max_loss is None


def test_mixed_sample():
    trades = [
        _trade("10"),
        _trade("20", contract="ETH_USDT"),
        _trade("-6"),
        _trade("-4", source="liquidation", contract="ETH_USDT"),
    ]
    stats = compute_review_stats(trades)
    assert stats.close_count == 4
    assert stats.total_pnl == Decimal("20")
    assert stats.win_rate == Decimal("0.5")
    assert stats.profit_factor == Decimal(3)  # 30 / |-10|
    assert stats.avg_win == Decimal(15)
    assert stats.avg_loss == Decimal(-5)
    assert stats.max_loss == Decimal(-6)
    assert stats.per_contract["BTC_USDT"].count == 2
    assert stats.per_contract["BTC_USDT"].pnl == Decimal(4)
    assert stats.per_contract["ETH_USDT"].pnl == Decimal(16)
    text = format_stats_text(stats)
    assert "胜率：0.5000（2/4）" in text
    assert "盈亏比：3.0000" in text
    assert "ETH_USDT 2 笔（盈亏 16）" in text


def test_non_close_sources_excluded():
    """llm_open 与 ''（历史/未知）不计入平仓样本。"""
    trades = [
        _trade("100", source="llm_open"),
        _trade("50", source=""),
        _trade("10", source="tpsl_close"),
        _trade("-2", source="user_close"),
    ]
    stats = compute_review_stats(trades)
    assert stats.close_count == 2
    assert stats.total_pnl == Decimal(8)


def test_to_dict_decimal_as_str():
    """stats_json 结构：Decimal 转字符串、None 保留，供落库。"""
    stats = compute_review_stats([_trade("10"), _trade("-4", contract="ETH_USDT")])
    data = stats.to_dict()
    assert data["total_pnl"] == "6"
    assert data["win_rate"] == "0.5000"
    assert data["per_contract"] == {
        "BTC_USDT": {"count": 1, "pnl": "10"},
        "ETH_USDT": {"count": 1, "pnl": "-4"},
    }
    empty = compute_review_stats([]).to_dict()
    assert empty["win_rate"] is None and empty["profit_factor"] is None
