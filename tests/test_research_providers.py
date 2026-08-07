"""研报数据源测试：金十/律动解析、FRED/Polymarket 渲染、时间解析、聚合器路由。

所有外部调用（MCP 会话 / httpx）均 mock，不触真实网络。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime

import pytest

from src.research.providers import fred, polymarket
from src.research.providers.base import (
    ResearchDataProvider,
    ResearchSourceError,
)
from src.research.providers.blockbeats import BlockbeatsSource
from src.research.providers.jin10 import BEIJING_TZ, Jin10Source, parse_ts
from src.research.providers.mcp_client import McpSession

# ---------- 时间解析 ----------


def test_parse_ts_formats() -> None:
    """支持 ISO 与 'YYYY-MM-DD HH:MM:SS'；空串/坏值用当前时间兜底。

    回归（M-TZ）：无时区串必须按北京时间（UTC+8）解释，与服务器本地时区
    无关——UTC 部署机上本测试在修复前必挂（旧实现按本地时区解释）。
    """
    assert (
        parse_ts("2026-08-05 20:30") == datetime(2026, 8, 5, 20, 30, tzinfo=BEIJING_TZ).timestamp()
    )
    assert (
        parse_ts("2026-08-05 20:30:00")
        == datetime(2026, 8, 5, 20, 30, tzinfo=BEIJING_TZ).timestamp()
    )
    assert parse_ts("0") == 0.0
    assert abs(parse_ts("") - datetime.now().timestamp()) < 5


def test_parse_ts_aware_iso_respects_own_tz() -> None:
    """复审 #9②：带 %z 的 ISO 串尊重自带时区（不套北京时间）。"""
    from datetime import timezone

    expected = datetime(2026, 8, 5, 20, 30, tzinfo=timezone.utc).timestamp()
    assert parse_ts("2026-08-05T20:30:00+00:00") == expected


# ---------- 金十源 ----------


def _fake_mcp(monkeypatch, responses: dict[str, str]):
    """同时 mock __aenter__（跳过真实连接）与 call_tool（按工具名返回预设）。"""

    async def fake_enter(self) -> McpSession:
        return self

    async def fake_call(self, name: str, args: dict | None = None) -> str:
        if name not in responses:
            raise ResearchSourceError(f"未预设工具 {name}")
        return responses[name]

    monkeypatch.setattr(McpSession, "__aenter__", fake_enter)
    monkeypatch.setattr(McpSession, "call_tool", fake_call)


async def test_jin10_calendar_parsing(monkeypatch) -> None:
    """日历解析：字段映射 + 星级 int 兜底。"""
    text = json.dumps(
        {
            "data": [
                {
                    "title": "美国7月非农就业人口",
                    "pub_time": "2026-08-07 20:30",
                    "star": 5,
                    "actual": None,
                    "consensus": "8.3",
                    "previous": "5.7",
                    "affect_txt": "未公布",
                },
                {"title": "坏星级", "star": "abc"},
            ]
        }
    )
    _fake_mcp(monkeypatch, {"list_calendar": text})
    src = Jin10Source(url="http://x", token="t")
    events = await src.fetch_calendar()
    assert len(events) == 2
    assert events[0].title == "美国7月非农就业人口"
    assert events[0].star == 5
    assert events[0].consensus == "8.3"
    assert events[1].star == 0  # 坏值兜底


