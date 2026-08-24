"""服务端配置、WebSocket 与状态接口测试（与 test_server_api.py 并列控制体量）。

覆盖：PUT /api/config 运行时原地写回与 needs_restart 诚实标注、
/api/status 的 kill_switch 取运行时内存值、/api/equity 基准按模式选取与分位舍入、
WS 异常时连接清理、非法调度配置 422。
"""

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from fastapi import WebSocketDisconnect
from httpx import ASGITransport, AsyncClient

from src.config import Settings
from src.config_io import write_settings
from src.gateway.base import Account, Position
from src.memory.db import Database
from src.memory.repo import Repo
from src.server.app import create_app
from src.server.deps import ServerDeps
from src.server.ws import ConnectionManager, ws_connection


class FakeGateway:
    """注入用假网关：账户权益 9100（available 9000 + unrealised 100），无持仓。"""

    def get_account(self) -> Account:
        """返回固定的假账户快照（可用 9000 + 未实现盈亏 100）。

        参数：无

        返回：
            Account：权益合计 9100 的假账户对象
        """
        return Account(available=Decimal("9000"), unrealised_pnl=Decimal("100"))

    def list_positions(self) -> list[Position]:
        """返回空持仓列表，模拟当前无任何持仓。

        参数：无

        返回：
            list[Position]：空列表，表示无持仓
        """
        return []


@pytest.fixture
async def deps(tmp_path: Path):
    """构造含默认 paper 配置、白名单与一笔成交的临时服务器依赖。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        AsyncIterator[ServerDeps]，生成测试依赖并在用例结束后关闭数据库
    """
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(
        yaml.safe_dump({"settle": "usdt", "contracts": ["BTC_USDT"]}), encoding="utf-8"
    )
    db = Database()
    await db.open(tmp_path / "test.db")
    repo = Repo(db)
    await repo.save_trade(
        "r1",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("100"),
        created_at=1000.0,
    )
    d = ServerDeps(
        repo=repo,
        gateway=FakeGateway(),
        config_path=config_path,
        watchlist_path=watchlist_path,
        prompt_path=tmp_path / "system_prompt.md",
        web_dist=tmp_path / "no_dist",
    )
    yield d
    await db.close()


@pytest.fixture
async def client(deps: ServerDeps):
    """构造直连假依赖所装配应用的 httpx 异步测试客户端。

    参数：
        deps: ServerDeps，fake 依赖夹具，create_app 以其装配 FastAPI 应用

    返回：
        AsyncIterator[AsyncClient]，yield 走 ASGI 内存传输的客户端，退出时关闭客户端上下文
    """
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------- /api/status：kill_switch 取运行时内存值 ----------


async def test_status_kill_switch_prefers_runtime(deps: ServerDeps, client: AsyncClient):
    """验证状态接口优先展示运行时内存中的风控锁而非旧文件值。

    参数：
        deps: ServerDeps，可修改运行时状态提供器的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证 kill_switch(风控锁)为运行时真值
    """
    deps.status_provider = lambda: {"uptime_seconds": 1, "kill_switch": True}
    assert (await client.get("/api/status")).json()["kill_switch"] is True


async def test_status_kill_switch_falls_back_to_file(deps: ServerDeps, client: AsyncClient):
    """验证运行时未提供风控锁字段时状态接口回退到配置文件值。

    参数：
        deps: ServerDeps，可修改运行时状态提供器的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证回退后的风控锁为文件中的假值
    """
    deps.status_provider = lambda: {"uptime_seconds": 1}
    assert (await client.get("/api/status")).json()["kill_switch"] is False


# ---------- PUT /api/config：运行时原地写回 ----------


async def test_put_config_updates_shared_runtime_risk(deps: ServerDeps, client: AsyncClient):
    """验证保存风控参数会原地更新决策循环共享的运行时配置。

    参数：
        deps: ServerDeps，可接入共享 Settings 的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证无需重启且共享风控参数即时更新
    """
    runtime = Settings()  # 模拟 build_app 持有的共享实例
    deps.runtime_settings = runtime
    raw = (await client.get("/api/config")).json()
    raw["risk"]["max_position_pct"] = 0.5
    raw["risk"]["max_position_stop_risk_pct"] = 0.02
    r = await client.put("/api/config", json=raw)
    assert r.json() == {"saved": True, "needs_restart": []}
    assert runtime.risk.max_position_pct == 0.5  # 共享对象已原地更新
    assert runtime.risk.max_position_stop_risk_pct == 0.02


