"""get_indicators 工具测试：经 ToolRegistry 调用，覆盖面板渲染与各类中文错误文本。

工具只依赖 watchlist 与 indicator_service，其余依赖空占位（同 test_utils_calc 模式）；
指标服务用假 K 线/OI 缓存装配（同 test_indicator_service 模式），不触网不触库。
"""

import time
from decimal import Decimal
from types import SimpleNamespace

from src.agent.tool_handlers import ToolDeps
from src.agent.tools import ToolRegistry
from src.gateway.base import Candle
from src.market.indicator_service import REGISTRY, IndicatorService

BTC = "BTC_USDT"


class FakeCandleCache:
    """内存 K 线缓存：与 CandleCache.get_recent 同签名（取尾 n 根）。"""

    def __init__(self, bars: list[Candle]) -> None:
        """保存预置 K 线序列，供 get_recent 截取。

        参数：
            bars: list[Candle]，预置的 K 线列表（时间升序）

        返回：
            None，就地初始化实例字段 self._bars
        """
        self._bars = bars

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        """返回预置 K 线的尾部 n 根，contract 与 interval 仅作签名占位。

        参数：
            contract: str，合约名；假缓存只有一份数据，忽略
            interval: str，K 线周期；假缓存只有一份数据，忽略
            n: int，截取尾部根数

        返回：
            list[Candle]：预置列表尾 n 根的副本
        """
        return list(self._bars)[-n:]


class FakeOiCache:
    """内存 OI 缓存：与 OpenInterestCache.get 同签名。"""

    def __init__(self, values: dict[str, Decimal]) -> None:
        """保存合约到持仓量的预置映射，供 get 查询。

        参数：
            values: dict[str, Decimal]，合约名到持仓量的预置映射

        返回：
            None，就地初始化实例字段 self._values
        """
        self._values = values

    def get(self, contract: str) -> Decimal | None:
        """查询预置映射中该合约的持仓量。

        参数：
            contract: str，合约名

        返回：
            Decimal | None：预置的持仓量；未预置时为 None
        """
        return self._values.get(contract)


class BrokenCandleCache:
    """get_recent 直接抛异常：模拟指标服务未就绪/缓存故障。"""

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        """任何调用都直接抛异常，模拟缓存未就绪/指标服务故障。

        参数：
            contract: str，合约名（未使用，调用即抛异常）
            interval: str，K 线周期（未使用，调用即抛异常）
            n: int，请求根数（未使用，调用即抛异常）

        返回：
            list[Candle]：无实际返回，调用即抛 RuntimeError

        异常：
            RuntimeError：固定抛出“缓存未就绪”，模拟指标服务缓存故障
        """
        raise RuntimeError("缓存未就绪")


def make_candles(n: int, start: int | None = None) -> list[Candle]:
    """n 根 1h K 线：收盘价单调上行（100 起），时间升序。

    start 缺省时取"当前整点往前 n 根"——issue #74 停更判定下，
    固定旧时间戳的 K 线会被工具出口拒绝。

    参数：
        n: int，需要读取或生成的记录数量
        start: int | None，K 线起始时间戳；None 时取当前整点前推 n 小时

    返回：
        list[Candle]，按小时升序排列且收盘价逐根递增的 K 线列表
    """
    if start is None:
        import time

        start = int(time.time()) // 3600 * 3600 - n * 3600
    return [
        Candle(
            t=start + i * 3600,
            o=Decimal(100 + i),
            h=Decimal(101 + i),
            l=Decimal(99 + i),
            c=Decimal(100 + i),
            v=Decimal(10),
        )
        for i in range(n)
    ]


def _registry(
    service: IndicatorService | None,
    watchlist: list[str] | None = None,
    candles: "FakeCandleCache | None" = None,
) -> ToolRegistry:
    """装配只接指标服务的 ToolRegistry，其余依赖用空对象占位。

    参数：
        service: IndicatorService | None，指标服务实例；None 用于模拟服务未接入
        watchlist: list[str] | None，合约白名单；None 时默认只含 BTC_USDT
        candles: FakeCandleCache | None，K 线缓存替身；None 时默认 60 根新鲜数据

    返回：
        ToolRegistry：可执行 get_indicators 的工具注册表
    """
    none = SimpleNamespace()
    deps = ToolDeps(
        gateway=none,
        risk_engine=none,
        risk_config=none,
        watchlist=watchlist if watchlist is not None else [BTC],
        repo=none,
        candles=candles if candles is not None else FakeCandleCache(make_candles(60)),
        triggers=none,
        indicator_service=service,
        daily_stats_fn=None,
    )
    return ToolRegistry(deps)


