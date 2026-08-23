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
    """构造一份默认安全的交易意图，各用例按需覆盖字段。

    参数：
        kw: 关键字参数，覆盖默认字段（如 contract、price、is_close、leverage）

    返回：
        TradeIntent：默认 BTC_USDT 开 1 张多、委托价/标记价 100、杠杆 1 的开仓意图
    """
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
    """构造一份默认安全的账户快照。

    参数：
        kw: 关键字参数，覆盖默认字段（如 equity、unrealised_pnl）

    返回：
        AccountSnapshot：默认权益 1000、未实现盈亏 0 的账户快照
    """
    defaults = dict(equity=D(1000), unrealised_pnl=D(0))
    return AccountSnapshot(**(defaults | kw))


def make_daily(**kw) -> DailyStats:
    """构造一份默认安全的当日统计。

    参数：
        kw: 关键字参数，覆盖默认字段（如 realized_pnl、orders_today）

    返回：
        DailyStats：默认当日已实现盈亏 0、已下单 0 笔的统计数据
    """
    defaults = dict(realized_pnl=D(0), orders_today=0)
    return DailyStats(**(defaults | kw))


def make_position(**kw) -> PositionSnapshot:
    """构造一份默认的持仓快照。

    参数：
        kw: 关键字参数，覆盖默认字段（如 size、mark_price）

    返回：
        PositionSnapshot：默认 BTC_USDT 持 1 张多、标记价 100 的持仓快照
    """
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
    open_orders=None,
):
    """使用安全默认值执行一次风控检查，允许调用方只覆盖目标规则输入。

    参数：
        intent: TradeIntent | None，待检查交易意图；为空时使用默认安全意图
        account: AccountSnapshot | None，账户快照；为空时使用默认安全账户
        positions: list[PositionSnapshot] | None，当前持仓；为空时使用空持仓
        daily: DailyStats | None，当日统计；为空时使用默认安全统计
        watchlist: list[str] | None，允许开仓的合约白名单；为空时仅允许 BTC_USDT
        config: RiskConfig | None，风控配置；为空时使用默认配置
        research_direction: str | None，研报方向；为空时不施加方向约束
        open_orders: list[OpenOrderIntent] | None，未成交挂单快照；为空时视为无挂单

    返回：
        Verdict，风控引擎汇总全部规则后的放行或拒绝结果
    """
    return RiskEngine().check(
        intent or make_intent(),
        account or make_account(),
        positions if positions is not None else [],
        daily or make_daily(),
        watchlist if watchlist is not None else ["BTC_USDT"],
        config or RiskConfig(),
        research_direction=research_direction,
        open_orders=open_orders,
    )


# ---------- 模型与辅助函数 ----------


def test_verdict_allow_and_deny():
    """校验 Verdict 放行/拒绝工厂方法的判定位与理由列表。

    参数：无

    返回：
        None，断言 allow() 放行、deny() 拒绝且理由列表原样保留
    """
    assert Verdict.allow().allowed is True
    deny = Verdict.deny(["理由一", "理由二"])
    assert deny.allowed is False
    assert deny.reasons == ["理由一", "理由二"]


def test_intent_notional_uses_price_for_limit_order():
    """校验限价单名义价值按委托价计算（|张数| × 合约乘数 × 委托价）。

    参数：无

    返回：
        None，断言 2 × 0.5 × 150 = 150，与持仓方向（正/负张数）无关
    """
    # 名义价值 = |张数| × quanto_multiplier × 委托价
    intent = make_intent(side_size=D(-2), price=D(150), quanto_multiplier=D("0.5"))
    assert intent_notional(intent) == D(150)


def test_intent_notional_uses_mark_price_for_market_order():
    """校验市价单（无委托价）的名义价值改用标记价估值。

    参数：无

    返回：
        None，断言 2 × 0.5 × 120 = 120，即 price=None 时回退到 mark_price
    """
    # 市价单（price=None）用标记价估值
    intent = make_intent(side_size=D(2), price=None, mark_price=D(120), quanto_multiplier=D("0.5"))
    assert intent_notional(intent) == D(120)


