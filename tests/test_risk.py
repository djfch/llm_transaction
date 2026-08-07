"""风控引擎与规则集的测试：覆盖率要求 100%，含恰好达线的边界用例。

边界总约定：超过阈值才拒绝，等于放行。
例外：日下单数为计数上限，orders_today == max_orders_per_day 时额度已用尽，拒绝开仓。
"""

from decimal import Decimal

from src.config import RiskConfig
from src.risk.engine import RiskEngine
from src.risk.models import AccountSnapshot, DailyStats, PositionSnapshot, TradeIntent, Verdict
from src.risk.rules import intent_notional, positions_notional

D = Decimal


def make_intent(**kw) -> TradeIntent:
    defaults = dict(
        contract="BTC_USDT",
        side_size=D(1),
        price=D(100),
        is_close=False,
        leverage=1,
        mark_price=D(100),
        quanto_multiplier=D(1),
    )
    return TradeIntent(**(defaults | kw))


def make_account(**kw) -> AccountSnapshot:
    defaults = dict(equity=D(1000), unrealised_pnl=D(0))
    return AccountSnapshot(**(defaults | kw))


def make_daily(**kw) -> DailyStats:
    defaults = dict(realized_pnl=D(0), orders_today=0)
    return DailyStats(**(defaults | kw))


def make_position(**kw) -> PositionSnapshot:
    defaults = dict(contract="BTC_USDT", size=D(1), mark_price=D(100), quanto_multiplier=D(1))
    return PositionSnapshot(**(defaults | kw))


def check(
    intent=None,
    account=None,
    positions=None,
    daily=None,
    watchlist=None,
    config=None,
    research_direction=None,
):
    """便捷入口：默认参数全部安全（应放行），各用例只覆盖目标规则。"""
    return RiskEngine().check(
        intent or make_intent(),
        account or make_account(),
        positions if positions is not None else [],
        daily or make_daily(),
        watchlist if watchlist is not None else ["BTC_USDT"],
        config or RiskConfig(),
        research_direction=research_direction,
    )


# ---------- 模型与辅助函数 ----------


def test_verdict_allow_and_deny():
    assert Verdict.allow().allowed is True
    deny = Verdict.deny(["理由一", "理由二"])
    assert deny.allowed is False
    assert deny.reasons == ["理由一", "理由二"]


def test_intent_notional_uses_price_for_limit_order():
    # 名义价值 = |张数| × quanto_multiplier × 委托价
    intent = make_intent(side_size=D(-2), price=D(150), quanto_multiplier=D("0.5"))
    assert intent_notional(intent) == D(150)


def test_intent_notional_uses_mark_price_for_market_order():
    # 市价单（price=None）用标记价估值
    intent = make_intent(side_size=D(2), price=None, mark_price=D(120), quanto_multiplier=D("0.5"))
    assert intent_notional(intent) == D(120)


def test_positions_notional_sums_absolute_values():
    assert positions_notional([]) == D(0)
    positions = [
        make_position(size=D(2), mark_price=D(100)),  # 多仓 200
        make_position(size=D(-3), mark_price=D(100)),  # 空仓按绝对值计 300
    ]
    assert positions_notional(positions) == D(500)


# ---------- 白名单 ----------


def test_whitelist_allows_listed_contract():
    assert check().allowed is True


def test_whitelist_denies_unlisted_contract():
    verdict = check(intent=make_intent(contract="DOGE_USDT"))
    assert verdict.allowed is False
    assert any("白名单" in r for r in verdict.reasons)


def test_whitelist_exempts_close():
    # 合约被移出白名单后仍必须能平仓
    verdict = check(intent=make_intent(contract="DOGE_USDT", is_close=True))
    assert verdict.allowed is True


# ---------- kill_switch ----------


def test_kill_switch_denies_open():
    verdict = check(config=RiskConfig(kill_switch=True))
    assert verdict.allowed is False
    assert any("kill_switch" in r for r in verdict.reasons)


def test_kill_switch_allows_close():
    verdict = check(intent=make_intent(is_close=True), config=RiskConfig(kill_switch=True))
    assert verdict.allowed is True


# ---------- 单仓名义价值占比 ----------


def test_position_limit_equal_threshold_allowed():
    # 名义价值 300 / 权益 1000 = 0.30，恰好等于 max_position_pct，放行
    intent = make_intent(side_size=D(1), price=D(300), mark_price=D(300))
    assert check(intent=intent).allowed is True


def test_position_limit_over_threshold_denied():
    intent = make_intent(side_size=D(1), price=D(301), mark_price=D(301))
    verdict = check(intent=intent)
    assert verdict.allowed is False
    assert any("单仓" in r for r in verdict.reasons)


def test_position_limit_exempts_close():
    intent = make_intent(side_size=D(1), price=D(500), mark_price=D(500), is_close=True)
    assert check(intent=intent).allowed is True


# ---------- 总持仓名义价值占比 ----------


def test_total_position_limit_equal_threshold_allowed():
    # 已有持仓 700 + 本单 100 = 800 / 权益 1000 = 0.80，恰好等于上限，放行
    positions = [make_position(size=D(7), mark_price=D(100))]
    intent = make_intent(side_size=D(1), price=D(100), mark_price=D(100))
    assert check(intent=intent, positions=positions).allowed is True


def test_total_position_limit_over_threshold_denied():
    # 700 + 101 = 801 > 800
    positions = [make_position(size=D(7), mark_price=D(100))]
    intent = make_intent(side_size=D(1), price=D(101), mark_price=D(101))
    verdict = check(intent=intent, positions=positions)
    assert verdict.allowed is False
    assert any("总持仓" in r for r in verdict.reasons)