def _service(n_candles: int = 60, oi: Decimal | None = Decimal("123456")) -> IndicatorService:
    """用内存假 K 线/OI 缓存装配指标服务实例。

    参数：
        n_candles: int，预置 1h K 线根数，默认 60 根足够全部指标出值
        oi: Decimal | None，BTC_USDT 的预置持仓量；None 表示 OI 缓存无数据

    返回：
        IndicatorService：基于假缓存、不触网不触库的指标服务
    """
    oi_map = {} if oi is None else {BTC: oi}
    return IndicatorService(FakeCandleCache(make_candles(n_candles)), FakeOiCache(oi_map))


async def test_full_panel_rendered():
    """验证指标工具渲染完整面板。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    out = await _registry(_service()).execute("get_indicators", {"contract": BTC})

    assert out.text.startswith(f"{BTC} 技术指标（1h")
    assert {item.label for item in REGISTRY.values()} <= {
        line.split(":")[0] for line in out.text.splitlines()[1:]
    }  # 注册表全指标逐行出现
    ema20 = next(line for line in out.text.splitlines() if line.startswith("EMA20(指数均线)"))
    assert "无数据" not in ema20  # 60 根足够全部 K 线指标出值
    macd = next(line for line in out.text.splitlines() if line.startswith("MACD(异同均线)"))
    assert "dif=" in macd and "dea=" in macd and "hist=" in macd  # 多值指标一行列子字段
    assert "持仓量: 123456" in out.text  # oi 来自 OI 缓存


async def test_oi_none_shows_no_data():
    """验证持仓量缺失时面板显示无数据。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    out = await _registry(_service(oi=None)).execute("get_indicators", {"contract": BTC})
    assert "持仓量: 无数据" in out.text  # oi 无数据如实显示


async def test_insufficient_candles_show_no_data():
    """K 线不足指标所需深度时该指标显示 无数据，其余正常出值。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    out = await _registry(_service(n_candles=10)).execute("get_indicators", {"contract": BTC})
    assert "EMA50(指数均线): 无数据" in out.text  # min_candles=50，10 根不够
    ema9 = next(line for line in out.text.splitlines() if line.startswith("EMA9(指数均线)"))
    assert "无数据" not in ema9


async def test_contract_not_in_watchlist():
    """验证指标工具拒绝关注列表外合约。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    out = await _registry(_service()).execute("get_indicators", {"contract": "DOGE_USDT"})
    assert "参数错误" in out.text and "不在白名单" in out.text


async def test_invalid_interval():
    """验证指标工具拒绝非法 K 线周期。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    out = await _registry(_service()).execute("get_indicators", {"contract": BTC, "interval": "3h"})
    assert "参数错误" in out.text and "interval" in out.text


async def test_service_none_reports_unavailable():
    """验证指标服务未接线时返回不可用提示。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    out = await _registry(None).execute("get_indicators", {"contract": BTC})
    assert "错误" in out.text and "指标服务未接入" in out.text


async def test_service_failure_returns_error_text_not_raised():
    """指标服务内部异常：转成中文错误文本返回（不向上抛，不中断本轮）。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    service = IndicatorService(BrokenCandleCache(), FakeOiCache({}))
    out = await _registry(service).execute("get_indicators", {"contract": BTC})
    assert "错误：指标计算失败" in out.text and "缓存未就绪" in out.text


async def test_get_indicators_stale_returns_unavailable():
    """K 线停更时 get_indicators 直接返回不可用文案，不输出旧指标值（issue #97）。

    参数：无

    返回：
        None，断言停更缓存下返回「K 线数据不可用」且不含 EMA 值与指标面板
    """
    stale_start = int(time.time()) // 3600 * 3600 - 65 * 3600  # 最后一根收盘在 ~5h 前
    stale_cache = FakeCandleCache(make_candles(60, start=stale_start))
    registry = _registry(_service(), candles=stale_cache)
    out = await registry.execute("get_indicators", {"contract": BTC})
    assert "K 线数据不可用" in out.text and "停更" in out.text
    assert "EMA20" not in out.text and "技术指标" not in out.text
