"""交易写操作与 K 线端点测试：手动平仓、模拟账户重置、agent 启停、K 线查询。

httpx ASGITransport + fake 依赖注入（tmp_path 隔离真实配置）。覆盖：
- 未接线 503、模式冲突 409、风控拒绝/参数非法 422、网关错误 502
- paper reset 的 config.yaml 写回与 runtime_settings 原地同步（同一实例）
- agent 启停后 agent_running 从 status_provider 读真实状态（不硬编码）
"""

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from src.config import Settings, load_settings
from src.config_io import write_settings
from src.gateway.base import Candle, GatewayError, OrderNotFound, OrderResult
from src.memory.db import Database
from src.memory.repo import Repo
from src.server.app import create_app
from src.server.deps import ServerDeps

BTC = "BTC_USDT"

_CLOSE_RESULT = {"contract": BTC, "status": "finished", "fill_price": 60100.5, "text": "已平仓"}


class FakeGateway:
    """注入用假网关：返回固定 K 线序列并记录调用参数。"""

    def __init__(self) -> None:
        self.candle_calls: list[dict] = []
        self.open_order_calls: list[dict] = []
        self.orders = [
            OrderResult(
                id="o-1",
                contract=BTC,
                status="open",
                size=Decimal("-2"),
                left=Decimal("2"),
                price=Decimal("59000"),
                tif="gtc",
                reduce_only=True,
                fill_price=Decimal(0),
            )
        ]

    def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        self.candle_calls.append({"contract": contract, "interval": interval, "limit": limit})
        return [
            Candle(
                t=3600,
                o=Decimal("60000.5"),
                h=Decimal("60100"),
                l=Decimal("59900"),
                c=Decimal("60050"),
                v=Decimal("12"),
            )
        ]

    def list_orders(
        self,
        contract: str | None = None,
        status: str = "open",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[OrderResult]:
        self.open_order_calls.append(
            {"contract": contract, "status": status, "limit": limit, "offset": offset}
        )
        orders = [order for order in self.orders if order.status == status]
        return orders[offset:] if limit is None else orders[offset : offset + limit]


@pytest.fixture
async def deps(tmp_path: Path):
    """组装 fake 依赖：tmp 配置/名单文件 + 内存 DB + 可调假的写操作回调。"""
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)  # 默认 paper 配置
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(
        yaml.safe_dump({"settle": "usdt", "contracts": [BTC]}), encoding="utf-8"
    )
    db = Database()
    await db.open(tmp_path / "t.db")
    repo = Repo(db)

    # close_impl 可在用例中替换为抛错版本；running 由启停 fake 翻转，status_provider 读它
    state: dict = {"running": False, "close_impl": None, "cancel_impl": None}
    close_calls: list[str] = []
    cancel_calls: list[tuple[str, str]] = []
    resets: list[Decimal] = []

    async def _default_close(contract: str) -> dict:
        return _CLOSE_RESULT

    state["close_impl"] = _default_close

    async def manual_close(contract: str) -> dict:
        close_calls.append(contract)
        return await state["close_impl"](contract)

    async def _default_cancel(contract: str, order_id: str) -> dict:
        return {
            "id": order_id,
            "contract": contract,
            "status": "finished",
            "finish_as": "cancelled",
            "warning": "",
        }

    state["cancel_impl"] = _default_cancel

    async def manual_cancel_order(contract: str, order_id: str) -> dict:
        cancel_calls.append((contract, order_id))
        return await state["cancel_impl"](contract, order_id)

    async def agent_start() -> None:
        state["running"] = True

    async def agent_stop() -> None:
        state["running"] = False

    d = ServerDeps(
        repo=repo,
        gateway=FakeGateway(),
        status_provider=lambda: {"agent_running": state["running"]},
        runtime_settings=Settings(),  # 默认 mode=paper
        runtime_watchlist=[BTC],
        config_path=config_path,
        watchlist_path=watchlist_path,
        web_dist=tmp_path / "no_dist",
        manual_close=manual_close,
        manual_cancel_order=manual_cancel_order,
        paper_reset=resets.append,
        agent_start=agent_start,
        agent_stop=agent_stop,
    )
    d.state = state  # 测试断言用
    d.close_calls = close_calls
    d.cancel_calls = cancel_calls
    d.resets = resets
    yield d
    await db.close()


@pytest.fixture
async def client(deps: ServerDeps):
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------- POST /api/positions/{contract}/close ----------