async def test_jin10_flash_pagination(monkeypatch) -> None:
    """快讯分页：收集全部页后统一按时间窗口过滤（不依赖服务端排序假设）。"""
    import time as _time
    from datetime import datetime as _dt

    recent_str = _dt.fromtimestamp(_time.time() - 100).strftime("%Y-%m-%d %H:%M:%S")
    old_str = _dt.fromtimestamp(_time.time() - 2 * 86400).strftime("%Y-%m-%d %H:%M:%S")
    recent = json.dumps(
        {
            "data": [{"id": "1", "title": "近讯", "content": "内容", "time": recent_str}],
            "next_cursor": "c2",
        }
    )
    old = json.dumps({"data": [{"id": "2", "title": "旧讯", "content": "旧", "time": old_str}]})
    calls: list[str] = []

    async def fake_enter(self) -> McpSession:
        return self

    async def fake_call(self, name: str, args: dict | None = None) -> str:
        calls.append(name)
        return recent if len(calls) == 1 else old

    monkeypatch.setattr(McpSession, "__aenter__", fake_enter)
    monkeypatch.setattr(McpSession, "call_tool", fake_call)
    src = Jin10Source(url="http://x", token="t")
    items = await src.fetch_flash(hours=24)
    assert len(items) == 1
    assert items[0].title == "近讯"
    assert items[0].source == "jin10"
    assert len(calls) == 2  # 第二页遇旧讯即停


async def test_jin10_flash_int_title_coerced(monkeypatch) -> None:
    """回归（M3）：快讯缺 title、id 为 JSON 数字时，title 兜底必须为 str。

    修复前 title 是 int，聚合层 title[:40] 抛 TypeError，一行畸形数据废掉整轮研报。
    """
    recent_str = datetime.fromtimestamp(time.time() - 100).strftime("%Y-%m-%d %H:%M:%S")
    text = json.dumps({"data": [{"id": 360139, "content": "无标题快讯", "time": recent_str}]})
    _fake_mcp(monkeypatch, {"list_flash": text})
    src = Jin10Source(url="http://x", token="t")
    items = await src.fetch_flash(hours=24)
    assert len(items) == 1
    assert isinstance(items[0].title, str)
    assert items[0].title == "360139"
    assert items[0].title[:40] == "360139"  # 切片可用（聚合层去重键安全）


async def test_jin10_search_both_channels_down_raises(monkeypatch) -> None:
    """回归（M2）：搜索双通道全挂抛 ResearchSourceError，不伪装'未找到'。"""

    async def fake_enter(self) -> McpSession:
        return self

    async def fake_call(self, name: str, args: dict | None = None) -> str:
        raise ResearchSourceError(f"{name} 连接失败")

    monkeypatch.setattr(McpSession, "__aenter__", fake_enter)
    monkeypatch.setattr(McpSession, "call_tool", fake_call)
    src = Jin10Source(url="http://x", token="t")
    with pytest.raises(ResearchSourceError, match="双通道均失败"):
        await src.search_news("美联储")


async def test_jin10_search_one_channel_down_degrades(monkeypatch) -> None:
    """M2 配套：单通道失败降级——返回成功通道的结果，不抛错。"""
    recent_str = datetime.fromtimestamp(time.time() - 50).strftime("%Y-%m-%d %H:%M:%S")
    ok = json.dumps({"data": [{"id": "1", "title": "搜到", "content": "c", "time": recent_str}]})

    async def fake_enter(self) -> McpSession:
        return self

    async def fake_call(self, name: str, args: dict | None = None) -> str:
        if name == "search_flash":
            raise ResearchSourceError("search_flash 挂了")
        return ok

    monkeypatch.setattr(McpSession, "__aenter__", fake_enter)
    monkeypatch.setattr(McpSession, "call_tool", fake_call)
    src = Jin10Source(url="http://x", token="t")
    items = await src.search_news("美联储")
    assert len(items) == 1 and items[0].title == "搜到"


# ---------- 律动源 ----------


async def test_blockbeats_flash_parsing(monkeypatch) -> None:
    """24h 快讯解析：HTML 剥离 + 全文保留。"""
    text = json.dumps(
        {
            "page": 1,
            "data": [
                {
                    "id": 360139,
                    "title": "美联储消息",
                    "content": "<p>BlockBeats 消息，<b>加息</b>概率上升。</p>",
                    "link": "https://m.theblockbeats.info/flash/360139",
                    "create_time": "2026-08-05 17:50:32",
                }
            ],
        }
    )
    _fake_mcp(monkeypatch, {"get_newsflash_24h": text})
    src = BlockbeatsSource(cmd="npx -y blockbeats-mcp")
    items = await src.fetch_flash()
    assert len(items) == 1
    assert items[0].source == "blockbeats"
    assert "加息" in items[0].summary  # HTML 标签已剥离
    assert "<b>" in items[0].detail  # 全文保留原始 HTML
    assert items[0].published_at == datetime(2026, 8, 5, 17, 50, 32, tzinfo=BEIJING_TZ).timestamp()


