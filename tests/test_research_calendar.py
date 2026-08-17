"""官方市场日历解析、缓存与降级语义测试。"""

import json
import re
from datetime import date
from pathlib import Path

import pytest

from src.research.calendars import MarketCalendarProvider, parse_jpx, parse_lse, parse_nyse


FIXTURES = Path(__file__).parent / "fixtures" / "calendar"


def _today() -> date:
    """返回夹具覆盖的固定 UTC+8 当前日期。

    参数：无

    返回：
        date：固定为 2026-08-17
    """
    return date(2026, 8, 17)


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
    jpx = parse_jpx(_fixture("jpx.html"))[2026]
    lse = parse_lse(_fixture("lse.json"))[2026]
    nyse = parse_nyse(_fixture("nyse.html"))[2026]
    assert {"2026-01-01", "2026-02-23"} <= jpx and len(jpx) == 10
    assert "2026-08-31" in lse and "2026-12-24" not in lse and len(lse) == 6
    assert {"2026-01-01", "2026-04-03"} <= nyse and len(nyse) == 9


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

    provider = MarketCalendarProvider(tmp_path / "calendar.json", fetcher=fetch, today=_today)
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
    provider = MarketCalendarProvider(path, fetcher=seed, today=_today)
    await provider.refresh()

    degraded = MarketCalendarProvider(path, fetcher=recoverable, today=_today)
    await degraded.refresh()
    assert degraded.is_trading_day("XTKS", date(2026, 1, 1)) is False
    assert degraded.status()["state"] == "fallback"

    empty = MarketCalendarProvider(tmp_path / "empty.json", fetcher=fail, today=_today)
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
    await MarketCalendarProvider(path, fetcher=seed, today=_today).refresh()
    provider = MarketCalendarProvider(path, fetcher=partial, today=_today)
    await provider.refresh()

    assert provider.is_trading_day("XTKS", date(2026, 2, 23)) is False
    assert set(provider.status()["errors"]) == {"XTKS"}
    assert provider.status()["state"] == "fallback"


@pytest.mark.asyncio
async def test_partial_year_refresh_preserves_complete_market_cache(tmp_path: Path):
    """新页面缺少下一年度时不得覆盖该市场原有完整缓存。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：断言残缺 JPX 结果降级且保留 2027 休市日
    """
    pages = {
        "XTKS": _fixture("jpx.html"),
        "XLON": _fixture("lse.json"),
        "XNYS": _fixture("nyse.html"),
    }

    async def seed(market: str, _url: str) -> str:
        """返回完整的当前年与下一年夹具。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：对应市场完整夹具
        """
        return pages[market]

    async def missing_next_year(market: str, _url: str) -> str:
        """仅让 JPX 新页面缺失下一年度。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：残缺 JPX 或其他市场完整夹具
        """
        if market != "XTKS":
            return pages[market]
        return (
            '<h2 class="heading-title"><span>2026</span></h2>'
            "<table><tr><td>Jan. 1 (Thu.)</td><td>New Year</td></tr></table>"
        )

    path = tmp_path / "calendar.json"
    await MarketCalendarProvider(path, fetcher=seed, today=_today).refresh()
    provider = MarketCalendarProvider(path, fetcher=missing_next_year, today=_today)
    await provider.refresh()

    assert provider.is_trading_day("XTKS", date(2027, 1, 1)) is False
    assert set(provider.status()["errors"]) == {"XTKS"}
    assert provider.status()["state"] == "fallback"


@pytest.mark.asyncio
async def test_current_year_is_kept_when_next_year_is_not_published(tmp_path: Path):
    """空缓存且下一年未发布时，当前年已知休市日仍须生效并持久化。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：断言当前年证据保留、下一年降级且重载后一致
    """
    pages = {
        "XTKS": _fixture("jpx.html").split('<h2 class="heading-title"><span>2027</span></h2>')[0],
        "XLON": _fixture("lse.json"),
        "XNYS": _fixture("nyse.html"),
    }

    async def current_only(market: str, _url: str) -> str:
        """让东京来源只发布当前年，其他市场保持完整。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：对应市场页面夹具
        """
        return pages[market]

    path = tmp_path / "calendar.json"
    provider = MarketCalendarProvider(path, fetcher=current_only, today=_today)

    result = await provider.refresh()

    assert result.complete is False
    assert provider.is_trading_day("XTKS", date(2026, 1, 1)) is False
    assert provider.is_trading_day("XTKS", date(2027, 1, 4)) is True
    assert provider.status()["state"] == "fallback"
    reloaded = MarketCalendarProvider(path, today=_today)
    assert reloaded.is_trading_day("XTKS", date(2026, 1, 1)) is False
    assert reloaded.is_trading_day("XNYS", date(2027, 1, 1)) is False


