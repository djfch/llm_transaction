"""监控后端 API 测试：httpx ASGITransport + fake 依赖注入（tmp_path 隔离真实配置）。

覆盖：全部只读端点、配置编辑（含 422 非法配置）、404 未知 round、
secrets 响应不含明文、kill_switch 写回 config.yaml、WS 握手与广播、CORS。
"""

import asyncio
import time
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from fastapi.testclient import TestClient

from src.audit.trail import AuditTrail
from src.config import AuditConfig
from src.config_io import write_settings
from src.gateway.base import Account, Position
from src.memory.db import Database
from src.memory.repo import Repo
from src.server.app import create_app
from src.server.deps import ServerDeps
from src.server.ws import ConnectionManager, pump_events


class FakeGateway:
    """注入用假网关：返回固定账户与持仓。"""

    def __init__(self) -> None:
        self.position_calls = 0

    def get_account(self) -> Account:
        return Account(available=Decimal("9000"), unrealised_pnl=Decimal("100"))

    def list_positions(self) -> list[Position]:
        self.position_calls += 1
        return [
            Position(
                contract="BTC_USDT",
                size=Decimal(1),
                entry_price=Decimal("50000"),
                mark_price=Decimal("51000"),
                liq_price=Decimal("40000"),
                leverage=Decimal(5),
                margin=Decimal("1000"),
                unrealised_pnl=Decimal("100"),
            )
        ]


@pytest.fixture
async def deps(tmp_path: Path):
    """组装 fake 依赖：tmp 配置文件 + 内存种子数据（一轮决策/审计、两笔成交、一条笔记）。"""
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)  # 默认配置（Decimal 安全写回）
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(
        yaml.safe_dump({"settle": "usdt", "contracts": ["BTC_USDT"]}), encoding="utf-8"
    )
    prompt_path = tmp_path / "system_prompt.md"
    prompt_path.write_text("原始提示词", encoding="utf-8")

    db = Database()
    await db.open(tmp_path / "test.db")
    repo = Repo(db)
    audit = AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit")))
    await repo.save_decision(round_id="r1", mode="paper", wake_source="timer")
    await repo.start_audit_round("r1", "paper", wake_source="timer", prompt_md5="md5")
    await repo.save_audit_tool_call("r1", seq=0, tool="place_order", risk_verdict="allow")
    await repo.finish_audit_round("r1", llm_raw="LLM 原文")
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
    await repo.save_trade(
        "r1",
        "paper",
        "ETH_USDT",
        Decimal(-1),
        Decimal("3000"),
        Decimal("1"),
        Decimal("-50"),
        created_at=2000.0,
    )
    await repo.add_note("r1", "第一条笔记")

    kill_calls: list[bool] = []
    d = ServerDeps(
        repo=repo,
        audit_trail=audit,
        gateway=FakeGateway(),
        status_provider=lambda: {"uptime_seconds": 12},
        on_kill_switch=kill_calls.append,
        config_path=config_path,
        watchlist_path=watchlist_path,
        prompt_path=prompt_path,
        web_dist=tmp_path / "no_dist",
    )
    d.kill_calls = kill_calls  # 测试断言用
    yield d
    await db.close()


@pytest.fixture
async def client(deps: ServerDeps):
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------- 只读端点 ----------


async def test_status(client: AsyncClient):
    r = await client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "mode": "paper",
        "uptime_seconds": 12,
        "kill_switch": False,
        "agent_running": False,  # status_provider 未提供时缺省 False
        "llm_provider": "anthropic",
        "llm_model": "claude-sonnet-4-5",
        "llm_configured": False,  # status_provider 未提供时缺省 False
    }


async def test_account_and_positions(client: AsyncClient):
    account = (await client.get("/api/account")).json()
    assert float(account["available"]) == 9000.0
    # 前端 AccountInfo 契约：equity 必在（缺了会让前端 fmtNum(undefined) 整页崩溃）
    assert "equity" in account and float(account["equity"]) > 0
    positions = (await client.get("/api/positions")).json()
    assert positions[0]["contract"] == "BTC_USDT"


async def test_portfolio_returns_one_authoritative_snapshot(client: AsyncClient, deps: ServerDeps):
    body = (await client.get("/api/portfolio")).json()

    assert isinstance(body["as_of"], float)
    assert float(body["account"]["equity"]) == 10100.0
    assert body["positions"][0]["contract"] == "BTC_USDT"
    assert deps.gateway.position_calls == 1


async def test_account_503_when_gateway_missing(deps: ServerDeps, client: AsyncClient):
    deps.gateway = None
    for path in ("/api/account", "/api/positions", "/api/portfolio"):
        r = await client.get(path)
        assert r.status_code == 503


