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
        """初始化假网关：预置一条 BTC 未成交空单并准备好调用记录列表。

        参数：无

        返回：
            None，就地初始化 candle_calls/open_order_calls 记录与 orders 列表
        """
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
                stop_loss_price=Decimal("58000"),
                take_profit_price=Decimal("62000"),
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
        """记录 K 线查询参数并返回一根固定的 BTC 测试 K 线。

        参数：
            contract: str，请求查询的合约名称
            interval: str，请求的 K 线周期，默认 1m
            limit: int | None，请求返回的最大根数
            from_ts: int | None，起始时间戳，本桩不读取该值
            to_ts: int | None，结束时间戳，本桩不读取该值

        返回：
            list[Candle]，仅含一根固定行情数据的列表
        """
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
        """记录订单查询参数并按状态和分页边界返回预置订单。

        参数：
            contract: str | None，合约过滤条件，本桩不按该字段过滤
            status: str，订单状态过滤条件，默认 open
            limit: int | None，单页最大订单数
            offset: int，分页起始偏移量

        返回：
            list[OrderResult]，按状态筛选并切片后的预置订单列表
        """
        self.open_order_calls.append(
            {"contract": contract, "status": status, "limit": limit, "offset": offset}
        )
        orders = [order for order in self.orders if order.status == status]
        return orders[offset:] if limit is None else orders[offset : offset + limit]


@pytest.fixture
async def deps(tmp_path: Path):
    """组装隔离配置、白名单、数据库和可替换交易回调的服务器依赖。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        AsyncIterator[ServerDeps]，生成测试依赖并在用例结束后关闭数据库
    """
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
        """返回固定的手动平仓成功结果。

        参数：
            contract: str，请求平仓的合约，本桩固定返回 BTC 结果

        返回：
            dict，包含成交状态、成交价和提示文本的平仓结果
        """
        return _CLOSE_RESULT

    state["close_impl"] = _default_close

    async def manual_close(contract: str) -> dict:
        """记录手动平仓合约并委托当前可替换实现。

        参数：
            contract: str，请求平仓的合约名称

        返回：
            dict，当前 close_impl(平仓实现)返回的结果
        """
        close_calls.append(contract)
        return await state["close_impl"](contract)

    async def _default_cancel(contract: str, order_id: str) -> dict:
        """返回固定的人工撤单成功结果。

        参数：
            contract: str，订单所属合约
            order_id: str，待撤销订单编号

        返回：
            dict，表示订单已取消且没有本地同步警告的结果
        """
        return {
            "id": order_id,
            "contract": contract,
            "status": "finished",
            "finish_as": "cancelled",
            "warning": "",
        }

    state["cancel_impl"] = _default_cancel

    async def manual_cancel_order(contract: str, order_id: str) -> dict:
        """记录人工撤单目标并委托当前可替换实现。

        参数：
            contract: str，订单所属合约
            order_id: str，待撤销订单编号

        返回：
            dict，当前 cancel_impl(撤单实现)返回的结果
        """
        cancel_calls.append((contract, order_id))
        return await state["cancel_impl"](contract, order_id)

    async def agent_start() -> None:
        """把测试运行状态切换为已启动。

        参数：无

        返回：
            None，副作用为把 state.running(运行状态)设为 True
        """
        state["running"] = True

    async def agent_stop() -> None:
        """把测试运行状态切换为已停止。

        参数：无

        返回：
            None，副作用为把 state.running(运行状态)设为 False
        """
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
    """构造直连交易测试应用的异步 HTTP 客户端。

    参数：
        deps: ServerDeps，交易端点测试依赖

    返回：
        AsyncIterator[AsyncClient]，生成无需真实网络端口的测试客户端
    """
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------- POST /api/positions/{contract}/close ----------


async def test_close_position_success(client: AsyncClient, deps: ServerDeps):
    """验证手动平仓端点原样返回成功结果并记录目标合约。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供平仓调用记录的服务器依赖

    返回：
        None，通过断言验证状态码、响应载荷和调用记录
    """
    r = await client.post(f"/api/positions/{BTC}/close")
    assert r.status_code == 200
    assert r.json() == _CLOSE_RESULT  # 成功原样返回 manual_close 结果
    assert deps.close_calls == [BTC]


async def test_close_position_503_when_not_wired(client: AsyncClient, deps: ServerDeps):
    """验证未接入手动平仓回调时端点返回 503。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可移除平仓回调的服务器依赖

    返回：
        None，通过断言验证服务不可用状态码
    """
    deps.manual_close = None
    r = await client.post(f"/api/positions/{BTC}/close")
    assert r.status_code == 503