async def test_blockbeats_flash_int_title_coerced(monkeypatch) -> None:
    """复审 #9①：律动数字 title 强制 str（与金十 M3 同族防御）。"""
    recent_str = datetime.fromtimestamp(time.time() - 100).strftime("%Y-%m-%d %H:%M:%S")
    text = json.dumps(
        {"page": 1, "data": [{"id": 1, "title": 12345, "content": "c", "create_time": recent_str}]}
    )
    _fake_mcp(monkeypatch, {"get_newsflash_24h": text})
    src = BlockbeatsSource(cmd="npx -y blockbeats-mcp")
    items = await src.fetch_flash()
    assert len(items) == 1
    assert isinstance(items[0].title, str) and items[0].title == "12345"


async def test_blockbeats_indicators_partial_failure(monkeypatch) -> None:
    """指标组：单工具失败标注不可用，不拖垮整组。"""
    ok = json.dumps({"value": 2.1})
    calls: list[str] = []

    async def fake_enter(self) -> McpSession:
        return self

    async def fake_call(self, name: str, args: dict | None = None) -> str:
        calls.append(name)
        if name == "get_btc_etf_flow":
            return ok
        raise ResearchSourceError("boom")

    monkeypatch.setattr(McpSession, "__aenter__", fake_enter)
    monkeypatch.setattr(McpSession, "call_tool", fake_call)
    src = BlockbeatsSource(cmd="npx")
    text = await src.fetch_indicators()
    assert "BTC ETF 净流入" in text
    assert "不可用" in text
    assert len(calls) == 8  # 全部工具都被尝试


# ---------- FRED ----------


async def test_fred_unknown_indicator(repo=None) -> None:
    """T11：FRED 未知指标/坏别名返回提示而非抛错（无需 key）。"""

    src = fred.FredSource()
    text = await src.get_macro_series("not a real indicator name at all")
    assert "未知指标" in text or "FRED" in text


async def test_polymarket_empty_result(monkeypatch) -> None:
    """T11：Polymarket 无匹配市场返回提示而非崩溃。"""

    class _EmptyResp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"events": []}

    async def fake_get(self, url, params=None, **kwargs):
        return _EmptyResp()

    monkeypatch.setattr(polymarket.httpx.AsyncClient, "get", fake_get)
    src = polymarket.PolymarketSource()
    text = await src.get_prediction_markets("不存在的话题xyz")
    assert "没有匹配" in text


# ---------- 审查补齐：T12 fetch_article_detail 各路径 ----------


async def test_article_detail_cache_hit(monkeypatch) -> None:
    """T12：缓存命中直接返回全文（不调金十）。"""
    from src.research.providers.base import FlashItem

    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    provider.flash_cache = {
        "c1": FlashItem(
            id="c1",
            source="jin10",
            title="t",
            summary="s",
            detail="缓存全文",
            url="",
            published_at=time.time() - 10,
        )
    }
    detail = await provider.fetch_article_detail("c1")
    assert detail == "缓存全文"


async def test_article_detail_fallback_jin10(monkeypatch) -> None:
    """T12：缓存未命中走金十详情。"""
    provider = ResearchDataProvider(jin10=_FakeJin10())
    detail = await provider.fetch_article_detail("j9")
    assert detail == "金十详情"