async def test_close_position_success(client: AsyncClient, deps: ServerDeps):
    r = await client.post(f"/api/positions/{BTC}/close")
    assert r.status_code == 200
    assert r.json() == _CLOSE_RESULT  # 成功原样返回 manual_close 结果
    assert deps.close_calls == [BTC]


async def test_close_position_503_when_not_wired(client: AsyncClient, deps: ServerDeps):
    deps.manual_close = None
    r = await client.post(f"/api/positions/{BTC}/close")
    assert r.status_code == 503


async def test_close_position_422_on_risk_rejection(client: AsyncClient, deps: ServerDeps):
    async def rejected(contract: str) -> dict:
        raise RuntimeError("风控拒绝：单日最大下单次数已达上限")

    deps.state["close_impl"] = rejected
    r = await client.post(f"/api/positions/{BTC}/close")
    assert r.status_code == 422
    assert "单日最大下单次数" in r.json()["detail"]  # 异常消息即原因文本


async def test_close_position_502_on_gateway_error(client: AsyncClient, deps: ServerDeps):
    async def gateway_fail(contract: str) -> dict:
        raise GatewayError("持仓不存在", label="POSITION_NOT_FOUND")

    deps.state["close_impl"] = gateway_fail
    r = await client.post(f"/api/positions/{BTC}/close")
    assert r.status_code == 502
    assert "持仓不存在" in r.json()["detail"]


# ---------- POST /api/paper/reset ----------
async def test_open_orders_lists_all_pages(client: AsyncClient, deps: ServerDeps):
    seed = deps.gateway.orders[0]
    deps.gateway.orders = [seed.model_copy(update={"id": f"o-{index}"}) for index in range(101)]
    response = await client.get("/api/open_orders")
    assert response.status_code == 200
    assert len(response.json()) == 101
    first = response.json()[0]
    assert first["size"] == "-2"
    assert first["price"] == "59000"
    assert first["tif"] == "gtc" and first["reduce_only"] is True
    assert deps.gateway.open_order_calls == [
        {"contract": None, "status": "open", "limit": 100, "offset": 0},
        {"contract": None, "status": "open", "limit": 100, "offset": 100},
    ]


async def test_cancel_open_order_success(client: AsyncClient, deps: ServerDeps):
    response = await client.delete(f"/api/orders/{BTC}/o-1")
    assert response.status_code == 200
    assert response.json()["warning"] == ""
    assert deps.cancel_calls == [(BTC, "o-1")]


async def test_cancel_open_order_keeps_gateway_success_when_local_sync_warns(
    client: AsyncClient, deps: ServerDeps
):
    async def warned(contract: str, order_id: str) -> dict:
        return {
            "id": order_id,
            "contract": contract,
            "status": "finished",
            "finish_as": "cancelled",
            "warning": "local order sync failed; do not retry",
        }

    deps.state["cancel_impl"] = warned
    response = await client.delete(f"/api/orders/{BTC}/o-1")

    assert response.status_code == 200
    assert response.json()["warning"] == "local order sync failed; do not retry"


async def test_cancel_open_order_503_when_not_wired(client: AsyncClient, deps: ServerDeps):
    deps.manual_cancel_order = None
    response = await client.delete(f"/api/orders/{BTC}/o-1")
    assert response.status_code == 503


async def test_cancel_open_order_502_on_gateway_error(client: AsyncClient, deps: ServerDeps):
    async def failed(contract: str, order_id: str) -> dict:
        raise GatewayError("order missing", label="ORDER_NOT_FOUND")

    deps.state["cancel_impl"] = failed
    response = await client.delete(f"/api/orders/{BTC}/o-1")
    assert response.status_code == 502


async def test_cancel_open_order_409_when_order_is_no_longer_open(
    client: AsyncClient, deps: ServerDeps
):
    async def already_closed(contract: str, order_id: str) -> dict:
        raise OrderNotFound("order is no longer open", label="ORDER_NOT_FOUND")

    deps.state["cancel_impl"] = already_closed
    response = await client.delete(f"/api/orders/{BTC}/o-1")

    assert response.status_code == 409
    assert response.json()["detail"] == "order is no longer open"


async def test_paper_reset_success(client: AsyncClient, deps: ServerDeps):
    r = await client.post("/api/paper/reset", json={"equity": 20000})
    assert r.status_code == 200
    assert r.json() == {"equity": 20000.0}
    assert deps.resets == [Decimal("20000")]  # paper_reset 以 Decimal 调用
    # runtime_settings 原地更新（同一实例，下轮决策即生效）
    assert deps.runtime_settings.paper.initial_equity == Decimal("20000")
    # config.yaml 已写回
    assert load_settings(deps.config_path).paper.initial_equity == Decimal("20000")