async def test_close_position_422_on_risk_rejection(client: AsyncClient, deps: ServerDeps):
    """验证手动平仓被风控拒绝时映射为包含原因的 422 响应。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可替换平仓实现的服务器依赖

    返回：
        None，通过断言验证状态码和风控原因文本
    """

    async def rejected(contract: str) -> dict:
        """模拟风控拒绝手动平仓请求。

        参数：
            contract: str，请求平仓的合约，本桩不读取其内容

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            RuntimeError: 每次调用均携带风控拒绝原因抛出
        """
        raise RuntimeError("风控拒绝：单日最大下单次数已达上限")

    deps.state["close_impl"] = rejected
    r = await client.post(f"/api/positions/{BTC}/close")
    assert r.status_code == 422
    assert "单日最大下单次数" in r.json()["detail"]  # 异常消息即原因文本


async def test_close_position_502_on_gateway_error(client: AsyncClient, deps: ServerDeps):
    """验证手动平仓发生交易所错误时映射为 502 响应。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可替换平仓实现的服务器依赖

    返回：
        None，通过断言验证状态码和网关错误文本
    """

    async def gateway_fail(contract: str) -> dict:
        """模拟交易所返回持仓不存在错误。

        参数：
            contract: str，请求平仓的合约，本桩不读取其内容

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            GatewayError: 每次调用均抛出持仓不存在错误
        """
        raise GatewayError("持仓不存在", label="POSITION_NOT_FOUND")

    deps.state["close_impl"] = gateway_fail
    r = await client.post(f"/api/positions/{BTC}/close")
    assert r.status_code == 502
    assert "持仓不存在" in r.json()["detail"]


# ---------- GET /api/open_orders / DELETE /api/orders/{contract}/{order_id} ----------
async def test_open_orders_lists_all_pages(client: AsyncClient, deps: ServerDeps):
    """验证未成交订单端点自动翻页并保持字段的十进制字符串契约。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供 101 条预置订单和查询记录的依赖

    返回：
        None，通过断言验证总数、字段映射和两次分页参数
    """
    seed = deps.gateway.orders[0]
    deps.gateway.orders = [seed.model_copy(update={"id": f"o-{index}"}) for index in range(101)]
    response = await client.get("/api/open_orders")
    assert response.status_code == 200
    assert len(response.json()) == 101
    first = response.json()[0]
    assert first["size"] == "-2"
    assert first["price"] == "59000"
    assert first["tif"] == "gtc" and first["reduce_only"] is True
    assert first["stop_loss_price"] == "58000"
    assert first["take_profit_price"] == "62000"
    assert deps.gateway.open_order_calls == [
        {"contract": None, "status": "open", "limit": 100, "offset": 0},
        {"contract": None, "status": "open", "limit": 100, "offset": 100},
    ]


async def test_cancel_open_order_success(client: AsyncClient, deps: ServerDeps):
    """验证人工撤单成功时返回空警告并记录合约和订单编号。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供撤单调用记录的服务器依赖

    返回：
        None，通过断言验证成功响应和调用目标
    """
    response = await client.delete(f"/api/orders/{BTC}/o-1")
    assert response.status_code == 200
    assert response.json()["warning"] == ""
    assert deps.cancel_calls == [(BTC, "o-1")]


async def test_cancel_open_order_keeps_gateway_success_when_local_sync_warns(
    client: AsyncClient, deps: ServerDeps
):
    """验证交易所撤单成功但本地同步告警时仍返回 200 并透传警告。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可替换撤单实现的服务器依赖

    返回：
        None，通过断言验证成功状态和防重试警告
    """

    async def warned(contract: str, order_id: str) -> dict:
        """返回带本地同步失败警告的撤单成功结果。

        参数：
            contract: str，订单所属合约
            order_id: str，待撤销订单编号

        返回：
            dict，表示交易所已取消但本地记录同步失败的结果
        """
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
    """验证未接入人工撤单回调时端点返回 503。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可移除撤单回调的服务器依赖

    返回：
        None，通过断言验证服务不可用状态码
    """
    deps.manual_cancel_order = None
    response = await client.delete(f"/api/orders/{BTC}/o-1")
    assert response.status_code == 503


async def test_cancel_open_order_502_on_gateway_error(client: AsyncClient, deps: ServerDeps):
    """验证人工撤单发生交易所错误时映射为 502。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可替换撤单实现的服务器依赖

    返回：
        None，通过断言验证网关失败状态码
    """

    async def failed(contract: str, order_id: str) -> dict:
        """模拟交易所查询不到待撤订单。

        参数：
            contract: str，订单所属合约，本桩不读取其内容
            order_id: str，待撤销订单编号，本桩不读取其内容

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            GatewayError: 每次调用均抛出订单不存在错误
        """
        raise GatewayError("order missing", label="ORDER_NOT_FOUND")

    deps.state["cancel_impl"] = failed
    response = await client.delete(f"/api/orders/{BTC}/o-1")
    assert response.status_code == 502


