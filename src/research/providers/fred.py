"""FRED 宏观序列：免费 API key + httpx 直调 api.stlouisfed.org。

借鉴 TradingAgents fred.py（Apache-2.0）：别名映射 + Markdown 表格输出 + 30s 超时；
差异：异步 httpx、key 未配置返回中文提示（不抛错）。
"""

from __future__ import annotations

import os

import httpx

from src.research.providers.base import ResearchSourceError

BASE_URL = "https://api.stlouisfed.org/fred"
TIMEOUT = 30.0
MAX_ROWS = 40
DEFAULT_LOOKBACK = 365

# 友好别名 → FRED series ID（未列出的原样透传）
MACRO_SERIES: dict[str, str] = {
    "fed_funds_rate": "FEDFUNDS",
    "federal_funds_rate": "FEDFUNDS",
    "2y_treasury": "DGS2",
    "10y_treasury": "DGS10",
    "30y_treasury": "DGS30",
    "10y_2y_spread": "T10Y2Y",
    "yield_curve": "T10Y2Y",
    "cpi": "CPIAUCSL",
    "core_cpi": "CPILFESL",
    "pce": "PCEPI",
    "core_pce": "PCEPILFE",
    "inflation_expectations": "T10YIE",
    "real_gdp": "GDPC1",
    "unemployment_rate": "UNRATE",
    "unemployment": "UNRATE",
    "nonfarm_payrolls": "PAYEMS",
    "payrolls": "PAYEMS",
    "initial_claims": "ICSA",
    "m2": "M2SL",
    "money_supply": "M2SL",
    "vix": "VIXCLS",
    "dollar_index": "DTWEXBGS",
    "consumer_sentiment": "UMCSENT",
}