@pytest.mark.asyncio
async def test_empty_cache_rejects_undersized_current_year(tmp_path: Path):
    """空缓存时当前年休市日低于数量下限不得被接纳为完整结果。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：断言刷新不完整、东京降级报错且下一年完整数据仍生效
    """
    jpx = _fixture("jpx.html")
    undersized_current = (
        jpx[:48] + "<table><tr><td><td>Jan. 1 (Thu.)</td><td>New Year</td></tr></table>" + jpx[647:]
    )

    async def fetch(market: str, _url: str) -> str:
        """让东京当前年仅剩 1 个休市日，其余市场保持完整夹具。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：残缺 JPX 或其他市场完整夹具
        """
        if market == "XTKS":
            return undersized_current
        return _fixture({"XLON": "lse.json", "XNYS": "nyse.html"}[market])

    provider = MarketCalendarProvider(tmp_path / "calendar.json", fetcher=fetch, today=_today)

    result = await provider.refresh()

    assert result.complete is False
    assert provider.status()["state"] == "fallback"
    assert "XTKS" in provider.status()["errors"]
    assert provider.is_trading_day("XTKS", date(2027, 1, 1)) is False


@pytest.mark.asyncio
async def test_threshold_sized_partial_year_cannot_shrink_old_cache(tmp_path: Path):
    """新结果恰好达到数量阈值但少于旧缓存时，不得静默删除休市日。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：断言旧日期保留、状态降级且重载后一致
    """
    pages = {
        "XTKS": _fixture("jpx.html"),
        "XLON": _fixture("lse.json"),
        "XNYS": _fixture("nyse.html"),
    }

    async def seed(market: str, _url: str) -> str:
        """返回含纽约下一年 9 个休市日的完整夹具。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：对应市场完整夹具
        """
        return pages[market]

    partial_nyse = pages["XNYS"].replace("<td>Monday, September 6</td>", "<td>Unavailable</td>")

    async def shrink_next_year(market: str, _url: str) -> str:
        """让纽约下一年结果从 9 天缩至阈值 8 天。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：缩水纽约或其他市场完整夹具
        """
        return partial_nyse if market == "XNYS" else pages[market]

    path = tmp_path / "calendar.json"
    await MarketCalendarProvider(path, fetcher=seed, today=_today).refresh()
    provider = MarketCalendarProvider(path, fetcher=shrink_next_year, today=_today)

    result = await provider.refresh()

    assert result.complete is False
    assert provider.is_trading_day("XNYS", date(2027, 9, 6)) is False
    assert provider.status()["state"] == "fallback"
    reloaded = MarketCalendarProvider(path, today=_today)
    assert reloaded.is_trading_day("XNYS", date(2027, 9, 6)) is False


@pytest.mark.asyncio
async def test_complete_next_year_superset_restores_ok_state(tmp_path: Path):
    """下一年完整结果是旧缓存超集时，应接纳新增日期并恢复正常状态。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：断言 8 天缓存升级为 9 天后 complete 与状态均正常
    """
    pages = {
        "XTKS": _fixture("jpx.html"),
        "XLON": _fixture("lse.json"),
        "XNYS": _fixture("nyse.html"),
    }
    eight_day_nyse = pages["XNYS"].replace("<td>Monday, September 6</td>", "<td>Unavailable</td>")

    async def partial_seed(market: str, _url: str) -> str:
        """首次给纽约下一年返回达到下限的 8 个日期。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：8 天纽约或其他市场完整夹具
        """
        return eight_day_nyse if market == "XNYS" else pages[market]

    async def complete(market: str, _url: str) -> str:
        """第二次返回包含旧集合的完整超集。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：对应市场完整夹具
        """
        return pages[market]

    path = tmp_path / "calendar.json"
    await MarketCalendarProvider(path, fetcher=partial_seed, today=_today).refresh()
    provider = MarketCalendarProvider(path, fetcher=complete, today=_today)

    result = await provider.refresh()

    assert result.complete is True
    assert provider.status()["state"] == "ok"
    assert provider.is_trading_day("XNYS", date(2027, 9, 6)) is False