def test_positions_notional_sums_absolute_values():
    """校验持仓总名义价值：空仓按绝对值计入，多空相加不对冲。

    参数：无

    返回：
        None，断言空列表为 0，多仓 200 加空仓 300 合计 500
    """
    assert positions_notional([]) == D(0)
    positions = [
        make_position(size=D(2), mark_price=D(100)),  # 多仓 200
        make_position(size=D(-3), mark_price=D(100)),  # 空仓按绝对值计 300
    ]
    assert positions_notional(positions) == D(500)


# ---------- 白名单 ----------


def test_whitelist_allows_listed_contract():
    """校验白名单内合约的开仓正常放行。

    参数：无

    返回：
        None，断言默认 BTC_USDT 开仓被允许
    """
    assert check().allowed is True


def test_whitelist_denies_unlisted_contract():
    """校验不在白名单的合约开仓被拒绝。

    参数：无

    返回：
        None，断言 DOGE_USDT 开仓被拒且理由含“白名单”
    """
    verdict = check(intent=make_intent(contract="DOGE_USDT"))
    assert verdict.allowed is False
    assert any("白名单" in r for r in verdict.reasons)


def test_whitelist_exempts_close():
    """校验合约被移出白名单后仍必须能平仓（平仓豁免白名单）。

    参数：无

    返回：
        None，断言 DOGE_USDT 的平仓单被放行
    """
    # 合约被移出白名单后仍必须能平仓
    verdict = check(intent=make_intent(contract="DOGE_USDT", is_close=True))
    assert verdict.allowed is True


# ---------- kill_switch ----------


def test_kill_switch_denies_open():
    """校验 kill_switch 开启时拒绝一切开仓。

    参数：无

    返回：
        None，断言开仓被拒且理由含“kill_switch”
    """
    verdict = check(config=RiskConfig(kill_switch=True))
    assert verdict.allowed is False
    assert any("kill_switch" in r for r in verdict.reasons)


def test_kill_switch_allows_close():
    """校验 kill_switch 开启时平仓仍放行（只锁开仓不锁平仓）。

    参数：无

    返回：
        None，断言平仓单被允许
    """
    verdict = check(intent=make_intent(is_close=True), config=RiskConfig(kill_switch=True))
    assert verdict.allowed is True


# ---------- 单仓名义价值占比 ----------


def test_position_limit_equal_threshold_allowed():
    """校验单仓名义占比恰好等于上限（0.30）时放行。

    参数：无

    返回：
        None，断言名义 300 / 权益 1000 = 0.30 达线但被允许（超过才拒绝）
    """
    # 名义价值 300 / 权益 1000 = 0.30，恰好等于 max_position_pct，放行
    intent = make_intent(side_size=D(1), price=D(300), mark_price=D(300))
    assert check(intent=intent).allowed is True


def test_position_limit_over_threshold_denied():
    """校验单仓名义占比略超上限（0.301）时拒绝开仓。

    参数：无

    返回：
        None，断言名义 301 / 权益 1000 超线被拒且理由含“单仓”
    """
    intent = make_intent(side_size=D(1), price=D(301), mark_price=D(301))
    verdict = check(intent=intent)
    assert verdict.allowed is False
    assert any("单仓" in r for r in verdict.reasons)


def test_position_limit_exempts_close():
    """校验单仓占比已超限（0.50）时平仓仍豁免放行。

    参数：无

    返回：
        None，断言超限的平仓单被允许
    """
    intent = make_intent(side_size=D(1), price=D(500), mark_price=D(500), is_close=True)
    assert check(intent=intent).allowed is True


# ---------- 总持仓名义价值占比 ----------


def test_total_position_limit_equal_threshold_allowed():
    """校验总持仓名义占比恰好等于上限（0.80）时放行。

    参数：无

    返回：
        None，断言已有 700 + 本单 100 = 800 / 权益 1000 = 0.80 达线但被允许
    """
    # 已有持仓 700 + 本单 100 = 800 / 权益 1000 = 0.80，恰好等于总仓上限，放行
    # （单仓上限置 1 隔离 #57 新语义：同合约持仓已计入单仓敞口会先行拒绝）
    positions = [make_position(size=D(7), mark_price=D(100))]
    intent = make_intent(side_size=D(1), price=D(100), mark_price=D(100))
    config = RiskConfig(max_position_pct=1, max_total_position_pct=0.80)
    assert check(intent=intent, positions=positions, config=config).allowed is True