async def test_cancel_open_order_409_when_order_is_no_longer_open(
    client: AsyncClient, deps: ServerDeps
):
    """验证待撤订单已不再开放时端点返回冲突状态而非鼓励重试。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可替换撤单实现的服务器依赖

    返回：
        None，通过断言验证 409 状态和原始提示文本
    """

    async def already_closed(contract: str, order_id: str) -> dict:
        """模拟订单已不处于开放状态。

        参数：
            contract: str，订单所属合约，本桩不读取其内容
            order_id: str，待撤销订单编号，本桩不读取其内容

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            OrderNotFound: 每次调用均抛出订单不再开放错误
        """
        raise OrderNotFound("order is no longer open", label="ORDER_NOT_FOUND")

    deps.state["cancel_impl"] = already_closed
    response = await client.delete(f"/api/orders/{BTC}/o-1")

    assert response.status_code == 409
    assert response.json()["detail"] == "order is no longer open"


async def test_open_orders_maps_gateway_error_to_502(client: AsyncClient, deps: ServerDeps):
    """验证未成交订单查询发生交易所错误时映射为 502 并保留原因。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可替换网关订单查询的服务器依赖

    返回：
        None，通过断言验证状态码和错误详情
    """

    def fail_list_orders(*_args, **_kwargs):
        """模拟未成交订单查询时交易所不可用。

        参数：
            _args: tuple，位置参数，本桩不读取其内容
            _kwargs: dict，关键字参数，本桩不读取其内容

        返回：
            list[OrderResult]，本函数始终在返回前抛出异常

        异常：
            GatewayError: 每次调用均抛出交易所不可用错误
        """
        raise GatewayError("gateway unavailable")

    deps.gateway.list_orders = fail_list_orders
    response = await client.get("/api/open_orders")

    assert response.status_code == 502
    assert response.json()["detail"] == "gateway unavailable"


# ---------- POST /api/paper/reset ----------
async def test_paper_reset_success(client: AsyncClient, deps: ServerDeps):
    """验证 paper 账户重置会调用 Decimal 回调并同步运行时与配置文件。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供重置记录、运行时设置和配置路径的依赖

    返回：
        None，通过断言验证响应、回调参数及两处初始权益更新
    """
    r = await client.post("/api/paper/reset", json={"equity": 20000})
    assert r.status_code == 200
    assert r.json() == {"equity": 20000.0}
    assert deps.resets == [Decimal("20000")]  # paper_reset 以 Decimal 调用
    # runtime_settings 原地更新（同一实例，下轮决策即生效）
    assert deps.runtime_settings.paper.initial_equity == Decimal("20000")
    # config.yaml 已写回
    assert load_settings(deps.config_path).paper.initial_equity == Decimal("20000")


async def test_paper_reset_409_when_not_paper_mode(client: AsyncClient, deps: ServerDeps):
    """验证非 paper 模式拒绝模拟账户重置且不会调用重置回调。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可切换运行模式的服务器依赖

    返回：
        None，通过断言验证 409 状态和空重置记录
    """
    deps.runtime_settings = Settings(mode="testnet")
    r = await client.post("/api/paper/reset", json={"equity": 20000})
    assert r.status_code == 409
    assert deps.resets == []  # 未触发重置


async def test_paper_reset_409_when_reset_not_wired(client: AsyncClient, deps: ServerDeps):
    """验证未接入 paper 重置回调时端点返回 409。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可移除重置回调的服务器依赖

    返回：
        None，通过断言验证模式冲突状态码
    """
    deps.paper_reset = None
    r = await client.post("/api/paper/reset", json={"equity": 20000})
    assert r.status_code == 409


async def test_paper_reset_422_on_non_positive_equity(client: AsyncClient, deps: ServerDeps):
    """验证模拟账户重置拒绝零值和负数初始权益。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供重置调用记录的服务器依赖

    返回：
        None，通过断言验证两类非法输入均为 422 且未调用重置
    """
    for bad in (0, -5):
        r = await client.post("/api/paper/reset", json={"equity": bad})
        assert r.status_code == 422
    assert deps.resets == []


# ---------- POST /api/agent/start|stop ----------