@pytest.mark.asyncio
async def test_shrunken_year_with_false_date_cannot_poison_cache(tmp_path: Path):
    """缩水结果混入错误日期时不得写入，官方恢复后应重新变为正常。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：断言错误日期始终不进入缓存且后续完整刷新恢复 ok
    """
    pages = {
        "XTKS": _fixture("jpx.html"),
        "XLON": _fixture("lse.json"),
        "XNYS": _fixture("nyse.html"),
    }

    async def seed(market: str, _url: str) -> str:
        """返回含纽约下一年 9 个休市日的完整夹具。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：对应市场完整夹具
        """
        return pages[market]

    poisoned = (
        pages["XNYS"]
        .replace("<td>Monday, September 6</td>", "<td>Unavailable</td>")
        .replace(
            "</tbody>",
            "<tr><th>Malformed row</th><td>Unavailable</td>"
            "<td>Friday, December 31</td></tr></tbody>",
        )
    )
    state = {"poisoned": True}

    async def recoverable(market: str, _url: str) -> str:
        """先返回缩水且混入错误日期的纽约页面，随后恢复完整页面。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：当前阶段对应的市场页面
        """
        if market == "XNYS" and state["poisoned"]:
            return poisoned
        return pages[market]

    path = tmp_path / "calendar.json"
    await MarketCalendarProvider(path, fetcher=seed, today=_today).refresh()
    provider = MarketCalendarProvider(path, fetcher=recoverable, today=_today)
    degraded = await provider.refresh()
    assert degraded.complete is False
    assert provider.is_trading_day("XNYS", date(2027, 12, 31)) is True

    state["poisoned"] = False
    recovered = await provider.refresh()

    assert recovered.complete is True
    assert provider.status()["state"] == "ok"
    assert provider.is_trading_day("XNYS", date(2027, 12, 31)) is True
    assert provider.is_trading_day("XNYS", date(2027, 9, 6)) is False


@pytest.mark.asyncio
async def test_rolling_current_year_page_merges_with_existing_cache(tmp_path: Path):
    """官方页面仅保留当前年未来日期时，应合并而不是删除旧休市日。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：断言刷新完整成功且旧的元旦休市日仍保留
    """
    pages = {
        "XTKS": _fixture("jpx.html"),
        "XLON": _fixture("lse.json"),
        "XNYS": _fixture("nyse.html"),
    }

    async def seed(market: str, _url: str) -> str:
        """返回完整夹具以建立旧缓存。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：对应市场完整夹具
        """
        return pages[market]

    payload = json.loads(pages["XLON"])
    table = payload["components"][0]["content"][0]["value"]["table"]
    table = re.sub(r"<tr><th>.*? 2026</th>.*?</tr>", "", table)
    rolling_row = (
        "<tr><th>Monday 31 August 2026</th><td>Summer Bank Holiday</td>"
        "<td>NON-trading day.</td></tr>"
    )
    payload["components"][0]["content"][0]["value"]["table"] = table.replace(
        "<table>", f"<table>{rolling_row}"
    )
    rolling_lse = json.dumps(payload)

    async def rolling(market: str, _url: str) -> str:
        """让 LSE 当前年只返回剩余日期，下一年仍完整。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：滚动 LSE 或其他市场完整夹具
        """
        return rolling_lse if market == "XLON" else pages[market]

    path = tmp_path / "calendar.json"
    await MarketCalendarProvider(path, fetcher=seed, today=_today).refresh()
    provider = MarketCalendarProvider(path, fetcher=rolling, today=_today)

    result = await provider.refresh()

    assert result.complete is True
    assert provider.is_trading_day("XLON", date(2026, 1, 1)) is False
    assert provider.status()["state"] == "ok"


@pytest.mark.parametrize("payload", [[], None, {"holidays": []}])
def test_valid_json_with_invalid_cache_shape_degrades_safely(tmp_path: Path, payload: object):
    """合法 JSON 但结构错误的缓存不得阻止应用构造。

    参数：
        tmp_path: Path，隔离的日历缓存目录
        payload: object，合法 JSON 的错误缓存结构

    返回：
        None：断言构造成功、状态报错且未知工作日按开市降级
    """
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    provider = MarketCalendarProvider(path)

    assert provider.status()["state"] == "error"
    assert provider.is_trading_day("XNYS", date(2026, 8, 17)) is True


@pytest.mark.asyncio
async def test_cache_write_failure_is_reported_without_replacing_memory(tmp_path: Path):
    """缓存写盘失败时刷新不得伪报完整成功或抛出顶层异常。

    参数：
        tmp_path: Path，隔离的日历缓存目录

    返回：
        None：断言结果暴露写盘失败且状态进入 error
    """
    pages = {
        "XTKS": _fixture("jpx.html"),
        "XLON": _fixture("lse.json"),
        "XNYS": _fixture("nyse.html"),
    }

    async def fetch(market: str, _url: str) -> str:
        """返回三个市场的完整夹具。

        参数：
            market: str，市场代码
            _url: str，官方来源地址（夹具不使用）

        返回：
            str：对应市场完整夹具
        """
        return pages[market]

    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    provider = MarketCalendarProvider(blocked_parent / "calendar.json", fetcher=fetch, today=_today)

    result = await provider.refresh()

    assert result.cache_saved is False
    assert result.complete is False
    assert "cache" in result.failed
    assert provider.status()["state"] == "error"