async def test_put_config_updates_shared_runtime_scheduler(deps: ServerDeps, client: AsyncClient):
    """验证保存调度参数会原地更新共享配置并在后续唤醒时生效。

    参数：
        deps: ServerDeps，可接入共享 Settings 的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证无需重启且默认唤醒分钟数已更新
    """
    runtime = Settings()
    deps.runtime_settings = runtime
    raw = (await client.get("/api/config")).json()
    raw["scheduler"]["default_wake_minutes"] = 30
    r = await client.put("/api/config", json=raw)
    assert r.json()["needs_restart"] == []
    assert runtime.scheduler.default_wake_minutes == 30


async def test_put_config_hot_applies_research_patch(deps: ServerDeps, client: AsyncClient):
    """研报总开关和调度列表以局部补丁保存并原地热写回共享配置。

    参数：
        deps: ServerDeps，可接入共享 Settings 的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None：断言无需重启且未提交配置段保持不变
    """
    runtime = Settings()
    deps.runtime_settings = runtime
    schedules = [item.model_dump(mode="json") for item in runtime.research.schedules]
    schedules[0]["enabled"] = False
    response = await client.put(
        "/api/config", json={"research": {"enabled": True, "schedules": schedules}}
    )
    assert response.json() == {"saved": True, "needs_restart": []}
    assert runtime.research.enabled is True
    assert runtime.research.schedules[0].enabled is False
    assert runtime.risk.max_leverage == 5


async def test_put_config_restart_fields_not_written_back(deps: ServerDeps, client: AsyncClient):
    """验证运行模式等构造期字段只标记需重启而不会改写当前运行时对象。

    参数：
        deps: ServerDeps，可接入共享 Settings 的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证 needs_restart(需重启字段)与当前模式值
    """
    runtime = Settings()
    deps.runtime_settings = runtime
    raw = (await client.get("/api/config")).json()
    raw["mode"] = "testnet"
    r = await client.put("/api/config", json=raw)
    assert r.json()["needs_restart"] == ["mode"]
    assert runtime.mode == "paper"  # 运行时不被改写


async def test_put_config_without_runtime_marks_restart(deps: ServerDeps, client: AsyncClient):
    """验证未接入运行时配置时把已保存字段诚实标记为需要重启。

    参数：
        deps: ServerDeps，未接入 runtime_settings 的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证风控字段出现在需重启列表
    """
    raw = (await client.get("/api/config")).json()
    raw["risk"]["max_position_pct"] = 0.5
    r = await client.put("/api/config", json=raw)
    assert r.json()["needs_restart"] == ["risk.max_position_pct"]


async def test_put_config_invalid_scheduler_422(client: AsyncClient):
    """验证最小唤醒时间大于最大值的调度配置被映射为 422。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证非法调度边界的响应状态码
    """
    raw = (await client.get("/api/config")).json()
    raw["scheduler"]["min_wake_minutes"] = 600
    raw["scheduler"]["max_wake_minutes"] = 60
    assert (await client.put("/api/config", json=raw)).status_code == 422


# ---------- PUT /api/watchlist：运行名单原地更新 ----------


async def test_put_watchlist_updates_shared_runtime_list(deps: ServerDeps, client: AsyncClient):
    """验证保存白名单会原地更新工具依赖持有的共享列表对象。

    参数：
        deps: ServerDeps，可接入共享白名单列表的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证保存成功且原列表已包含新增合约
    """
    runtime_list = ["BTC_USDT"]
    deps.runtime_watchlist = runtime_list
    r = await client.put(
        "/api/watchlist", json={"settle": "usdt", "contracts": ["BTC_USDT", "ETH_USDT"]}
    )
    assert r.json() == {"saved": True}
    assert runtime_list == ["BTC_USDT", "ETH_USDT"]


# ---------- /api/equity：基准按模式选取、分位舍入 ----------


async def test_equity_baseline_paper_uses_config(client: AsyncClient):
    """验证 paper 模式权益曲线以模拟账户初始权益配置作为基准。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证基准来源、初始权益与数值类型
    """
    body = (await client.get("/api/equity")).json()
    assert body["baseline_source"] == "paper_config"
    assert body["initial_equity"] == 10000.0
    assert isinstance(body["points"][0]["equity"], float)


async def test_equity_points_rounded_to_cents(client: AsyncClient):
    """验证权益曲线点保持数值类型并统一四舍五入到分位。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证每个权益点均无分位后的浮点长尾
    """
    body = (await client.get("/api/equity")).json()
    for p in body["points"]:
        assert p["equity"] == round(p["equity"], 2)