async def test_article_detail_blockbeats_refetch(monkeypatch) -> None:
    """T12：金十拒绝后走律动重拉兜底找到全文。"""

    class _BbDetail(_FakeBb):
        async def fetch_flash(self, hours=24):
            from src.research.providers.base import FlashItem

            return [
                FlashItem(
                    id="b42",
                    source="blockbeats",
                    title="律动文章",
                    summary="s",
                    detail="律动全文",
                    url="",
                    published_at=time.time() - 10,
                )
            ]

    class _Jin10NoDetail(_FakeJin10):
        async def fetch_article_detail(self, item_id):
            raise ResearchSourceError("金十无此 id")

    provider = ResearchDataProvider(jin10=_Jin10NoDetail(), blockbeats=_BbDetail())
    detail = await provider.fetch_article_detail("b42")
    assert detail == "律动全文"


async def test_article_detail_not_found(monkeypatch) -> None:
    """T12：全部兜底失败返回"未找到"哨兵。"""

    class _NoDetailJin10(_FakeJin10):
        async def fetch_article_detail(self, item_id):
            raise ResearchSourceError("金十无此 id")

    provider = ResearchDataProvider(jin10=_NoDetailJin10(), blockbeats=_FakeBb())
    from src.research.providers.base import ResearchSourceError as RSE

    try:
        await provider.fetch_article_detail("不存在id")
        assert False, "应抛 ResearchSourceError"
    except RSE as exc:
        assert "未找到" in str(exc)


async def test_fred_no_key_returns_guidance(monkeypatch) -> None:
    """key 未配置：返回中文提示而非抛错。"""
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    src = fred.FredSource()
    text = await src.get_macro_series("cpi")
    assert "FRED_API_KEY" in text and "免费注册" in text


async def test_fred_render(monkeypatch) -> None:
    """有 key：渲染最新值 + 窗口变化 + 表格（mock 内部请求）。"""
    monkeypatch.setenv("FRED_API_KEY", "fake")
    src = fred.FredSource()

    async def fake_meta(self, client, series_id):
        return {"title": "Consumer Price Index"}

    async def fake_obs(self, client, series_id, look_back):
        return [("2026-01-01", "310"), ("2026-07-01", "320")]

    monkeypatch.setattr(fred.FredSource, "_series_meta", fake_meta)
    monkeypatch.setattr(fred.FredSource, "_observations", fake_obs)
    text = await src.get_macro_series("cpi", look_back=180)
    assert "Consumer Price Index" in text
    assert "+10.00 (+3.23%)" in text  # 320-310=10, 10/310=3.23%
    assert "| 2026-07-01 | 320 |" in text


# ---------- Polymarket ----------


async def test_polymarket_rendering(monkeypatch) -> None:
    """预测市场渲染：过滤已关闭市场、按成交额排序、含概率与结算日。"""

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "events": [
                    {
                        "markets": [
                            {
                                "question": "Fed cut in Sep?",
                                "outcomePrices": '["0.76","0.24"]',
                                "outcomes": '["Yes","No"]',
                                "volumeNum": "500000",
                                "endDate": "2026-09-18T00:00:00Z",
                                "oneWeekPriceChange": 0.05,
                                "closed": False,
                            },
                            {
                                "question": "Closed market",
                                "outcomePrices": '["0.5","0.5"]',
                                "outcomes": '["Yes","No"]',
                                "volumeNum": "999999",
                                "endDate": "2026-01-01T00:00:00Z",
                                "closed": True,
                            },
                        ]
                    }
                ]
            }

    async def fake_get(self, url, params=None, **kwargs):
        return _Resp()

    monkeypatch.setattr(polymarket.httpx.AsyncClient, "get", fake_get)
    src = polymarket.PolymarketSource()
    text = await src.get_prediction_markets("Fed rate cut")
    assert "Fed cut in Sep?" in text
    assert "76%" in text
    assert "Closed market" not in text  # 已关闭被过滤


# ---------- 聚合器 ----------


class _FakeJin10:
    async def fetch_calendar(self):
        return []

    async def fetch_flash(self, hours=24):
        from src.research.providers.base import FlashItem

        return [
            FlashItem(
                id="j1",
                source="jin10",
                title="金十新闻",
                summary="s",
                detail="d",
                url="",
                published_at=time.time() - 100,
            )
        ]

    async def fetch_article_detail(self, item_id):
        return "金十详情"

    async def search_news(self, keyword, limit=20):
        return []