def test_total_position_limit_over_threshold_denied():
    """校验总持仓名义占比略超上限（0.801）时拒绝开仓。

    参数：无

    返回：
        None，断言 700 + 101 = 801 / 1000 超线被拒且理由含“总持仓”
    """
    # 700 + 101 = 801 > 800
    positions = [make_position(size=D(7), mark_price=D(100))]
    intent = make_intent(side_size=D(1), price=D(101), mark_price=D(101))
    verdict = check(intent=intent, positions=positions)
    assert verdict.allowed is False
    assert any("总持仓" in r for r in verdict.reasons)


def test_total_position_limit_exempts_close():
    """校验总持仓已超限（0.90）时平仓仍豁免放行。

    参数：无

    返回：
        None，断言超总持仓上限情况下的平仓单被允许
    """
    # 持仓已超限（900/1000），平仓仍放行
    positions = [make_position(size=D(9), mark_price=D(100))]
    intent = make_intent(is_close=True)
    assert check(intent=intent, positions=positions).allowed is True


# ---------- 杠杆上限 ----------


def test_leverage_equal_max_allowed():
    """校验杠杆恰好等于 max_leverage（5）时放行。

    参数：无

    返回：
        None，断言 5 倍杠杆开仓被允许（超过才拒绝）
    """
    assert check(intent=make_intent(leverage=5)).allowed is True


def test_leverage_over_max_denied():
    """校验杠杆超过 max_leverage（6 > 5）时拒绝开仓。

    参数：无

    返回：
        None，断言 6 倍杠杆开仓被拒且理由含“杠杆”
    """
    verdict = check(intent=make_intent(leverage=6))
    assert verdict.allowed is False
    assert any("杠杆" in r for r in verdict.reasons)


def test_leverage_exempts_close():
    """校验杠杆超限时平仓仍豁免放行。

    参数：无

    返回：
        None，断言 6 倍杠杆的平仓单被允许
    """
    assert check(intent=make_intent(leverage=6, is_close=True)).allowed is True


# ---------- 日亏损锁仓 ----------


def test_daily_loss_equal_limit_allowed():
    """校验当日已实现亏损恰好达到锁仓线（-10%）时仍可开仓。

    参数：无

    返回：
        None，断言已实现 -100 / 权益 1000 = -0.10 达线但被允许（超过才锁仓）
    """
    # 当日已实现 -100 / 权益 1000 = 0.10，恰好等于 daily_loss_limit，仍可开仓
    assert check(daily=make_daily(realized_pnl=D(-100))).allowed is True


def test_daily_loss_over_limit_denies_open():
    """校验当日已实现亏损略超锁仓线时拒绝开仓。

    参数：无

    返回：
        None，断言已实现 -100.01 超线被拒且理由含“锁仓”
    """
    verdict = check(daily=make_daily(realized_pnl=D("-100.01")))
    assert verdict.allowed is False
    assert any("锁仓" in r for r in verdict.reasons)


def test_daily_loss_counts_unrealised_pnl():
    """校验锁仓判定把未实现盈亏也计入当日亏损。

    参数：无

    返回：
        None，断言已实现 -50 加未实现 -60 合计 -110 超线被拒且理由含“锁仓”
    """
    # 已实现 -50 + 未实现 -60 = -110，超过 -100 锁仓线
    verdict = check(
        account=make_account(unrealised_pnl=D(-60)),
        daily=make_daily(realized_pnl=D(-50)),
    )
    assert verdict.allowed is False
    assert any("锁仓" in r for r in verdict.reasons)


def test_daily_loss_lock_allows_close():
    """校验日亏损已触发锁仓时平仓仍放行。

    参数：无

    返回：
        None，断言已实现 -200 锁仓状态下平仓单被允许
    """
    verdict = check(intent=make_intent(is_close=True), daily=make_daily(realized_pnl=D(-200)))
    assert verdict.allowed is True


# ---------- 日下单数上限 ----------


def test_max_orders_below_limit_allowed():
    """校验当日下单数未达上限（19 < 20）时放行。

    参数：无

    返回：
        None，断言第 20 单开仓被允许
    """
    assert check(daily=make_daily(orders_today=19)).allowed is True


def test_max_orders_at_limit_denies_open():
    """校验当日下单数已达上限（20）时拒绝开仓（计数上限，等于即用尽）。

    参数：无

    返回：
        None，断言第 21 单开仓被拒且理由含“下单数”
    """
    # 计数型上限：已达 20 单即额度用尽，拒绝第 21 单开仓
    verdict = check(daily=make_daily(orders_today=20))
    assert verdict.allowed is False
    assert any("下单数" in r for r in verdict.reasons)


