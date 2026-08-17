"""官方市场日历解析、缓存与降级语义测试。"""

from datetime import date
from pathlib import Path

import pytest

from src.research.calendars import MarketCalendarProvider, parse_jpx, parse_lse, parse_nyse


FIXTURES = Path(__file__).parent / "fixtures" / "calendar"


def _fixture(name: str) -> str:
    """读取官方页面最小测试夹具。

    参数：
        name: str，夹具文件名

    返回：
        str：UTF-8 夹具内容
    """
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_official_calendar_parsers_extract_full_day_closures():
    """三个官方来源均解析完整休市日，伦敦半日市不误判为休市。

    参数：无

    返回：
        None：通过已知日期字面量校验三个解析器
    """
    assert parse_jpx(_fixture("jpx.html"))[2026] == {"2026-01-01", "2026-02-23"}
    assert parse_lse(_fixture("lse.json"))[2026] == {"2026-08-31"}
    assert parse_nyse(_fixture("nyse.html"))[2026] == {"2026-01-01", "2026-04-03"}


def test_calendar_parser_rejects_missing_year_data():
    """来源页面没有年度有效休市日时拒绝伪造成功结果。

    参数：无

    返回：
        None：断言空页面解析失败
    """
    for parser in (parse_jpx, parse_lse, parse_nyse):
        with pytest.raises(ValueError):
            parser("<html>no calendar</html>")


@pytest.mark.asyncio
async def test_provider_uses_cache_and_unknown_weekday_defaults_open(tmp_path: Path):
    """刷新成功写缓存；后续无覆盖年份时工作日默认开市、周末仍休市。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：通过公开 refresh/is_trading_day/status 校验缓存与降级
    """
    pages = {
        "XTKS": _fixture("jpx.html"),
        "XLON": _fixture("lse.json"),
        "XNYS": _fixture("nyse.html"),
    }

    async def fetch(market: str, _url: str) -> str:
        """返回指定市场的固定官方页面夹具。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：对应市场页面内容
        """
        return pages[market]

    provider = MarketCalendarProvider(tmp_path / "calendar.json", fetcher=fetch)
    await provider.refresh()
    assert provider.is_trading_day("XTKS", date(2026, 1, 1)) is False
    assert provider.is_trading_day("XLON", date(2026, 12, 24)) is True
    assert provider.is_trading_day("XNYS", date(2026, 4, 3)) is False
    assert provider.status()["state"] == "ok"

    cached = MarketCalendarProvider(tmp_path / "calendar.json")
    assert cached.is_trading_day("XTKS", date(2026, 2, 23)) is False
    assert cached.is_trading_day("XNYS", date(2030, 1, 7)) is True
    assert cached.is_trading_day("XNYS", date(2030, 1, 5)) is False
    assert cached.status()["state"] == "fallback"


@pytest.mark.asyncio
async def test_failed_refresh_keeps_cache_and_reports_degraded_state(tmp_path: Path):
    """网络或页面结构失败时保留旧缓存；完全无缓存时明确报告 error。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：校验缓存不被失败结果替换以及状态分级
    """
    pages = {
        "XTKS": _fixture("jpx.html"),
        "XLON": _fixture("lse.json"),
        "XNYS": _fixture("nyse.html"),
    }

    async def seed(market: str, _url: str) -> str:
        """返回可解析页面以建立缓存。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：对应市场的有效夹具
        """
        return pages[market]

    async def fail(_market: str, _url: str) -> str:
        """模拟所有官方来源读取失败。

        参数：
            _market: str，市场代码
            _url: str，官方来源地址

        返回：
            str：此边界始终抛错，不会返回

        异常：
            RuntimeError：固定模拟网络失败
        """
        raise RuntimeError("network down")

    recovery = {"failing": True}

    async def recoverable(market: str, _url: str) -> str:
        """先模拟失败，切换测试状态后恢复有效页面。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：恢复后对应市场的有效夹具

        异常：
            RuntimeError：测试仍处于失败阶段时抛出
        """
        if recovery["failing"]:
            raise RuntimeError("network down")
        return pages[market]

    path = tmp_path / "calendar.json"
    provider = MarketCalendarProvider(path, fetcher=seed)
    await provider.refresh()

    degraded = MarketCalendarProvider(path, fetcher=recoverable)
    await degraded.refresh()
    assert degraded.is_trading_day("XTKS", date(2026, 1, 1)) is False
    assert degraded.status()["state"] == "fallback"

    empty = MarketCalendarProvider(tmp_path / "empty.json", fetcher=fail)
    await empty.refresh()
    assert empty.is_trading_day("XNYS", date(2026, 1, 2)) is True
    assert empty.status()["state"] == "error"

    degraded.is_trading_day("XNYS", date(2030, 1, 7))
    assert degraded.status()["state"] == "fallback"
    recovery["failing"] = False
    await degraded.refresh()
    assert degraded.status()["state"] == "ok"


@pytest.mark.asyncio
async def test_single_source_failure_preserves_only_its_old_cache(tmp_path: Path):
    """单一官方来源失败时保留该市场旧缓存，其他来源仍可独立刷新。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：断言逐来源错误、旧 JPX 休市日和降级状态
    """
    pages = {
        "XTKS": _fixture("jpx.html"),
        "XLON": _fixture("lse.json"),
        "XNYS": _fixture("nyse.html"),
    }

    async def seed(market: str, _url: str) -> str:
        """返回三个市场的有效夹具。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：对应市场夹具
        """
        return pages[market]

    async def partial(market: str, _url: str) -> str:
        """仅让 JPX 页面结构损坏，其他来源保持成功。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：损坏 JPX 或其他市场有效夹具
        """
        return "<html>broken</html>" if market == "XTKS" else pages[market]

    path = tmp_path / "calendar.json"
    await MarketCalendarProvider(path, fetcher=seed).refresh()
    provider = MarketCalendarProvider(path, fetcher=partial)
    await provider.refresh()

    assert provider.is_trading_day("XTKS", date(2026, 2, 23)) is False
    assert set(provider.status()["errors"]) == {"XTKS"}
    assert provider.status()["state"] == "fallback"