class _FakeBb:
    async def fetch_flash(self, hours=24):
        from src.research.providers.base import FlashItem

        return [
            FlashItem(
                id="b1",
                source="blockbeats",
                title="金十新闻",
                summary="s2",
                detail="d2",
                url="",
                published_at=time.time() - 100,
            )
        ]  # 同秒同标题 → 去重

    async def fetch_indicators(self):
        return "指标快照"

    async def search_news(self, keyword, limit=20):
        return []


async def test_aggregator_merge_dedup() -> None:
    """聚合器：双源快讯按 时间+标题 去重合并。"""
    provider = ResearchDataProvider(jin10=_FakeJin10(), blockbeats=_FakeBb())
    items = await provider.fetch_flash()
    assert len(items) == 1  # 重复被去重
    assert items[0].source == "jin10"  # 先到者保留
    assert provider.flash_cache["j1"].detail == "d"


async def test_aggregator_missing_source_raises() -> None:
    """聚合器：未装配源调用时报错（工具层转哨兵）。"""
    provider = ResearchDataProvider()
    with pytest.raises(ResearchSourceError):
        await provider.fetch_calendar()


class _BrokenSource:
    async def fetch_flash(self, hours=24):
        raise ResearchSourceError("连接失败")

    async def search_news(self, keyword, limit=20):
        raise ResearchSourceError("连接失败")


async def test_aggregator_all_sources_down_raises() -> None:
    """双源全挂：抛 ResearchSourceError（回归 H2：不得伪装'无快讯'）。"""
    provider = ResearchDataProvider(jin10=_BrokenSource(), blockbeats=_BrokenSource())
    with pytest.raises(ResearchSourceError, match="全部不可用"):
        await provider.fetch_flash()
    with pytest.raises(ResearchSourceError, match="全部不可用"):
        await provider.search_news("美联储")


async def test_aggregator_one_source_down_degrades() -> None:
    """单源失败：另一源数据仍可用（降级不中断）。"""
    from src.research.providers.base import FlashItem

    class _OkSource:
        async def fetch_flash(self, hours=24):
            return [
                FlashItem(
                    id="ok1",
                    source="blockbeats",
                    title="可用新闻",
                    summary="s",
                    detail="d",
                    url="",
                    published_at=time.time() - 100,
                )
            ]

    provider = ResearchDataProvider(jin10=_BrokenSource(), blockbeats=_OkSource())
    items = await provider.fetch_flash()
    assert len(items) == 1 and items[0].title == "可用新闻"


# ---------- MCP stdio 命令平台拆分 ----------


def test_stdio_command_platform_split(monkeypatch) -> None:
    """回归（M1）：stdio 命令按平台拆分——Windows 走 cmd /c，POSIX 直接 exec。

    修复前硬编码 cmd /c，Linux 部署机（systemd）上律动源必然连接失败。
    """
    from src.research.providers.mcp_client import _stdio_command

    monkeypatch.setattr(sys, "platform", "win32")
    command, args = _stdio_command("npx -y blockbeats-mcp")
    assert (command, args) == ("cmd", ["/c", "npx", "-y", "blockbeats-mcp"])

    # 复审 #3：Windows 路径型自定义命令不被 shlex 吃反斜杠拆坏
    command, args = _stdio_command(r"C:\tools\bb-mcp.cmd -y")
    assert (command, args) == ("cmd", ["/c", r"C:\tools\bb-mcp.cmd", "-y"])

    monkeypatch.setattr(sys, "platform", "linux")
    command, args = _stdio_command("npx -y blockbeats-mcp")
    assert (command, args) == ("npx", ["-y", "blockbeats-mcp"])


def test_stdio_command_empty_raises() -> None:
    """复审 #9③：空/纯空白命令抛 ResearchSourceError（__init__ 的 not cmd 拦不住 ' '）。"""
    from src.research.providers.mcp_client import _stdio_command

    with pytest.raises(ResearchSourceError):
        _stdio_command("   ")