def test_max_orders_exempts_close():
    """校验下单数已达上限时平仓仍豁免放行。

    参数：无

    返回：
        None，断言当日已下 20 单情况下平仓单被允许
    """
    verdict = check(intent=make_intent(is_close=True), daily=make_daily(orders_today=20))
    assert verdict.allowed is True


# ---------- 价格偏离保护 ----------


def test_deviation_market_order_exempt():
    """校验市价单无委托价，天然豁免价格偏离检查。

    参数：无

    返回：
        None，断言 price=None 的市价单被放行
    """
    assert check(intent=make_intent(price=None)).allowed is True


def test_deviation_equal_threshold_allowed():
    """校验委托价偏离标记价恰好等于上限（0.02）时放行。

    参数：无

    返回：
        None，断言偏离 2/100 = 0.02 达线但被允许（超过才拒绝）
    """
    # 偏离 2/100 = 0.02，恰好等于 max_deviation，放行
    assert check(intent=make_intent(price=D(102), mark_price=D(100))).allowed is True


def test_deviation_over_threshold_denied():
    """校验委托价偏离标记价超过上限（0.03）时拒绝。

    参数：无

    返回：
        None，断言偏离 3/100 超线被拒且理由含“偏离”
    """
    verdict = check(intent=make_intent(price=D(103), mark_price=D(100)))
    assert verdict.allowed is False
    assert any("偏离" in r for r in verdict.reasons)


def test_deviation_denies_close_too():
    """校验价格偏离保护不豁免平仓（防胖手指的唯一不豁免规则）。

    参数：无

    返回：
        None，断言平仓价偏离 20% 仍被拒且理由含“偏离”
    """
    # 价格偏离是平仓也不豁免的唯一规则（防胖手指）
    verdict = check(intent=make_intent(price=D(120), mark_price=D(100), is_close=True))
    assert verdict.allowed is False
    assert any("偏离" in r for r in verdict.reasons)


# ---------- 引擎汇总 ----------


def test_engine_aggregates_multiple_reasons():
    """校验引擎同时命中多条规则时把所有拒绝理由汇总返回。

    参数：无

    返回：
        None，断言白名单 + kill_switch + 杠杆 + 价格偏离四杀时理由恰好 4 条
    """
    # 同时命中：白名单 + kill_switch + 杠杆 + 价格偏离，理由全部汇总返回
    config = RiskConfig(kill_switch=True)
    intent = make_intent(contract="DOGE_USDT", leverage=6, price=D(103), mark_price=D(100))
    verdict = check(intent=intent, config=config)
    assert verdict.allowed is False
    assert len(verdict.reasons) == 4


# ---------- 研报方向闸门 ----------


def test_research_gate_exempts_close():
    """校验平仓/减仓永远豁免研报方向闸门（高置信反向也不拦）。

    参数：无

    返回：
        None，断言研报偏空时平仓单仍被放行
    """
    # 平仓/减仓永远豁免：即使高置信研报反向也不拦
    verdict = check(intent=make_intent(is_close=True), research_direction="偏空")
    assert verdict.allowed is True


def test_research_gate_none_direction_allowed():
    """校验闸门未传入方向（关闭/无研报/过期/降级）时不约束开仓。

    参数：无

    返回：
        None，断言 research_direction=None 时开仓被放行
    """
    # 闸门未传入方向（关闭/无研报/过期/降级）：不约束
    assert check(research_direction=None).allowed is True


def test_research_gate_neutral_allowed():
    """校验研报方向为中性时双向开仓均不受限。

    参数：无

    返回：
        None，断言中性方向下开仓被放行
    """
    assert check(research_direction="中性").allowed is True


def test_research_gate_bearish_denies_long():
    """校验高置信研报偏空时拦截反向开多。

    参数：无

    返回：
        None，断言开多被拒且理由含“闸门”
    """
    verdict = check(intent=make_intent(side_size=D(1)), research_direction="偏空")
    assert verdict.allowed is False
    assert any("闸门" in r for r in verdict.reasons)


