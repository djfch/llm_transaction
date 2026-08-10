"""ContextBuilder 单元测试：价格预警线段——内存唯一存储语义如实暴露给 LLM。

段标题标注「内存·重启即失效」；空列表显示（无）；有条目时按 above/below 用词逐行列出
（与 set_price_alert / cancel_price_alert 工具参数用词一致，LLM 可直接照抄参数调用取消）。

另覆盖行情段的指标短名单行：每合约 K 线摘要行后的第三行——短名单回调驱动内容、
服务为 None 时整行省略、服务异常时降级为提示行且不影响其余 section。
"""

from decimal import Decimal

from src.agent.context import ContextBuilder
from src.gateway.base import Candle
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.indicator_service import IndicatorService
from src.market.triggers import TriggerManager
from src.memory import Database, Repo


async def _build_text(triggers: TriggerManager, tmp_path, alerts_n: int = 20) -> str:
    """用给定触发器管理器构建一次 timer 上下文并返回其文本。

    参数：
        triggers: TriggerManager，注入 ContextBuilder 的价格预警触发器管理器
        tmp_path: Path，pytest 临时目录夹具，隔离的 agent.db 落在其中
        alerts_n: int，价格预警段最多展示的条数，默认 20

    返回：
        str：构建出的完整上下文文本
    """
    db = Database()
    await db.open(tmp_path / "agent.db")
    try:
        gateway = MockGateway()
        candles = CandleCache(gateway, ManualPriceSource())
        builder = ContextBuilder(
            gateway, Repo(db), candles, triggers, ["BTC_USDT"], alerts_n=alerts_n
        )
        return (await builder.build("timer")).text
    finally:
        await db.close()


async def test_alerts_section_empty(tmp_path):
    """校验无预警条目时段标题显示 0/10 条且内容占位为（无）。

    参数：
        tmp_path: Path，pytest 临时目录夹具，隔离的测试数据库落在其中

    返回：
        None，断言段标题含「0/10 条」且段内显示（无）
    """
    text = await _build_text(TriggerManager(lambda t, p: None), tmp_path)

    assert "## 价格预警线（内存·重启即失效，0/10 条）" in text
    assert "（无）" in text


async def test_alerts_section_lists_pending(tmp_path):
    """校验有待触发预警时按 above/below 用词逐行列出并标注设置时间。

    参数：
        tmp_path: Path，pytest 临时目录夹具，隔离的测试数据库落在其中

    返回：
        None，断言段标题含「2/10 条」、各条目为「合约 above/below 价格」格式
        且带「（设置于」时间标注
    """
    triggers = TriggerManager(lambda t, p: None)
    triggers.add("BTC_USDT", ">=", Decimal("70000"))
    triggers.add("ETH_USDT", "<=", Decimal("3000"))

    text = await _build_text(triggers, tmp_path)

    assert "## 价格预警线（内存·重启即失效，2/10 条）" in text
    assert "- BTC_USDT above 70000" in text
    assert "- ETH_USDT below 3000" in text
    # 条目带设置时间（_fmt_ts 的 MM-DD HH:MM 形态），供 LLM 判断新旧
    assert "（设置于" in text


