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
from src.config import AuditConfig, Settings
from src.config_io import write_settings
from src.gateway.base import Account, Position
from src.market.triggers import TriggerManager
from src.memory.db import Database
from src.memory.repo import Repo
from src.server.app import create_app
from src.server.deps import ServerDeps
from src.server.ws import ConnectionManager, pump_events


class FakeGateway:
    """注入用假网关：返回固定账户与持仓。"""

    def __init__(self) -> None:
        """初始化假网关，归零持仓查询计数器。

        参数：无

        返回：
            None，就地把 position_calls 置 0，供用例断言持仓查询次数
        """
        self.position_calls = 0

    def get_account(self) -> Account:
        """返回固定账户数据，模拟网关账户查询。

        参数：无

        返回：
            Account：固定账户（available=9000、unrealised_pnl=100）
        """
        return Account(available=Decimal("9000"), unrealised_pnl=Decimal("100"))

    def list_positions(self) -> list[Position]:
        """返回固定的 BTC_USDT 多单，并累计查询次数。

        参数：无

        返回：
            list[Position]：固定的一条 BTC_USDT 多头持仓；
            副作用是 position_calls 加一，供单次快照断言
        """
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

    def list_tpsl_orders(self, contract: str) -> list:
        """返回空保护单列表（网关协议要求的展示补全读取）。

        参数：
            contract: str，合约名
        返回：
            list：空列表（本假网关不模拟止盈止损保护单）
        """
        return []


@pytest.fixture
async def deps(tmp_path: Path):
    """组装 fake 依赖：tmp 配置文件 + 内存种子数据（一轮决策/审计、两笔成交、一条笔记）。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        AsyncIterator[ServerDeps]，通过夹具向测试提供上述临时依赖，并在结束后清理资源
    """
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
    triggers = TriggerManager(lambda t, p: None)
    d = ServerDeps(
        repo=repo,
        audit_trail=audit,
        gateway=FakeGateway(),
        status_provider=lambda: {"uptime_seconds": 12},
        on_kill_switch=kill_calls.append,
        alerts_provider=lambda: triggers.list(),
        config_path=config_path,
        watchlist_path=watchlist_path,
        prompt_path=prompt_path,
        web_dist=tmp_path / "no_dist",
    )
    d.kill_calls = kill_calls  # 测试断言用
    d.triggers = triggers  # 价格唤醒用例直接读写内存索引（内存唯一存储）
    yield d
    await db.close()


@pytest.fixture
async def client(deps: ServerDeps):
    """基于 fake 依赖组装应用并给出 ASGI 测试客户端。

    参数：
        deps: ServerDeps，fake 依赖夹具，注入 create_app 组装被测应用

    返回：
        AsyncIterator[AsyncClient]，yield 可直连各 API 的客户端，退出时关闭客户端上下文
    """
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------- 只读端点 ----------


async def test_status(client: AsyncClient):
    """校验 /api/status 返回运行状态快照及未接线字段的缺省值。

    参数：
        client: AsyncClient，ASGI 测试客户端夹具

    返回：
        None，断言 200 且 mode/uptime/kill_switch/llm 字段与种子配置一致，
        status_provider 未提供的 agent_running 与 llm_configured 缺省为 False
    """
    r = await client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "mode": "paper",
        "uptime_seconds": 12,
        "kill_switch": False,
        "agent_running": False,  # status_provider 未提供时缺省 False
        "llm_credential_name": "default",
        "llm_provider": "anthropic",
        "llm_model": "claude-sonnet-4-5",
        "llm_thinking_effort": "",
        "llm_configured": False,  # status_provider 未提供时缺省 False
    }


async def test_status_legacy_flat_llm_resolves_default_credential(
    client: AsyncClient, deps: ServerDeps
):
    """旧平铺 LLM 配置仍解析为 default 凭证并保留非空思考强度。

    参数：
        client: AsyncClient，ASGI 测试客户端夹具
        deps: ServerDeps，提供隔离配置文件路径的服务器依赖

    返回：
        None，通过状态接口断言旧配置无需迁移即可返回完整决策凭证摘要
    """
    write_settings(
        {
            "llm": {
                "provider": "openai_compat",
                "model": "deepseek-v4-pro",
                "openai_base_url": "https://api.deepseek.example/v1",
                "thinking_effort": "high",
            }
        },
        deps.config_path,
    )

    status = (await client.get("/api/status")).json()
    assert status["llm_credential_name"] == "default"
    assert status["llm_provider"] == "openai_compat"
    assert status["llm_model"] == "deepseek-v4-pro"
    assert status["llm_thinking_effort"] == "high"


