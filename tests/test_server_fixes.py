"""已确认缺陷修复的回归测试（与 test_server_api.py 并列，控制单文件行数）。

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
        return Account(available=Decimal("9000"), unrealised_pnl=Decimal("100"))

    def list_positions(self) -> list[Position]:
        return []


@pytest.fixture
async def deps(tmp_path: Path):
    """fake 依赖：tmp 配置（默认 paper）+ 一笔 paper 成交；watchlist 文件同步就绪。"""
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
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------- /api/status：kill_switch 取运行时内存值 ----------


async def test_status_kill_switch_prefers_runtime(deps: ServerDeps, client: AsyncClient):
    """agent 连续失败触发的内存态风控锁必须对监控可见（文件里仍是 false）。"""
    deps.status_provider = lambda: {"uptime_seconds": 1, "kill_switch": True}
    assert (await client.get("/api/status")).json()["kill_switch"] is True


async def test_status_kill_switch_falls_back_to_file(deps: ServerDeps, client: AsyncClient):
    """运行时状态未提供 kill_switch 时回退到配置文件值。"""
    deps.status_provider = lambda: {"uptime_seconds": 1}
    assert (await client.get("/api/status")).json()["kill_switch"] is False


# ---------- PUT /api/config：运行时原地写回 ----------


async def test_put_config_updates_shared_runtime_risk(deps: ServerDeps, client: AsyncClient):
    """改风控参数后与 agent 循环共享的 Settings 实例原地更新（下轮决策即生效）。"""
    runtime = Settings()  # 模拟 build_app 持有的共享实例
    deps.runtime_settings = runtime
    raw = (await client.get("/api/config")).json()
    raw["risk"]["max_position_pct"] = 0.5
    r = await client.put("/api/config", json=raw)
    assert r.json() == {"saved": True, "needs_restart": []}
    assert runtime.risk.max_position_pct == 0.5  # 共享对象已原地更新


async def test_put_config_updates_shared_runtime_scheduler(deps: ServerDeps, client: AsyncClient):
    """调度参数同样原地写回（WakeupScheduler 每次唤醒时读配置字段）。"""
    runtime = Settings()
    deps.runtime_settings = runtime
    raw = (await client.get("/api/config")).json()
    raw["scheduler"]["default_wake_minutes"] = 30
    r = await client.put("/api/config", json=raw)
    assert r.json()["needs_restart"] == []
    assert runtime.scheduler.default_wake_minutes == 30


async def test_put_config_restart_fields_not_written_back(deps: ServerDeps, client: AsyncClient):
    """mode 等构造期绑定字段写不回运行时，须标 needs_restart（llm 热键已移出，见 secrets 测试）。"""
    runtime = Settings()
    deps.runtime_settings = runtime
    raw = (await client.get("/api/config")).json()
    raw["mode"] = "testnet"
    r = await client.put("/api/config", json=raw)
    assert r.json()["needs_restart"] == ["mode"]
    assert runtime.mode == "paper"  # 运行时不被改写


async def test_put_config_without_runtime_marks_restart(deps: ServerDeps, client: AsyncClient):
    """未接线运行时配置时诚实标注 needs_restart，不假称下轮生效。"""
    raw = (await client.get("/api/config")).json()
    raw["risk"]["max_position_pct"] = 0.5
    r = await client.put("/api/config", json=raw)
    assert r.json()["needs_restart"] == ["risk.max_position_pct"]


async def test_put_config_invalid_scheduler_422(client: AsyncClient):
    """min > max 的调度配置被模型校验拒绝（ConfigError → 422）。"""
    raw = (await client.get("/api/config")).json()
    raw["scheduler"]["min_wake_minutes"] = 600
    raw["scheduler"]["max_wake_minutes"] = 60
    assert (await client.put("/api/config", json=raw)).status_code == 422


# ---------- PUT /api/watchlist：运行名单原地更新 ----------


async def test_put_watchlist_updates_shared_runtime_list(deps: ServerDeps, client: AsyncClient):
    """watchlist 写回后共享名单同一 list 对象原地更新（ToolDeps.watchlist 下轮可见）。"""
    runtime_list = ["BTC_USDT"]
    deps.runtime_watchlist = runtime_list
    r = await client.put(
        "/api/watchlist", json={"settle": "usdt", "contracts": ["BTC_USDT", "ETH_USDT"]}
    )
    assert r.json() == {"saved": True}
    assert runtime_list == ["BTC_USDT", "ETH_USDT"]


# ---------- /api/equity：基准按模式选取、分位舍入 ----------


async def test_equity_baseline_paper_uses_config(client: AsyncClient):
    """paper 模式基准取 paper.initial_equity（响应为 number，前端图表可直接用）。"""
    body = (await client.get("/api/equity")).json()
    assert body["baseline_source"] == "paper_config"
    assert body["initial_equity"] == 10000.0
    assert isinstance(body["points"][0]["equity"], float)


async def test_equity_points_rounded_to_cents(client: AsyncClient):
    """曲线点保留 number 但四舍五入到分位，避免浮点长尾（如 10099.000000000002）。"""
    body = (await client.get("/api/equity")).json()
    for p in body["points"]:
        assert p["equity"] == round(p["equity"], 2)


async def test_equity_baseline_testnet_uses_account(deps: ServerDeps, client: AsyncClient):
    """非 paper 模式基准由账户当前权益倒推（曲线末端回到当前权益），不读 paper 配置。"""
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
    """账户查询不可用时基准降级为 0 并在响应标注，不 5xx。"""
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
        self.sent: list[dict] = []
        self._exc = exc

    async def accept(self) -> None:
        pass

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        raise self._exc


async def test_ws_connection_cleans_up_on_unexpected_error():
    """接收侧抛非 WebSocketDisconnect 异常时，连接也必须从 manager 移除（不残留）。"""
    manager = ConnectionManager()
    ws = _FakeWS(RuntimeError("连接异常"))
    with pytest.raises(RuntimeError):
        await ws_connection(ws, manager)
    assert manager.count == 0


async def test_ws_connection_cleans_up_on_disconnect():
    """正常断开同样清理（回归保护：finally 不破坏既有行为）。"""
    manager = ConnectionManager()
    ws = _FakeWS(WebSocketDisconnect())
    await ws_connection(ws, manager)
    assert manager.count == 0


async def test_put_config_merges_preserves_untouched_sections(
    deps: ServerDeps, client: AsyncClient
):
    """PUT 只提交字段子集时，未提及的段/键必须原样保留（回归：整体写回曾把
    gate/paper/server 等段静默重置为默认值）。"""
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