class FredSource:
    """FRED 宏观序列源。key 未配置时 get_macro_series 返回中文提示。"""

    def __init__(self, *, base_url: str = BASE_URL) -> None:
        """创建 FRED 宏观序列数据源实例。

        参数：
            base_url: str，FRED API 基础地址，省略时使用官方地址；末尾斜杠会被去掉

        返回：
            None，初始化实例状态（保存基础地址）
        """
        self._base = base_url.rstrip("/")

    def _api_key(self) -> str:
        """从环境变量读取 FRED API key。

        参数：无

        返回：
            str：FRED_API_KEY 环境变量的值；未设置时返回空字符串
        """
        return os.environ.get("FRED_API_KEY", "")

    async def get_macro_series(self, indicator: str, look_back: int = DEFAULT_LOOKBACK) -> str:
        """取一条宏观序列，返回 Markdown（最新值 + 窗口变化 + 最近观测表格）。

        参数：
            indicator: str，宏观指标名称或别名
            look_back: int，回看天数

        返回：
            str，取一条宏观序列，返回 Markdown（最新值 + 窗口变化 + 最近观测表格）

        异常：
            ResearchSourceError，FRED HTTP 请求或响应处理失败时抛出
        """
        key = self._api_key()
        if not key:
            return (
                "FRED 未配置：FRED_API_KEY 未设置。"
                "免费注册：https://fred.stlouisfed.org/docs/api/api_key.html，"
                "注册后填入项目 .env。"
            )
        series_id = self._resolve(indicator)
        if series_id is None:
            return (
                f"FRED：未知指标 {indicator!r}（可用别名：{'/'.join(sorted(MACRO_SERIES)[:8])} 等）"
            )
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                meta = await self._series_meta(client, series_id)
                observations = await self._observations(client, series_id, look_back)
        except httpx.HTTPError as exc:
            raise ResearchSourceError(f"FRED 请求失败：{exc}") from exc
        if not observations:
            return f"## FRED: {series_id}\n窗口内无观测值（可能是低频序列，可增大 look_back）。"
        title = meta.get("title", series_id)
        return self._render(title, series_id, observations)

    def _resolve(self, indicator: str) -> str | None:
        """把用户输入的指标名解析成 FRED 序列 ID。

        参数：
            indicator: str，指标名，支持友好别名（如 cpi、10y_treasury）或原始序列 ID

        返回：
            str | None：对应的 FRED 序列 ID；输入为空、过长或含空白无法识别时返回 None
        """
        key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
        if key in MACRO_SERIES:
            return MACRO_SERIES[key]
        candidate = indicator.strip().upper()
        if candidate and len(candidate) <= 30 and not any(c.isspace() for c in candidate):
            return candidate
        return None

    async def _series_meta(self, client: httpx.AsyncClient, series_id: str) -> dict:
        """请求一条序列的元信息（标题等）。

        参数：
            client: httpx.AsyncClient，用于发请求的异步 HTTP 客户端
            series_id: str，FRED 序列 ID

        返回：
            dict：序列元信息字典；接口未返回该序列时返回空字典

        异常：
            ResearchSourceError：响应体不是合法 JSON、解析失败时抛出
        """
        resp = await client.get(
            f"{self._base}/series",
            params={"series_id": series_id, "api_key": self._api_key(), "file_type": "json"},
        )
        resp.raise_for_status()
        try:
            rows = resp.json().get("seriess") or []
        except ValueError as exc:
            raise ResearchSourceError(f"FRED 响应解析失败：{exc}") from exc
        return rows[0] if rows else {}

    async def _observations(
        self, client: httpx.AsyncClient, series_id: str, look_back: int
    ) -> list[tuple[str, str]]:
        """拉取序列在回溯窗口内的观测值并剔除缺失点。

        参数：
            client: httpx.AsyncClient，用于发请求的异步 HTTP 客户端
            series_id: str，FRED 序列 ID
            look_back: int，从当前日期向前回溯的天数

        返回：
            list[tuple[str, str]]：按日期升序的（日期, 数值）观测点列表，
            已剔除缺失值（"."、空串、None）

        异常：
            ResearchSourceError：响应体不是合法 JSON、解析失败时抛出
        """
        from datetime import datetime, timedelta

        end = datetime.now()
        start = end - timedelta(days=look_back)
        resp = await client.get(
            f"{self._base}/series/observations",
            params={
                "series_id": series_id,
                "api_key": self._api_key(),
                "file_type": "json",
                "observation_start": start.strftime("%Y-%m-%d"),
                "observation_end": end.strftime("%Y-%m-%d"),
                "sort_order": "asc",
            },
        )
        resp.raise_for_status()
        try:
            observations = resp.json().get("observations", [])
        except ValueError as exc:
            raise ResearchSourceError(f"FRED 响应解析失败：{exc}") from exc
        points = [
            (o.get("date", ""), o.get("value", ""))
            for o in observations
            if o.get("value") not in (".", "", None)
        ]
        return points

    def _render(self, title: str, series_id: str, points: list[tuple[str, str]]) -> str:
        """把观测点渲染成 Markdown 报告（最新值 + 窗口变化 + 最近观测表格）。

        参数：
            title: str，序列标题，用于报告标题行
            series_id: str，FRED 序列 ID
            points: list[tuple[str, str]]，按日期升序的（日期, 数值）观测点，需非空

        返回：
            str：Markdown 文本，含最新值与窗口变化摘要、最近至多 MAX_ROWS 个观测点的表格
        """
        first_date, first_val = points[0]
        last_date, last_val = points[-1]
        try:
            delta = float(last_val) - float(first_val)
            pct = f" ({delta / float(first_val) * 100:+.2f}%)" if float(first_val) != 0 else ""
            summary = f"**最新：** {last_val}（{last_date}）| **窗口变化：** {delta:+.2f}{pct}"
        except ValueError:
            summary = f"**最新：** {last_val}（{last_date}）"
        shown = points[-MAX_ROWS:]
        note = (
            f"\n_（显示最近 {len(shown)}/{len(points)} 个观测点）_"
            if len(points) > MAX_ROWS
            else ""
        )
        table = "\n| 日期 | 数值 |\n| --- | --- |\n" + "\n".join(f"| {d} | {v} |" for d, v in shown)
        return f"## FRED: {title}（{series_id}）\n{summary}{note}\n{table}"