async def test_status_prefers_runtime_trader_credential_when_file_is_stale_or_invalid(
    client: AsyncClient, deps: ServerDeps
):
    """状态接口始终以共享运行配置中的决策凭证为准，不受旧文件阻断。

    参数：
        client: AsyncClient，ASGI 测试客户端夹具
        deps: ServerDeps，提供共享运行配置和隔离配置文件路径的服务依赖

    返回：
        None，先断言运行中 Pro 凭证覆盖文件默认值，再断言损坏文件不影响运行状态
    """
    deps.runtime_settings = Settings.model_validate(
        {
            "llm": {
                "credentials": [
                    {
                        "name": "default",
                        "provider": "openai_compat",
                        "model": "deepseek-v4-pro",
                        "thinking_effort": "high",
                    }
                ]
            }
        }
    )

    status = (await client.get("/api/status")).json()
    assert status["llm_model"] == "deepseek-v4-pro"
    assert status["llm_thinking_effort"] == "high"

    deps.config_path.write_text("mode: [invalid", encoding="utf-8")
    status = (await client.get("/api/status")).json()
    assert status["llm_model"] == "deepseek-v4-pro"
    assert status["llm_thinking_effort"] == "high"


async def test_account_and_positions(client: AsyncClient):
    """校验账户与持仓端点：equity 必在（前端渲染契约），持仓透传假网关种子仓位。

    参数：
        client: AsyncClient，ASGI 测试客户端夹具

    返回：
        None，断言账户 available=9000、equity 存在且为正，
        持仓首条合约为 BTC_USDT
    """
    account = (await client.get("/api/account")).json()
    assert float(account["available"]) == 9000.0
    # 前端 AccountInfo 契约：equity 必在（缺了会让前端 fmtNum(undefined) 整页崩溃）
    assert "equity" in account and float(account["equity"]) > 0
    positions = (await client.get("/api/positions")).json()
    assert positions[0]["contract"] == "BTC_USDT"


async def test_portfolio_returns_one_authoritative_snapshot(client: AsyncClient, deps: ServerDeps):
    """验证组合快照只查询并返回一份权威账户与持仓数据。

    参数：
        client: AsyncClient，用于发起测试请求的客户端
        deps: ServerDeps，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    body = (await client.get("/api/portfolio")).json()

    assert isinstance(body["as_of"], float)
    assert float(body["account"]["equity"]) == 10100.0
    assert body["positions"][0]["contract"] == "BTC_USDT"
    assert deps.gateway.position_calls == 1


async def test_account_503_when_gateway_missing(deps: ServerDeps, client: AsyncClient):
    """验证网关未接线时账户接口明确返回 503。

    参数：
        deps: ServerDeps，测试应用或工具的依赖集合
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
    deps.gateway = None
    for path in ("/api/account", "/api/positions", "/api/portfolio"):
        r = await client.get(path)
        assert r.status_code == 503