async def test_agent_start_stop_reflects_real_state(client: AsyncClient, deps: ServerDeps):
    """验证 agent 启停端点返回状态提供器中的真实运行状态。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供可变运行状态和启停回调的依赖

    返回：
        None，通过断言验证启动与停止后的响应和内部状态
    """
    r = await client.post("/api/agent/start")
    assert r.json() == {"agent_running": True}  # 从 status_provider 读真实状态
    assert deps.state["running"] is True
    r = await client.post("/api/agent/stop")
    assert r.json() == {"agent_running": False}
    assert deps.state["running"] is False


async def test_agent_start_stop_503_when_not_wired(client: AsyncClient, deps: ServerDeps):
    """验证未接入 agent 启停回调时两个控制端点均返回 503。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可移除启停回调的服务器依赖

    返回：
        None，通过断言验证启动和停止端点的服务不可用状态
    """
    deps.agent_start = None
    deps.agent_stop = None
    assert (await client.post("/api/agent/start")).status_code == 503
    assert (await client.post("/api/agent/stop")).status_code == 503


# ---------- GET /api/candles ----------


async def test_candles_success(client: AsyncClient, deps: ServerDeps):
    """验证 K 线端点映射行情字段并把默认周期和数量透传给网关。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供固定 K 线及调用记录的假网关依赖

    返回：
        None，通过断言验证响应载荷和默认查询参数
    """
    r = await client.get("/api/candles", params={"contract": BTC})
    assert r.status_code == 200
    assert r.json()["items"] == [
        {"t": 3600, "o": 60000.5, "h": 60100.0, "l": 59900.0, "c": 60050.0, "v": 12.0}
    ]
    # 默认 interval=1h、limit=200 透传网关
    assert deps.gateway.candle_calls == [{"contract": BTC, "interval": "1h", "limit": 200}]


async def test_candles_interval_whitelist(client: AsyncClient):
    """验证 K 线端点允许全部约定周期并拒绝未登记周期。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证六个合法周期和一个非法周期
    """
    for ok in ("1m", "5m", "15m", "1h", "4h", "1d"):
        r = await client.get("/api/candles", params={"contract": BTC, "interval": ok})
        assert r.status_code == 200
    r = await client.get("/api/candles", params={"contract": BTC, "interval": "3h"})
    assert r.status_code == 422


async def test_candles_422_for_contract_outside_watchlist(client: AsyncClient):
    """验证 K 线查询拒绝不在运行白名单中的合约。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证白名单外合约返回 422
    """
    r = await client.get("/api/candles", params={"contract": "DOGE_USDT"})
    assert r.status_code == 422


async def test_candles_limit_validation(client: AsyncClient):
    """验证 K 线数量参数拒绝越界值并接受约定上限。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证零、上限外和恰好上限三个边界
    """
    for bad in (0, 1001):
        r = await client.get("/api/candles", params={"contract": BTC, "limit": bad})
        assert r.status_code == 422
    r = await client.get("/api/candles", params={"contract": BTC, "limit": 1000})
    assert r.status_code == 200


async def test_candles_503_when_gateway_missing(client: AsyncClient, deps: ServerDeps):
    """验证未接入交易所网关时 K 线端点返回 503。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可移除网关的服务器依赖

    返回：
        None，通过断言验证服务不可用状态码
    """
    deps.gateway = None
    r = await client.get("/api/candles", params={"contract": BTC})
    assert r.status_code == 503


async def test_candles_502_on_gateway_error(client: AsyncClient, deps: ServerDeps):
    """验证 K 线网关错误被映射为含交易所原因的 502 响应。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，可替换 K 线查询实现的服务器依赖

    返回：
        None，通过断言验证状态码和限流错误文本
    """

    def boom(*args, **kwargs):
        """模拟 K 线查询遭遇交易所限流。

        参数：
            args: tuple，位置参数，本桩不读取其内容
            kwargs: dict，关键字参数，本桩不读取其内容

        返回：
            list[Candle]，本函数始终在返回前抛出异常

        异常：
            GatewayError: 每次调用均抛出交易所限流错误
        """
        raise GatewayError("交易所限流")

    deps.gateway.get_candlesticks = boom
    r = await client.get("/api/candles", params={"contract": BTC})
    assert r.status_code == 502
    assert "交易所限流" in r.json()["detail"]


async def test_candles_watchlist_falls_back_to_file(client: AsyncClient, deps: ServerDeps):
    """验证未接入运行时白名单时 K 线端点回退读取白名单文件。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供白名单文件且可移除运行时列表的依赖

    返回：
        None，通过断言验证文件内合约成功、文件外合约被拒绝
    """
    deps.runtime_watchlist = None  # 未接线运行时名单：读 watchlist.yaml
    assert (await client.get("/api/candles", params={"contract": BTC})).status_code == 200
    assert (await client.get("/api/candles", params={"contract": "ETH_USDT"})).status_code == 422
