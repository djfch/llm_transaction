"""get_indicators 工具测试：经 ToolRegistry 调用，覆盖面板渲染与各类中文错误文本。

工具只依赖 watchlist 与 indicator_service，其余依赖空占位（同 test_utils_calc 模式）；
指标服务用假 K 线/OI 缓存装配（同 test_indicator_service 模式），不触网不触库。
"""

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
        self._bars = bars

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        return list(self._bars)[-n:]


class FakeOiCache:
    """内存 OI 缓存：与 OpenInterestCache.get 同签名。"""

    def __init__(self, values: dict[str, Decimal]) -> None:
        self._values = values

    def get(self, contract: str) -> Decimal | None:
        return self._values.get(contract)


class BrokenCandleCache:
    """get_recent 直接抛异常：模拟指标服务未就绪/缓存故障。"""

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        raise RuntimeError("缓存未就绪")


def make_candles(n: int, start: int = 1_700_000_000) -> list[Candle]:
    """n 根 1h K 线：收盘价单调上行（100 起），时间升序。"""
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


def _registry(service: IndicatorService | None, watchlist: list[str] | None = None) -> ToolRegistry:
    none = SimpleNamespace()
    deps = ToolDeps(
        gateway=none,
        risk_engine=none,
        risk_config=none,
        watchlist=watchlist if watchlist is not None else [BTC],
        repo=none,
        candles=none,
        triggers=none,
        indicator_service=service,
        daily_stats_fn=None,
    )
    return ToolRegistry(deps)


def _service(n_candles: int = 60, oi: Decimal | None = Decimal("123456")) -> IndicatorService:
    oi_map = {} if oi is None else {BTC: oi}
    return IndicatorService(FakeCandleCache(make_candles(n_candles)), FakeOiCache(oi_map))


async def test_full_panel_rendered():
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
    out = await _registry(_service(oi=None)).execute("get_indicators", {"contract": BTC})
    assert "持仓量: 无数据" in out.text  # oi 无数据如实显示


async def test_insufficient_candles_show_no_data():
    """K 线不足指标所需深度时该指标显示 无数据，其余正常出值。"""
    out = await _registry(_service(n_candles=10)).execute("get_indicators", {"contract": BTC})
    assert "EMA50(指数均线): 无数据" in out.text  # min_candles=50，10 根不够
    ema9 = next(line for line in out.text.splitlines() if line.startswith("EMA9(指数均线)"))
    assert "无数据" not in ema9


async def test_contract_not_in_watchlist():
    out = await _registry(_service()).execute("get_indicators", {"contract": "DOGE_USDT"})
    assert "参数错误" in out.text and "不在白名单" in out.text


async def test_invalid_interval():
    out = await _registry(_service()).execute("get_indicators", {"contract": BTC, "interval": "3h"})
    assert "参数错误" in out.text and "interval" in out.text


async def test_service_none_reports_unavailable():
    out = await _registry(None).execute("get_indicators", {"contract": BTC})
    assert "错误" in out.text and "指标服务未接入" in out.text


async def test_service_failure_returns_error_text_not_raised():
    """指标服务内部异常：转成中文错误文本返回（不向上抛，不中断本轮）。"""
    service = IndicatorService(BrokenCandleCache(), FakeOiCache({}))
    out = await _registry(service).execute("get_indicators", {"contract": BTC})
    assert "错误：指标计算失败" in out.text and "缓存未就绪" in out.text