async def test_rounds_list_and_pagination(client: AsyncClient):
    """验证决策轮次列表的排序与分页结果。

    参数：
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """验证决策轮次详情及不存在轮次的 404 响应。

    参数：
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """验证成交列表能够按合约筛选。

    参数：
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
    body = (await client.get("/api/trades")).json()
    assert len(body["items"]) == 2
    assert body["total"] == 2 and body["offset"] == 0 and body["limit"] == 50
    assert body["items"][0]["contract"] == "ETH_USDT"  # 最新在前
    filtered = (await client.get("/api/trades", params={"contract": "BTC_USDT"})).json()
    assert [t["contract"] for t in filtered["items"]] == ["BTC_USDT"]
    assert filtered["total"] == 1  # total 同样按合约过滤


async def test_trades_pagination(client: AsyncClient):
    """验证成交列表的分页边界与总数。

    参数：
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
    r = await client.get("/api/trades", params={"limit": 1, "offset": 1})
    body = r.json()
    assert [t["contract"] for t in body["items"]] == ["BTC_USDT"]  # 第 2 页只余较旧那笔
    assert body["total"] == 2 and body["offset"] == 1 and body["limit"] == 1
    # 非法分页参数：offset<0 / limit 越界 → 422
    assert (await client.get("/api/trades", params={"offset": -1})).status_code == 422
    assert (await client.get("/api/trades", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/trades", params={"limit": 201})).status_code == 422


async def test_equity_series(client: AsyncClient):
    """验证权益曲线接口返回按时间排列的数据点。

    参数：
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
    body = (await client.get("/api/equity")).json()
    assert body["initial_equity"] == 10000.0
    equities = [p["equity"] for p in body["points"]]
    assert equities == [10099.0, 10048.0]  # 10000+100-1, 再 -50-1


async def test_notes(client: AsyncClient, deps: ServerDeps):
    """验证笔记列表与新增笔记接口。

    参数：
        client: AsyncClient，用于发起测试请求的客户端
        deps: ServerDeps，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
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


async def test_alerts_lists_pending_from_memory(client: AsyncClient, deps: ServerDeps):
    """价格唤醒端点：返回内存索引中的未触发预警线，字段契约供前端面板渲染（内存唯一存储）。

    参数：
        client: AsyncClient，用于发起测试请求的客户端
        deps: ServerDeps，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    first = deps.triggers.add("BTC_USDT", ">=", Decimal("52000"))
    drop = deps.triggers.add("ETH_USDT", "<=", Decimal("2800"))
    second = deps.triggers.add("ETH_USDT", "<=", Decimal("3000"))
    deps.triggers.remove(drop.id)  # 触发/取消即从索引移除，端点不再返回

    r = await client.get("/api/alerts")
    assert r.status_code == 200
    items = r.json()
    assert [item["id"] for item in items] == [first.id, second.id]  # 按 id 升序
    item = items[0]
    assert item["contract"] == "BTC_USDT"
    assert item["direction"] == "above"
    assert item["price"] == "52000"  # 锁形态：Decimal 以字符串返回（原 pydantic 序列化口径）
    assert item["active"] is True  # 内存索引只存未触发条目，恒真以保持响应形态
    assert isinstance(item["created_at"], (int, float))


async def test_alerts_503_when_not_wired(client: AsyncClient, deps: ServerDeps):
    """alerts_provider 未接线（agent 未就绪）时诚实 503，不返回空列表冒充无预警。

    参数：
        client: AsyncClient，用于发起测试请求的客户端
        deps: ServerDeps，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    deps.alerts_provider = None
    r = await client.get("/api/alerts")
    assert r.status_code == 503


async def test_daily_stats_endpoint(client: AsyncClient, deps: ServerDeps):
    """当日统计端点：与风控同一口径（服务器时区自然日、按 mode 过滤、仅开仓单计数）。

    参数：
        client: AsyncClient，用于发起测试请求的客户端
        deps: ServerDeps，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
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
    """验证配置读取与更新能够往返保持字段值。

    参数：
        client: AsyncClient，用于发起测试请求的客户端
        deps: ServerDeps，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    raw = (await client.get("/api/config")).json()
    assert raw["risk"]["max_leverage"] == 5
    raw["llm"]["model"] = "claude-opus-4"
    r = await client.put("/api/config", json=raw)
    # 本 fixture 未接线 runtime_settings：llm.model 无法原地生效，诚实标 needs_restart
    # （接线后热重建生效的契约见 test_server_secrets.py）
    assert r.json() == {"saved": True, "needs_restart": ["llm.model"]}
    assert (await client.get("/api/config")).json()["llm"]["model"] == "claude-opus-4"


async def test_config_put_needs_restart(client: AsyncClient):
    """验证需重启配置变更会返回明确标记。

    参数：
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
    raw = (await client.get("/api/config")).json()
    raw["mode"] = "testnet"
    r = await client.put("/api/config", json=raw)
    assert r.json()["needs_restart"] == ["mode"]


async def test_config_put_invalid_422(client: AsyncClient):
    """验证非法配置更新返回 422 校验错误。

    参数：
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
    raw = (await client.get("/api/config")).json()
    raw["risk"]["max_leverage"] = 0  # 违反 ge=1
    r = await client.put("/api/config", json=raw)
    assert r.status_code == 422
    raw["risk"]["max_leverage"] = 5
    raw["mode"] = "mars"  # 非法 mode
    assert (await client.put("/api/config", json=raw)).status_code == 422


async def test_strategy_get_and_put(client: AsyncClient):
    """验证策略正文的读取与更新。

    参数：
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
    assert (await client.get("/api/strategy")).text == "原始提示词"
    r = await client.put("/api/strategy", content="新提示词")
    assert r.status_code == 200
    assert (await client.get("/api/strategy")).text == "新提示词"


async def test_watchlist_get_put_and_422(client: AsyncClient):
    """验证关注列表读写及非法输入的 422 响应。

    参数：
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
    assert (await client.get("/api/watchlist")).json()["contracts"] == ["BTC_USDT"]
    r = await client.put(
        "/api/watchlist", json={"settle": "usdt", "contracts": ["BTC_USDT", "ETH_USDT"]}
    )
    assert r.json() == {"saved": True}
    assert (await client.get("/api/watchlist")).json()["contracts"][1] == "ETH_USDT"
    bad = await client.put("/api/watchlist", json={"settle": "usdt", "contracts": []})
    assert bad.status_code == 422  # 空白名单非法


async def test_secrets_status_never_leaks_plaintext(client: AsyncClient, monkeypatch):
    """验证密钥状态接口绝不回显明文凭证。

    参数：
        client: AsyncClient，用于发起测试请求的客户端
        monkeypatch: pytest.MonkeyPatch，用于隔离并替换依赖或环境变量的 pytest 夹具

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    # 旧三键契约不变；credentials 为新增的多凭证状态数组（default 为旧平铺合成）
    assert {k: v for k, v in r.json().items() if k != "credentials"} == {
        "gate_key": False,
        "llm_key": False,
        "telegram": False,
    }
    creds = r.json()["credentials"]
    assert len(creds) == 1 and creds[0]["name"] == "default"
    assert creds[0]["key_configured"] is False

    monkeypatch.setenv("GATE_API_KEY", "明文-gate-key-xyz")
    monkeypatch.setenv("GATE_API_SECRET", "明文-gate-secret-xyz")
    # 默认凭证为 anthropic：llm_key 按生效凭证的 api_key_env 读取 ANTHROPIC_API_KEY；
    # OPENAI_API_KEY 一并设置，额外验证不出现在响应里
    monkeypatch.setenv("ANTHROPIC_API_KEY", "明文-ant-key-xyz")
    monkeypatch.setenv("OPENAI_API_KEY", "明文-openai-key-xyz")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "明文-tg-token-xyz")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "明文-tg-chat-xyz")
    r = await client.get("/api/secrets/status")
    assert {k: v for k, v in r.json().items() if k != "credentials"} == {
        "gate_key": True,
        "llm_key": True,
        "telegram": True,
    }
    assert "明文" not in r.text and "xyz" not in r.text  # 响应不含任何明文


async def test_kill_switch_writes_config_and_callback(client: AsyncClient, deps: ServerDeps):
    """验证熔断开关同时写回配置并触发回调。

    参数：
        client: AsyncClient，用于发起测试请求的客户端
        deps: ServerDeps，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    r = await client.post("/api/kill_switch", json={"enabled": True})
    assert r.json() == {"kill_switch": True}
    assert deps.kill_calls == [True]  # 回调被调用
    saved = yaml.safe_load(deps.config_path.read_text(encoding="utf-8"))
    assert saved["risk"]["kill_switch"] is True  # 写回 config.yaml
    assert (await client.get("/api/status")).json()["kill_switch"] is True


# ---------- CORS / 静态托管 / WebSocket ----------


async def test_cors_allows_vite_dev_server(client: AsyncClient):
    """验证 CORS 允许 Vite 开发服务器访问。

    参数：
        client: AsyncClient，用于发起测试请求的客户端

    返回：
        None，通过断言验证上述行为，无返回值
    """
    r = await client.get("/api/status", headers={"Origin": "http://localhost:17576"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:17576"


async def test_static_mount_when_dist_exists(deps: ServerDeps, tmp_path: Path):
    """验证前端构建产物存在时挂载静态页面。

    参数：
        deps: ServerDeps，测试应用或工具的依赖集合
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    deps.web_dist = dist
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/")
        assert r.status_code == 200 and "ok" in r.text


def test_ws_hello_on_connect(deps: ServerDeps):
    """验证 WebSocket 建连后立即收到握手消息。

    参数：
        deps: ServerDeps，测试应用或工具的依赖集合

    返回：
        None，通过断言验证上述行为，无返回值
    """
    app = create_app(deps)
    with TestClient(app) as tc, tc.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "hello"}


class _FakeWS:
    """ConnectionManager 单元测试用假连接。"""

    def __init__(self) -> None:
        """初始化测试替身并保存后续调用所需的预设数据。

        参数：无

        返回：
            None，初始化当前测试替身，无返回值
        """
        self.sent: list[dict] = []

    async def accept(self) -> None:
        """记录模拟 WebSocket 已接受连接。

        参数：无

        返回：
            None，执行上述模拟操作或副作用，无返回值
        """
        pass

    async def send_json(self, payload: dict) -> None:
        """保存发送给模拟 WebSocket 的 JSON 消息。

        参数：
            payload: dict，待发送的事件或 JSON 数据

        返回：
            None，执行上述模拟操作或副作用，无返回值
        """
        self.sent.append(payload)


async def test_connection_manager_broadcast():
    """验证连接管理器向所有活跃连接广播消息。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
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
    """验证事件泵把队列消息转发给连接管理器。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    queue: asyncio.Queue = asyncio.Queue()
    manager = ConnectionManager()
    ws = _FakeWS()
    await manager.connect(ws)
    task = asyncio.create_task(pump_events(manager, queue))
    await queue.put({"type": "position"})
    await asyncio.sleep(0.05)
    task.cancel()
    assert ws.sent[-1] == {"type": "position"}