async def test_rounds_list_and_pagination(client: AsyncClient):
    r = await client.get("/api/rounds", params={"offset": 0, "limit": 10})
    assert r.status_code == 200
    body = r.json()
    items = body["items"]
    assert len(items) == 1 and items[0]["round_id"] == "r1"
    assert body["total"] == 1 and body["offset"] == 0 and body["limit"] == 10
    assert "llm_raw" not in items[0]  # 列表不含 LLM 原文
    assert items[0]["audit"]["prompt_md5"] == "md5"
    beyond = (await client.get("/api/rounds", params={"offset": 1})).json()
    assert beyond["items"] == [] and beyond["total"] == 1
    assert (await client.get("/api/rounds", params={"offset": -1})).status_code == 422
    assert (await client.get("/api/rounds", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/rounds", params={"limit": 201})).status_code == 422


async def test_round_detail_and_404(client: AsyncClient):
    r = await client.get("/api/rounds/r1")
    assert r.status_code == 200
    body = r.json()
    # 契约形态：round 展平到顶层（前端 RoundDetail 逐字对齐），tool_calls 解析为对象
    assert body["llm_raw"] == "LLM 原文"
    assert body["round_id"] == "r1"
    assert body["tool_calls"][0]["tool"] == "place_order"
    assert isinstance(body["tool_calls"][0]["args"], dict)
    assert (await client.get("/api/rounds/unknown")).status_code == 404


async def test_trades_filter(client: AsyncClient):
    body = (await client.get("/api/trades")).json()
    assert len(body["items"]) == 2
    assert body["total"] == 2 and body["offset"] == 0 and body["limit"] == 50
    assert body["items"][0]["contract"] == "ETH_USDT"  # 最新在前
    filtered = (await client.get("/api/trades", params={"contract": "BTC_USDT"})).json()
    assert [t["contract"] for t in filtered["items"]] == ["BTC_USDT"]
    assert filtered["total"] == 1  # total 同样按合约过滤


async def test_trades_pagination(client: AsyncClient):
    r = await client.get("/api/trades", params={"limit": 1, "offset": 1})
    body = r.json()
    assert [t["contract"] for t in body["items"]] == ["BTC_USDT"]  # 第 2 页只余较旧那笔
    assert body["total"] == 2 and body["offset"] == 1 and body["limit"] == 1
    # 非法分页参数：offset<0 / limit 越界 → 422
    assert (await client.get("/api/trades", params={"offset": -1})).status_code == 422
    assert (await client.get("/api/trades", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/trades", params={"limit": 201})).status_code == 422


async def test_equity_series(client: AsyncClient):
    body = (await client.get("/api/equity")).json()
    assert body["initial_equity"] == 10000.0
    equities = [p["equity"] for p in body["points"]]
    assert equities == [10099.0, 10048.0]  # 10000+100-1, 再 -50-1


async def test_notes(client: AsyncClient, deps: ServerDeps):
    for index in range(2, 6):
        await deps.repo.add_note("r1", f"第{index}条笔记")
    first = (await client.get("/api/notes", params={"offset": 0, "limit": 2})).json()
    assert [item["content"] for item in first["items"]] == ["第5条笔记", "第4条笔记"]
    assert first["total"] == 5 and first["offset"] == 0 and first["limit"] == 2
    beyond = (await client.get("/api/notes", params={"offset": 5, "limit": 2})).json()
    assert beyond["items"] == [] and beyond["total"] == 5
    assert (await client.get("/api/notes", params={"offset": -1})).status_code == 422
    assert (await client.get("/api/notes", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/notes", params={"limit": 201})).status_code == 422


async def test_daily_stats_endpoint(client: AsyncClient, deps: ServerDeps):
    """当日统计端点：与风控同一口径（服务器时区自然日、按 mode 过滤、仅开仓单计数）。

    fixture 自带的两笔成交 created_at=1000/2000（1970 年，非当日）不应计入；
    本用例追加当日成交与订单后断言三键取值。
    """
    now = time.time()
    # 当日 paper 成交两笔：realized = 10 + (-3) = 7；昨日一笔 99 不计入
    await deps.repo.save_trade(
        "r1",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("0.5"),
        Decimal("10"),
        "llm_close",
        now,
    )
    await deps.repo.save_trade(
        "r1",
        "paper",
        "BTC_USDT",
        Decimal(-1),
        Decimal("50000"),
        Decimal("0.5"),
        Decimal("-3"),
        "llm_close",
        now,
    )
    await deps.repo.save_trade(
        "r1",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("0.5"),
        Decimal("99"),
        "llm_close",
        now - 90000,
    )
    # 订单：paper 开仓单 1（计入）；paper 平仓单（is_close 排除）；testnet 开仓单（mode 排除）
    await deps.repo.save_order("o1", "r1", "paper", "BTC_USDT", Decimal(1))
    await deps.repo.save_order("o2", "r1", "paper", "BTC_USDT", Decimal(0), is_close=True)
    await deps.repo.save_order("o3", "r1", "testnet", "BTC_USDT", Decimal(1))

    r = await client.get("/api/daily_stats")
    assert r.status_code == 200
    assert r.json() == {"realized_pnl": 7.0, "orders_today": 1, "max_orders_per_day": 20}


# ---------- 配置编辑端点 ----------


async def test_config_get_and_put(client: AsyncClient, deps: ServerDeps):
    raw = (await client.get("/api/config")).json()
    assert raw["risk"]["max_leverage"] == 5
    raw["llm"]["model"] = "claude-opus-4"
    r = await client.put("/api/config", json=raw)
    # 本 fixture 未接线 runtime_settings：llm.model 无法原地生效，诚实标 needs_restart
    # （接线后热重建生效的契约见 test_server_secrets.py）
    assert r.json() == {"saved": True, "needs_restart": ["llm.model"]}
    assert (await client.get("/api/config")).json()["llm"]["model"] == "claude-opus-4"


async def test_config_put_needs_restart(client: AsyncClient):
    raw = (await client.get("/api/config")).json()
    raw["mode"] = "testnet"
    r = await client.put("/api/config", json=raw)
    assert r.json()["needs_restart"] == ["mode"]


async def test_config_put_invalid_422(client: AsyncClient):
    raw = (await client.get("/api/config")).json()
    raw["risk"]["max_leverage"] = 0  # 违反 ge=1
    r = await client.put("/api/config", json=raw)
    assert r.status_code == 422
    raw["risk"]["max_leverage"] = 5
    raw["mode"] = "mars"  # 非法 mode
    assert (await client.put("/api/config", json=raw)).status_code == 422


async def test_strategy_get_and_put(client: AsyncClient):
    assert (await client.get("/api/strategy")).text == "原始提示词"
    r = await client.put("/api/strategy", content="新提示词")
    assert r.status_code == 200
    assert (await client.get("/api/strategy")).text == "新提示词"


async def test_watchlist_get_put_and_422(client: AsyncClient):
    assert (await client.get("/api/watchlist")).json()["contracts"] == ["BTC_USDT"]
    r = await client.put(
        "/api/watchlist", json={"settle": "usdt", "contracts": ["BTC_USDT", "ETH_USDT"]}
    )
    assert r.json() == {"saved": True}
    assert (await client.get("/api/watchlist")).json()["contracts"][1] == "ETH_USDT"
    bad = await client.put("/api/watchlist", json={"settle": "usdt", "contracts": []})
    assert bad.status_code == 422  # 空白名单非法


async def test_secrets_status_never_leaks_plaintext(client: AsyncClient, monkeypatch):
    for name in (
        "GATE_API_KEY",
        "GATE_API_SECRET",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    r = await client.get("/api/secrets/status")
    assert r.json() == {"gate_key": False, "llm_key": False, "telegram": False}

    monkeypatch.setenv("GATE_API_KEY", "明文-gate-key-xyz")
    monkeypatch.setenv("GATE_API_SECRET", "明文-gate-secret-xyz")
    monkeypatch.setenv("OPENAI_API_KEY", "明文-openai-key-xyz")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "明文-tg-token-xyz")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "明文-tg-chat-xyz")
    r = await client.get("/api/secrets/status")
    assert r.json() == {"gate_key": True, "llm_key": True, "telegram": True}
    assert "明文" not in r.text and "xyz" not in r.text  # 响应不含任何明文


async def test_kill_switch_writes_config_and_callback(client: AsyncClient, deps: ServerDeps):
    r = await client.post("/api/kill_switch", json={"enabled": True})
    assert r.json() == {"kill_switch": True}
    assert deps.kill_calls == [True]  # 回调被调用
    saved = yaml.safe_load(deps.config_path.read_text(encoding="utf-8"))
    assert saved["risk"]["kill_switch"] is True  # 写回 config.yaml
    assert (await client.get("/api/status")).json()["kill_switch"] is True


# ---------- CORS / 静态托管 / WebSocket ----------


async def test_cors_allows_vite_dev_server(client: AsyncClient):
    r = await client.get("/api/status", headers={"Origin": "http://localhost:17576"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:17576"


async def test_static_mount_when_dist_exists(deps: ServerDeps, tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    deps.web_dist = dist
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/")
        assert r.status_code == 200 and "ok" in r.text


def test_ws_hello_on_connect(deps: ServerDeps):
    app = create_app(deps)
    with TestClient(app) as tc, tc.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "hello"}


class _FakeWS:
    """ConnectionManager 单元测试用假连接。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def test_connection_manager_broadcast():
    manager = ConnectionManager()
    a, b = _FakeWS(), _FakeWS()
    await manager.connect(a)
    await manager.connect(b)
    assert a.sent == [{"type": "hello"}]  # 连接建立先回握手消息
    await manager.broadcast({"type": "round", "round_id": "r1"})
    assert a.sent[-1] == b.sent[-1] == {"type": "round", "round_id": "r1"}
    manager.disconnect(a)
    await manager.broadcast({"type": "trade"})
    assert len(a.sent) == 2 and b.sent[-1] == {"type": "trade"}


async def test_pump_events_broadcasts_queue():
    queue: asyncio.Queue = asyncio.Queue()
    manager = ConnectionManager()
    ws = _FakeWS()
    await manager.connect(ws)
    task = asyncio.create_task(pump_events(manager, queue))
    await queue.put({"type": "position"})
    await asyncio.sleep(0.05)
    task.cancel()
    assert ws.sent[-1] == {"type": "position"}
