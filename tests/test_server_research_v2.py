"""研报 v2 API：列表给逐标的摘要，详情给安全字段且不泄漏原始市场快照。"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.config_io import write_settings
from src.memory import Database, Repo
from src.server.app import create_app
from src.server.deps import ServerDeps


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    """构造指向临时数据库的 Repo 实例，测试结束后关闭数据库连接。

    参数：
        tmp_path: Path，pytest 临时目录夹具，数据库文件落在其中

    返回：
        AsyncIterator[Repo]：已打开临时数据库的仓储对象
    """
    db = Database()
    await db.open(tmp_path / "research-v2.db")
    yield Repo(db)
    await db.close()


def _client(repo: Repo, tmp_path: Path) -> AsyncClient:
    """构造挂载临时配置的 FastAPI 测试客户端（ASGI 直连，不起真实端口）。

    参数：
        repo: Repo，仓储夹具，注入服务依赖
        tmp_path: Path，pytest 临时目录夹具，配置文件与提示词路径落在其中

    返回：
        AsyncClient：指向 create_app 所建应用的 HTTP 测试客户端
    """
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)
    deps = ServerDeps(
        repo=repo,
        config_path=config_path,
        prompt_path=tmp_path / "system_prompt.md",
        web_dist=tmp_path / "no-dist",
    )
    return AsyncClient(transport=ASGITransport(app=create_app(deps)), base_url="http://test")


async def test_v2_report_api_returns_asset_summaries_and_safe_detail(repo: Repo, tmp_path: Path):
    """校验研报 v2 接口：列表只给逐标的摘要字段，详情给安全字段且不泄漏原始市场快照。

    参数：
        repo: Repo，仓储夹具，预置一条含敏感 market_context_json 的研报
        tmp_path: Path，pytest 临时目录夹具，供测试客户端写配置

    返回：
        None，断言列表项为 schema v2 摘要、详情含 evidence/risks 且不含 secret_snapshot
    """
    report, _ = await repo.research.save_report_bundle(
        report_type="manual",
        summary="BTC 与 ETH 分化",
        cross_market_view="BTC 较强",
        global_risks_json='["美联储"]',
        raw_json='{"summary":"BTC 与 ETH 分化"}',
        round_id="r-v2",
        asset_views=[
            {
                "contract": "BTC_USDT",
                "direction": "偏多",
                "confidence": "高",
                "horizon": "3日",
                "market_regime": "上涨趋势",
                "technical_confirmation": "确认",
                "basis_type": "混合",
                "data_status": "完整",
                "evidence_json": '[{"point":"放量增仓","source":"4h"}]',
                "risks_json": '["资金费率偏高"]',
                "narrative": "BTC 结构获得催化验证",
                "market_context_json": '{"secret_snapshot":"仅后端复盘读取"}',
            }
        ],
    )
    async with _client(repo, tmp_path) as client:
        listed = (await client.get("/api/research/reports")).json()["items"][0]
        assert listed["schema_version"] == 2
        assert listed["asset_views"] == [
            {
                "contract": "BTC_USDT",
                "direction": "偏多",
                "confidence": "高",
                "horizon": "3日",
                "market_regime": "上涨趋势",
                "technical_confirmation": "确认",
                "basis_type": "混合",
                "data_status": "完整",
            }
        ]

        detail = (await client.get(f"/api/research/reports/{report.id}")).json()
        assert detail["summary"] == "BTC 与 ETH 分化"
        assert detail["global_risks"] == ["美联储"]
        assert detail["asset_views"][0]["evidence"] == [{"point": "放量增仓", "source": "4h"}]
        assert detail["asset_views"][0]["risks"] == ["资金费率偏高"]
        assert "market_context_json" not in detail["asset_views"][0]
        assert "secret_snapshot" not in str(detail)