def test_total_position_limit_exempts_close():
    # 持仓已超限（900/1000），平仓仍放行
    positions = [make_position(size=D(9), mark_price=D(100))]
    intent = make_intent(is_close=True)
    assert check(intent=intent, positions=positions).allowed is True


# ---------- 杠杆上限 ----------


def test_leverage_equal_max_allowed():
    assert check(intent=make_intent(leverage=5)).allowed is True


def test_leverage_over_max_denied():
    verdict = check(intent=make_intent(leverage=6))
    assert verdict.allowed is False
    assert any("杠杆" in r for r in verdict.reasons)


def test_leverage_exempts_close():
    assert check(intent=make_intent(leverage=6, is_close=True)).allowed is True


# ---------- 日亏损锁仓 ----------


def test_daily_loss_equal_limit_allowed():
    # 当日已实现 -100 / 权益 1000 = 0.10，恰好等于 daily_loss_limit，仍可开仓
    assert check(daily=make_daily(realized_pnl=D(-100))).allowed is True


def test_daily_loss_over_limit_denies_open():
    verdict = check(daily=make_daily(realized_pnl=D("-100.01")))
    assert verdict.allowed is False
    assert any("锁仓" in r for r in verdict.reasons)


def test_daily_loss_counts_unrealised_pnl():
    # 已实现 -50 + 未实现 -60 = -110，超过 -100 锁仓线
    verdict = check(
        account=make_account(unrealised_pnl=D(-60)),
        daily=make_daily(realized_pnl=D(-50)),
    )
    assert verdict.allowed is False
    assert any("锁仓" in r for r in verdict.reasons)


def test_daily_loss_lock_allows_close():
    verdict = check(intent=make_intent(is_close=True), daily=make_daily(realized_pnl=D(-200)))
    assert verdict.allowed is True


# ---------- 日下单数上限 ----------


def test_max_orders_below_limit_allowed():
    assert check(daily=make_daily(orders_today=19)).allowed is True


def test_max_orders_at_limit_denies_open():
    # 计数型上限：已达 20 单即额度用尽，拒绝第 21 单开仓
    verdict = check(daily=make_daily(orders_today=20))
    assert verdict.allowed is False
    assert any("下单数" in r for r in verdict.reasons)


def test_max_orders_exempts_close():
    verdict = check(intent=make_intent(is_close=True), daily=make_daily(orders_today=20))
    assert verdict.allowed is True


# ---------- 价格偏离保护 ----------


def test_deviation_market_order_exempt():
    assert check(intent=make_intent(price=None)).allowed is True


def test_deviation_equal_threshold_allowed():
    # 偏离 2/100 = 0.02，恰好等于 max_deviation，放行
    assert check(intent=make_intent(price=D(102), mark_price=D(100))).allowed is True


def test_deviation_over_threshold_denied():
    verdict = check(intent=make_intent(price=D(103), mark_price=D(100)))
    assert verdict.allowed is False
    assert any("偏离" in r for r in verdict.reasons)


def test_deviation_denies_close_too():
    # 价格偏离是平仓也不豁免的唯一规则（防胖手指）
    verdict = check(intent=make_intent(price=D(120), mark_price=D(100), is_close=True))
    assert verdict.allowed is False
    assert any("偏离" in r for r in verdict.reasons)


# ---------- 引擎汇总 ----------


def test_engine_aggregates_multiple_reasons():
    # 同时命中：白名单 + kill_switch + 杠杆 + 价格偏离，理由全部汇总返回
    config = RiskConfig(kill_switch=True)
    intent = make_intent(contract="DOGE_USDT", leverage=6, price=D(103), mark_price=D(100))
    verdict = check(intent=intent, config=config)
    assert verdict.allowed is False
    assert len(verdict.reasons) == 4


# ---------- 研报方向闸门 ----------


def test_research_gate_exempts_close():
    # 平仓/减仓永远豁免：即使高置信研报反向也不拦
    verdict = check(intent=make_intent(is_close=True), research_direction="偏空")
    assert verdict.allowed is True


def test_research_gate_none_direction_allowed():
    # 闸门未传入方向（关闭/无研报/过期/降级）：不约束
    assert check(research_direction=None).allowed is True


def test_research_gate_neutral_allowed():
    assert check(research_direction="中性").allowed is True


def test_research_gate_bearish_denies_long():
    verdict = check(intent=make_intent(side_size=D(1)), research_direction="偏空")
    assert verdict.allowed is False
    assert any("闸门" in r for r in verdict.reasons)


def test_research_gate_bullish_denies_short():
    verdict = check(intent=make_intent(side_size=D(-1)), research_direction="偏多")
    assert verdict.allowed is False
    assert any("闸门" in r for r in verdict.reasons)


def test_research_gate_bearish_allows_short():
    # 顺向（研报偏空 + 开空）放行
    assert check(intent=make_intent(side_size=D(-1)), research_direction="偏空").allowed is True


def test_research_gate_bullish_allows_long():
    # 顺向（研报偏多 + 开多）放行
    assert check(intent=make_intent(side_size=D(1)), research_direction="偏多").allowed is True


def test_engine_check_passes_research_direction():
    # engine.check 的 research_direction 入参链路：传入即组装进 RuleInput 生效
    verdict = RiskEngine().check(
        make_intent(side_size=D(1)),
        make_account(),
        [],
        make_daily(),
        ["BTC_USDT"],
        RiskConfig(),
        research_direction="偏空",
    )
    assert verdict.allowed is False
    assert verdict.reasons == ["高置信研报偏空，反向开多被闸门拦截"]