async def test_equity_baseline_testnet_uses_account(deps: ServerDeps, client: AsyncClient):
    """验证 testnet 模式从当前账户权益倒推曲线基准而不读取 paper 配置。

    参数：
        deps: ServerDeps，提供配置、仓储与假网关的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证基准来源、倒推起点与曲线终点
    """
    raw = yaml.safe_load(deps.config_path.read_text(encoding="utf-8"))
    raw["mode"] = "testnet"
    raw["paper"]["initial_equity"] = 555.0  # 若误读配置基准会暴露
    write_settings(raw, deps.config_path)
    await deps.repo.save_trade(
        "r2",
        "testnet",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("100"),
        created_at=2000.0,
    )
    body = (await client.get("/api/equity")).json()
    assert body["baseline_source"] == "account"
    # 当前权益 9100；曲线只含 testnet 成交（pnl 100 - fee 1），倒推起点 9001
    assert body["initial_equity"] == 9001.0
    assert body["points"][-1]["equity"] == 9100.0


async def test_equity_baseline_fallback_when_gateway_missing(deps: ServerDeps, client: AsyncClient):
    """验证非 paper 模式缺少网关时权益基准降级为零而不返回服务端错误。

    参数：
        deps: ServerDeps，可移除网关的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证成功状态、降级来源与零初始权益
    """
    raw = yaml.safe_load(deps.config_path.read_text(encoding="utf-8"))
    raw["mode"] = "testnet"
    write_settings(raw, deps.config_path)
    deps.gateway = None
    r = await client.get("/api/equity")
    assert r.status_code == 200
    body = r.json()
    assert body["baseline_source"] == "fallback_zero"
    assert body["initial_equity"] == 0.0


# ---------- WebSocket：异常时连接清理 ----------


class _FakeWS:
    """receive 行为可配的假连接。"""

    def __init__(self, exc: Exception) -> None:
        """初始化假连接，记录 receive_text 被调用时应抛出的异常。

        参数：
            exc: Exception，receive_text 被调用时要抛出的异常

        返回：
            None，就地初始化实例状态（已发送消息列表与待抛异常）
        """
        self.sent: list[dict] = []
        self._exc = exc

    async def accept(self) -> None:
        """接受连接（空实现，仅为满足 ws_connection 的调用）。

        参数：无

        返回：
            None，无任何副作用
        """
        pass

    async def send_json(self, payload: dict) -> None:
        """记录服务端下发的 JSON 消息，供测试断言检查。

        参数：
            payload: dict，服务端要发送的 JSON 消息体

        返回：
            None，消息追加到 sent 列表
        """
        self.sent.append(payload)

    async def receive_text(self) -> str:
        """模拟接收侧失败，调用即抛出初始化时注入的异常。

        参数：无

        返回：
            str：类型标注为 str，实际调用即抛异常、不会返回

        异常：
            Exception：构造时注入的异常（如 WebSocketDisconnect 或 RuntimeError）
        """
        raise self._exc


async def test_ws_connection_cleans_up_on_unexpected_error():
    """验证 WebSocket 接收侧意外异常传播后连接仍从管理器中清理。

    参数：无

    返回：
        None，通过断言验证 RuntimeError 传播且连接计数归零
    """
    manager = ConnectionManager()
    ws = _FakeWS(RuntimeError("连接异常"))
    with pytest.raises(RuntimeError):
        await ws_connection(ws, manager)
    assert manager.count == 0


async def test_ws_connection_cleans_up_on_disconnect():
    """验证正常 WebSocket 断开同样会从连接管理器中清理。

    参数：无

    返回：
        None，通过断言验证断开后连接计数归零
    """
    manager = ConnectionManager()
    ws = _FakeWS(WebSocketDisconnect())
    await ws_connection(ws, manager)
    assert manager.count == 0


async def test_put_config_merges_preserves_untouched_sections(
    deps: ServerDeps, client: AsyncClient
):
    """验证配置局部更新只覆盖提交字段并保留其他段与同段其他键。

    参数：
        deps: ServerDeps，提供配置文件路径的服务器依赖
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证目标键更新且三个未提交值原样保留
    """
    raw = yaml.safe_load(deps.config_path.read_text(encoding="utf-8"))
    raw["paper"]["initial_equity"] = 23456
    raw["gate"]["testnet_host"] = "https://custom.example.com"
    deps.config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    r = await client.put("/api/config", json={"risk": {"max_position_pct": 0.2}})
    assert r.status_code == 200

    saved = yaml.safe_load(deps.config_path.read_text(encoding="utf-8"))
    assert float(saved["paper"]["initial_equity"]) == 23456.0  # 未提及段保留
    assert saved["gate"]["testnet_host"] == "https://custom.example.com"
    assert float(saved["risk"]["max_position_pct"]) == 0.2  # 提交的键已更新
    assert int(saved["risk"]["max_leverage"]) == 5  # 同段未提及键保留