def test_research_gate_bullish_denies_short():
    """校验高置信研报偏多时拦截反向开空。

    参数：无

    返回：
        None，断言开空被拒且理由含“闸门”
    """
    verdict = check(intent=make_intent(side_size=D(-1)), research_direction="偏多")
    assert verdict.allowed is False
    assert any("闸门" in r for r in verdict.reasons)


def test_research_gate_bearish_allows_short():
    """校验研报偏空时顺向开空放行。

    参数：无

    返回：
        None，断言偏空 + 开空被允许
    """
    # 顺向（研报偏空 + 开空）放行
    assert check(intent=make_intent(side_size=D(-1)), research_direction="偏空").allowed is True


def test_research_gate_bullish_allows_long():
    """校验研报偏多时顺向开多放行。

    参数：无

    返回：
        None，断言偏多 + 开多被允许
    """
    # 顺向（研报偏多 + 开多）放行
    assert check(intent=make_intent(side_size=D(1)), research_direction="偏多").allowed is True


def test_engine_check_passes_research_direction():
    """校验 engine.check 的 research_direction 入参确实组装进规则输入并生效。

    参数：无

    返回：
        None，断言偏空方向下开多被拒，且理由精确等于闸门拦截文案
    """
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


def test_position_limit_counts_same_contract_position():
    """单仓上限计入同合约已有持仓：拆单累计超限被拒（issue #57）。

    参数：无

    返回：
        None，断言同合约持仓 700+新单 100 超过 30% 上限被拒；跨合约持仓不计入
    """
    other = make_position(size=D(7), mark_price=D(100))
    other.contract = "ETH_USDT"  # 跨合约持仓不得计入 BTC 单仓敞口
    same = [make_position(size=D(7), mark_price=D(100))]
    intent = make_intent(side_size=D(1), price=D(100), mark_price=D(100))
    assert check(intent=intent, positions=same).allowed is False  # 同合约拆单超限
    assert check(intent=intent, positions=[other]).allowed is True  # 跨合约不误伤


def test_total_limit_counts_open_orders():
    """总仓敞口计入未成交挂单：持仓+挂单+本单超限被拒（issue #58）。

    参数：无

    返回：
        None，断言挂单名义计入总敞口后超限拒绝、无挂单时放行
    """
    from src.risk.models import OpenOrderIntent

    positions = [make_position(size=D(6), mark_price=D(100))]  # 600
    orders = [
        OpenOrderIntent(contract="BTC_USDT", price=D(100), size_left=D(1), quanto_multiplier=D(1))
    ]  # 挂单 100
    intent = make_intent(side_size=D(1), price=D(100), mark_price=D(100))  # 本单 100
    config = RiskConfig(max_position_pct=1, max_total_position_pct=0.80)  # 隔离单仓规则
    assert (
        check(intent=intent, positions=positions, open_orders=orders, config=config).allowed is True
    )  # 600+100+100=800，恰等于上限：放行（等于放行约定）
    big = [
        OpenOrderIntent(contract="BTC_USDT", price=D(100), size_left=D(2), quanto_multiplier=D(1))
    ]  # 挂单 200
    assert (
        check(
            intent=intent,
            positions=positions,
            open_orders=big,
            config=config,
        ).allowed
        is False
    )  # 900 > 800 拒绝


def test_open_orders_notional_filters_by_contract():
    """open_orders_notional 按 contract 过滤与全量合计。

    参数：无

    返回：
        None，断言指定合约只计自身挂单、None 计全部
    """
    from src.risk.rules import open_orders_notional

    zero = OpenOrderIntentFactory("BTC_USDT", D(0))  # 来源缺价兜底 0：跳过不计
    a = OpenOrderIntentFactory("BTC_USDT", D(100))
    b = OpenOrderIntentFactory("ETH_USDT", D(50))
    assert open_orders_notional([zero, a, b], "BTC_USDT") == D(100)  # 缺价挂单被跳过
    assert open_orders_notional([zero, a, b]) == D(150)


def OpenOrderIntentFactory(contract: str, price: Decimal):
    """构造最小挂单快照的测试辅助。

    参数：
        contract: str，合约名
        price: Decimal，挂单价

    返回：
        OpenOrderIntent：剩余 1 张、quanto=1 的挂单快照
    """
    from src.risk.models import OpenOrderIntent

    return OpenOrderIntent(contract=contract, price=price, size_left=D(1), quanto_multiplier=D(1))