async def test_alerts_section_truncates_to_alerts_n(tmp_path):
    """条数超 alerts_n 时截断：标题仍显示总数，尾部标注未显示条数（上下文有界性）。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    triggers = TriggerManager(lambda t, p: None)
    for price in ("10000", "20000", "30000"):
        triggers.add("BTC_USDT", ">=", Decimal(price))

    text = await _build_text(triggers, tmp_path, alerts_n=2)

    assert "## 价格预警线（内存·重启即失效，3/10 条）" in text  # 标题保留总数
    assert "- BTC_USDT above 10000" in text
    assert "- BTC_USDT above 20000" in text
    assert "- BTC_USDT above 30000" not in text  # 超出部分截断
    assert "另有 1 条未显示" in text


# ---------- 指标短名单行（行情段每合约第三行） ----------


class _FakeCandleCache:
    """内存 K 线缓存：与 CandleCache.get_recent 同签名（取尾 n 根）。"""

    def __init__(self, bars: list[Candle]) -> None:
        """以固定 K 线序列初始化缓存。

        参数：
            bars: list[Candle]，缓存持有的全部 K 线，get_recent 从中取尾部

        返回：
            None，副作用为把 K 线序列存入实例属性
        """
        self._bars = bars

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        """返回尾部 n 根 K 线，签名与 CandleCache.get_recent 一致。

        参数：
            contract: str，合约名，本假实现忽略
            interval: str，K 线周期，本假实现忽略
            n: int，取尾部多少根

        返回：
            list[Candle]：缓存序列尾部 n 根的副本
        """
        return list(self._bars)[-n:]


class _FakeOiCache:
    """内存 OI 缓存：与 OpenInterestCache.get 同签名。"""

    def __init__(self, values: dict[str, Decimal]) -> None:
        """以合约到持仓量的映射初始化缓存。

        参数：
            values: dict[str, Decimal]，合约名到持仓量的映射

        返回：
            None，副作用为把映射存入实例属性
        """
        self._values = values

    def get(self, contract: str) -> Decimal | None:
        """查询合约持仓量，签名与 OpenInterestCache.get 一致。

        参数：
            contract: str，合约名

        返回：
            Decimal | None：命中返回持仓量，未命中返回 None
        """
        return self._values.get(contract)


def _make_service(oi: Decimal | None = Decimal("8888")) -> IndicatorService:
    """60 根单调上行 1h K 线 + OI 缓存装配的服务（全部指标出值）。

    参数：
        oi: Decimal | None，持仓量缓存或假数据

    返回：
    IndicatorService：注入 60 根上行 K 线及可选持仓量的指标服务
    """
    bars = [
        Candle(
            t=1_700_000_000 + i * 3600,
            o=Decimal(100 + i),
            h=Decimal(101 + i),
            l=Decimal(99 + i),
            c=Decimal(100 + i),
            v=Decimal(10),
        )
        for i in range(60)
    ]
    return IndicatorService(
        _FakeCandleCache(bars), _FakeOiCache({"BTC_USDT": oi} if oi is not None else {})
    )


async def _build_text_with_indicators(tmp_path, service, shortlist=None) -> str:
    """带指标服务构建上下文；builder 自身 K 线缓存为空（摘要行固定「无 K 线数据」）。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具
        service: IndicatorService，指标服务测试替身
        shortlist: list[str]，需要展示的指标短名单

    返回：
    str：以 timer 唤醒原因构建、包含指标行的完整上下文文本
    """
    db = Database()
    await db.open(tmp_path / "agent.db")
    try:
        builder = ContextBuilder(
            MockGateway(),
            Repo(db),
            CandleCache(MockGateway(), ManualPriceSource()),
            TriggerManager(lambda t, p: None),
            ["BTC_USDT"],
            indicator_service=service,
            indicator_shortlist=shortlist,
        )
        return (await builder.build("timer")).text
    finally:
        await db.close()


async def test_indicator_line_after_candle_summary(tmp_path):
    """短名单行出现在每合约 K 线摘要行之后（第三行），缺省短名单取内置基线。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _build_text_with_indicators(tmp_path, _make_service())

    lines = text.splitlines()
    candle_idx = next(i for i, line in enumerate(lines) if "无 K 线数据" in line)
    indicator_line = lines[candle_idx + 1]
    assert indicator_line.startswith("BTC_USDT 指标(1h): ")
    assert "EMA20=" in indicator_line and "RSI14=" in indicator_line  # 基线短名单内容
    assert "持仓量=8888" in indicator_line  # oi 来自 OI 缓存


async def test_indicator_line_driven_by_shortlist_callback(tmp_path):
    """短名单回调返回值变化直接驱动行内容变化。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    keys = ["rsi14"]
    text = await _build_text_with_indicators(tmp_path, _make_service(), shortlist=lambda: keys)
    line = next(line for line in text.splitlines() if "指标(1h):" in line)
    assert "RSI14=" in line and "EMA20=" not in line

    keys[:] = ["macd"]
    text = await _build_text_with_indicators(tmp_path, _make_service(), shortlist=lambda: keys)
    line = next(line for line in text.splitlines() if "指标(1h):" in line)
    assert "MACD(dif/dea/hist)=" in line and "RSI14=" not in line


async def test_indicator_line_omitted_when_service_none(tmp_path):
    """indicator_service 为 None（装配未接）：整行省略，不留痕迹。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    text = await _build_text(TriggerManager(lambda t, p: None), tmp_path)
    assert "指标(1h):" not in text


async def test_indicator_line_degrades_on_service_error(tmp_path):
    """指标服务异常：降级为提示行（同 ticker 降级风格），其余 section 不受影响。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """

    class _BrokenService:
        def shortlist_line(self, contract: str, interval: str, keys: list[str]) -> str:
            """模拟指标服务故障：调用即抛错。

            参数：
                contract: str，合约名，未使用
                interval: str，K 线周期，未使用
                keys: list[str]，指标短名单键，未使用

            返回：
                str：不会真正返回，调用即抛出 RuntimeError

            异常：
                RuntimeError：无条件抛出，模拟「服务未就绪」故障
            """
            raise RuntimeError("服务未就绪")

    text = await _build_text_with_indicators(tmp_path, _BrokenService())
    assert "BTC_USDT 指标(1h): 暂不可用" in text
    assert "## 价格预警线" in text  # 后续 section 照常组装
