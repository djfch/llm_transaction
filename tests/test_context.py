"""ContextBuilder 单元测试：价格预警线段——内存唯一存储语义如实暴露给 LLM。

段标题标注「内存·重启即失效」；空列表显示（无）；有条目时按 above/below 用词逐行列出
（与 set_price_alert / cancel_price_alert 工具参数用词一致，LLM 可直接照抄参数调用取消）。
"""

from decimal import Decimal

from src.agent.context import ContextBuilder
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo


async def _build_text(triggers: TriggerManager, tmp_path, alerts_n: int = 20) -> str:
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
    text = await _build_text(TriggerManager(lambda t, p: None), tmp_path)

    assert "## 价格预警线（内存·重启即失效，0/10 条）" in text
    assert "（无）" in text


async def test_alerts_section_lists_pending(tmp_path):
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
    """条数超 alerts_n 时截断：标题仍显示总数，尾部标注未显示条数（上下文有界性）。"""
    triggers = TriggerManager(lambda t, p: None)
    for price in ("10000", "20000", "30000"):
        triggers.add("BTC_USDT", ">=", Decimal(price))

    text = await _build_text(triggers, tmp_path, alerts_n=2)

    assert "## 价格预警线（内存·重启即失效，3/10 条）" in text  # 标题保留总数
    assert "- BTC_USDT above 10000" in text
    assert "- BTC_USDT above 20000" in text
    assert "- BTC_USDT above 30000" not in text  # 超出部分截断
    assert "另有 1 条未显示" in text