async def test_paper_reset_409_when_not_paper_mode(client: AsyncClient, deps: ServerDeps):
    deps.runtime_settings = Settings(mode="testnet")
    r = await client.post("/api/paper/reset", json={"equity": 20000})
    assert r.status_code == 409
    assert deps.resets == []  # 未触发重置


async def test_paper_reset_409_when_reset_not_wired(client: AsyncClient, deps: ServerDeps):
    deps.paper_reset = None
    r = await client.post("/api/paper/reset", json={"equity": 20000})
    assert r.status_code == 409


async def test_open_orders_maps_gateway_error_to_502(client: AsyncClient, deps: ServerDeps):
    def fail_list_orders(*_args, **_kwargs):
        raise GatewayError("gateway unavailable")

    deps.gateway.list_orders = fail_list_orders
    response = await client.get("/api/open_orders")

    assert response.status_code == 502
    assert response.json()["detail"] == "gateway unavailable"


async def test_paper_reset_422_on_non_positive_equity(client: AsyncClient, deps: ServerDeps):
    for bad in (0, -5):
        r = await client.post("/api/paper/reset", json={"equity": bad})
        assert r.status_code == 422
    assert deps.resets == []


# ---------- POST /api/agent/start|stop ----------


async def test_agent_start_stop_reflects_real_state(client: AsyncClient, deps: ServerDeps):
    r = await client.post("/api/agent/start")
    assert r.json() == {"agent_running": True}  # 从 status_provider 读真实状态
    assert deps.state["running"] is True
    r = await client.post("/api/agent/stop")
    assert r.json() == {"agent_running": False}
    assert deps.state["running"] is False


async def test_agent_start_stop_503_when_not_wired(client: AsyncClient, deps: ServerDeps):
    deps.agent_start = None
    deps.agent_stop = None
    assert (await client.post("/api/agent/start")).status_code == 503
    assert (await client.post("/api/agent/stop")).status_code == 503


# ---------- GET /api/candles ----------


async def test_candles_success(client: AsyncClient, deps: ServerDeps):
    r = await client.get("/api/candles", params={"contract": BTC})
    assert r.status_code == 200
    assert r.json()["items"] == [
        {"t": 3600, "o": 60000.5, "h": 60100.0, "l": 59900.0, "c": 60050.0, "v": 12.0}
    ]
    # 默认 interval=1h、limit=200 透传网关
    assert deps.gateway.candle_calls == [{"contract": BTC, "interval": "1h", "limit": 200}]


async def test_candles_interval_whitelist(client: AsyncClient):
    for ok in ("1m", "5m", "15m", "1h", "4h", "1d"):
        r = await client.get("/api/candles", params={"contract": BTC, "interval": ok})
        assert r.status_code == 200
    r = await client.get("/api/candles", params={"contract": BTC, "interval": "3h"})
    assert r.status_code == 422


async def test_candles_422_for_contract_outside_watchlist(client: AsyncClient):
    r = await client.get("/api/candles", params={"contract": "DOGE_USDT"})
    assert r.status_code == 422


async def test_candles_limit_validation(client: AsyncClient):
    for bad in (0, 1001):
        r = await client.get("/api/candles", params={"contract": BTC, "limit": bad})
        assert r.status_code == 422
    r = await client.get("/api/candles", params={"contract": BTC, "limit": 1000})
    assert r.status_code == 200


async def test_candles_503_when_gateway_missing(client: AsyncClient, deps: ServerDeps):
    deps.gateway = None
    r = await client.get("/api/candles", params={"contract": BTC})
    assert r.status_code == 503


async def test_candles_502_on_gateway_error(client: AsyncClient, deps: ServerDeps):
    def boom(*args, **kwargs):
        raise GatewayError("交易所限流")

    deps.gateway.get_candlesticks = boom
    r = await client.get("/api/candles", params={"contract": BTC})
    assert r.status_code == 502
    assert "交易所限流" in r.json()["detail"]


async def test_candles_watchlist_falls_back_to_file(client: AsyncClient, deps: ServerDeps):
    deps.runtime_watchlist = None  # 未接线运行时名单：读 watchlist.yaml
    assert (await client.get("/api/candles", params={"contract": BTC})).status_code == 200
    assert (await client.get("/api/candles", params={"contract": "ETH_USDT"})).status_code == 422
