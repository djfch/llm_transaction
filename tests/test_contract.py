"""前后端契约测试：冻结前端消费的全部 REST 端点响应键与类型（进常规 pytest 套件）。

契约表（与 web/src/api/types.ts 对齐，前端类型变更必须同步本表）：
- GET  /api/status → mode/uptime_seconds/kill_switch/agent_running/llm_provider/llm_model/llm_configured
- GET  /api/account → available/unrealised_pnl/equity（equity 必在，前端 AccountInfo 契约）
- GET  /api/positions → 元素含 contract/size/entry_price/mark_price/leverage/margin/unrealised_pnl/liq_price
- GET  /api/trades → items[] 含 contract/size/price/fee/pnl/source/round_id；顶层 total/offset/limit
- GET  /api/equity → initial_equity/baseline_source/points[].t,equity
- GET  /api/notes → items[] 含 content/created_at/round_id
- GET  /api/daily_stats → realized_pnl/orders_today/max_orders_per_day（风控口径当日统计）
- GET  /api/rounds → items[] 含 round_id 且 audit 摘要含 round_id/prompt_md5/started_at/ended_at/error
- GET  /api/rounds/{id} → round 字段展平到顶层（round_id/prompt_snapshot/llm_raw 等）
       + tool_calls[] 含 seq/tool/args/risk_verdict/risk_reason/result/duration_ms（args/result 为已解析对象）
- GET  /api/agent/live → in_round/round（可为 null）/tool_calls[] 含 seq/tool/args/risk_verdict/risk_reason/result/duration_ms
- GET  /api/candles → items[] 含 t/o/h/l/c/v
- GET  /api/config → mode 且含 llm/risk/scheduler 段；GET /api/watchlist → settle/contracts
- GET  /api/secrets/status → gate_key/llm_key/telegram 全布尔
- POST /api/kill_switch → kill_switch；POST /api/agent/start|stop → agent_running
- POST /api/paper/reset → equity；POST /api/positions/{contract}/close → contract/status/fill_price/text
- POST /api/secrets → saved/llm_configured/error；PUT /api/config → saved/needs_restart

历史注：/api/rounds/{id} 曾是 {round: 嵌套, tool_calls: 未解析 dump} 形态（前端读顶层会崩页），
已修复为展平 + 解析形态，本表即冻结契约。

安全护栏：全部响应原始文本（JSON 即全量递归）不得含 "GATE_API_SECRET" 及注入的假 key 值。
"""

from decimal import Decimal as D
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.audit.trail import AuditTrail
from src.config import AuditConfig, Settings
from src.config_io import write_settings
from src.gateway.base import Account, Candle, Position
from src.memory.db import Database
from src.memory.repo import Repo
from src.server.app import create_app
from src.server.deps import ServerDeps

BTC = "BTC_USDT"
# 注入用假 LLM key（哨兵值）：任何响应都绝不允许回显它们
FAKE_ANTHROPIC_KEY = "fake-anthropic-key-sentinel-1a2b3c"
FAKE_OPENAI_KEY = "fake-openai-key-sentinel-4d5e6f"
_FORBIDDEN = ("GATE_API_SECRET", FAKE_ANTHROPIC_KEY, FAKE_OPENAI_KEY)
_CLOSE_RESULT = {"contract": BTC, "status": "finished", "fill_price": 60100.5, "text": "已平仓"}


class FakeGateway:
    """注入用假网关：固定账户/持仓/K 线，保证列表端点非空（键断言才有意义）。"""

    def get_account(self) -> Account:
        return Account(available=D("9000"), unrealised_pnl=D("100"))

    def list_positions(self) -> list[Position]:
        # fmt: skip 保持紧凑表格写法（字段多，炸开一人一行反而难读）
        return [  # fmt: skip
            Position(
                contract=BTC,
                size=D(1),
                entry_price=D("50000"),
                mark_price=D("51000"),
                liq_price=D("40000"),
                leverage=D(5),
                margin=D("1000"),
                unrealised_pnl=D("100"),
            )
        ]

    def get_candlesticks(self, contract: str, interval: str = "1m", limit: int | None = None):
        return [Candle(t=3600, o=D("60000.5"), h=D("60100"), l=D("59900"), c=D("60050"), v=D("12"))]


async def _close(contract: str) -> dict:
    return _CLOSE_RESULT


async def _reconfigure() -> dict:
    return {"llm_configured": True, "error": ""}


async def _seed(repo: Repo) -> None:
    """种子数据：决策/审计/工具调用/成交/笔记各一条，保证列表端点非空（键断言才有意义）。"""
    await repo.save_decision(round_id="r1", mode="paper", wake_source="timer")
    await repo.start_audit_round("r1", "paper", wake_source="timer", prompt_md5="md5")
    await repo.save_audit_tool_call("r1", 1, "place_order", '{"size": 1}', "allow")
    await repo.finish_audit_round("r1", llm_raw="LLM 原文")
    await repo.save_trade(  # fmt: skip
        "r1", "paper", BTC, D(1), D("60000"), D("1"), D("100"), "llm_open", 1000.0
    )
    await repo.add_note("r1", "第一条笔记")


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch):
    """隔离真实 LLM key 环境变量（POST /api/secrets 会写 os.environ，monkeypatch 自动复原）。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture
async def deps(tmp_path: Path):
    """组装 fake 依赖：tmp 配置/名单/.env + 内存 DB 种子数据 + 各写操作假回调。"""
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)  # 默认 paper 配置
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text("settle: usdt\ncontracts:\n- BTC_USDT\n", encoding="utf-8")
    db = Database()
    await db.open(tmp_path / "t.db")
    repo = Repo(db)
    await _seed(repo)
    state = {"running": False}

    async def _set_running(value: bool) -> None:
        state["running"] = value

    d = ServerDeps(
        repo=repo,
        audit_trail=AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit"))),
        gateway=FakeGateway(),
        status_provider=lambda: {"uptime_seconds": 5, "agent_running": state["running"]},
        runtime_settings=Settings(),  # 默认 mode=paper（paper reset 需要）
        runtime_watchlist=[BTC],
        config_path=config_path,
        watchlist_path=watchlist_path,
        env_path=tmp_path / ".env",
        web_dist=tmp_path / "no_dist",
        manual_close=lambda c: _close(c),
        paper_reset=lambda equity: None,
        agent_start=lambda: _set_running(True),
        agent_stop=lambda: _set_running(False),
        llm_reconfigure=_reconfigure,
    )
    yield d
    await db.close()


@pytest.fixture
async def client(deps: ServerDeps):
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------- 断言助手 ----------

_KINDS = {"s": str, "b": bool, "i": int, "l": list, "d": dict}


def _is_num(value: object) -> bool:
    """number 或可解析的数字字符串（Decimal 金额序列化为字符串，前端 Number() 适配）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(value)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False


def _typed(obj: dict, spec: str, where: str) -> None:
    """按紧凑契约表断言键存在且类型对：s=str b=bool i=int l=list d=dict n=number。"""
    for pair in spec.split():
        key, code = pair.rsplit(":", 1)
        assert key in obj, f"{where} 缺少契约键: {key}"
        value = obj[key]
        ok = _is_num(value) if code == "n" else isinstance(value, _KINDS[code])
        assert ok, f"{where}.{key} 类型错误({code}): {value!r}"


def _no_leak(text: str, where: str) -> None:
    """安全护栏：响应原始文本（JSON 全量递归）不得含密钥字样与注入的假 key 值。"""
    for token in _FORBIDDEN:
        assert token not in text, f"{where} 响应泄漏敏感串: {token}"


async def _get(client: AsyncClient, path: str) -> dict:
    """GET → 200 + 泄漏扫描，返回响应 json。"""
    r = await client.get(path)
    assert r.status_code == 200, f"GET {path} → {r.status_code}"
    _no_leak(r.text, f"GET {path}")
    return r.json()


async def _post(client: AsyncClient, path: str, body: dict | None = None) -> dict:
    """POST → 200 + 泄漏扫描，返回响应 json。"""
    r = await client.post(path, json=body)
    assert r.status_code == 200, f"POST {path} → {r.status_code}: {r.text}"
    _no_leak(r.text, f"POST {path}")
    return r.json()


async def test_status_account_positions_contract(client: AsyncClient):
    body = await _get(client, "/api/status")
    _typed(
        body,
        "mode:s uptime_seconds:n kill_switch:b agent_running:b "
        "llm_provider:s llm_model:s llm_configured:b",
        "/api/status",
    )
    body = await _get(client, "/api/account")
    _typed(body, "available:n unrealised_pnl:n equity:n", "/api/account")
    items = await _get(client, "/api/positions")
    assert items, "/api/positions 应非空"
    _typed(
        items[0],
        "contract:s size:n entry_price:n mark_price:n leverage:n margin:n unrealised_pnl:n liq_price:n",
        "/api/positions[0]",
    )


async def test_trades_equity_notes_contract(client: AsyncClient):
    body = await _get(client, "/api/trades")
    _typed(body, "total:i offset:i limit:i", "/api/trades")
    assert body["items"], "/api/trades items 应非空"
    _typed(
        body["items"][0],
        "contract:s size:n price:n fee:n pnl:n source:s round_id:s",
        "/api/trades items[0]",
    )
    body = await _get(client, "/api/equity")
    _typed(body, "initial_equity:n baseline_source:s", "/api/equity")
    assert body["points"], "/api/equity points 应非空"
    _typed(body["points"][0], "t:n equity:n", "/api/equity points[0]")
    notes = await _get(client, "/api/notes")
    assert notes["items"], "/api/notes items 应非空"
    _typed(notes["items"][0], "content:s created_at:n round_id:s", "/api/notes items[0]")
    daily = await _get(client, "/api/daily_stats")
    _typed(daily, "realized_pnl:n orders_today:i max_orders_per_day:i", "/api/daily_stats")


async def test_rounds_contract(client: AsyncClient):
    body = await _get(client, "/api/rounds")
    assert body["items"], "/api/rounds items 应非空"
    item = body["items"][0]
    _typed(item, "round_id:s", "/api/rounds items[0]")
    _typed(item["audit"], "round_id:s prompt_md5:s started_at:n ended_at:n error:s", "audit 摘要")
    detail = await _get(client, "/api/rounds/r1")
    # 契约：round 展平到顶层（前端 RoundDetail 读顶层 round_id/prompt_snapshot/llm_raw）
    assert detail["round_id"] == "r1"
    _typed(detail, "prompt_snapshot:s llm_raw:s", "/api/rounds/r1 展平字段")
    assert detail["tool_calls"], "/api/rounds/r1 tool_calls 应非空"
    _typed(
        detail["tool_calls"][0],
        "seq:i tool:s risk_verdict:s risk_reason:s duration_ms:i",
        "/api/rounds/r1 tool_calls[0]",
    )
    # args/result 为已解析对象（dict；契约允许字符串兜底，但本 fixture 种子是 JSON 对象）
    assert isinstance(detail["tool_calls"][0]["args"], dict)
    assert isinstance(detail["tool_calls"][0]["result"], dict)
    live = await _get(client, "/api/agent/live")
    _typed(live, "in_round:b tool_calls:l", "/api/agent/live")
    round_ = live["round"]
    assert round_ is None or round_["round_id"] == "r1"  # 契约允许 null；本 fixture 已种子一轮
    assert live["tool_calls"], "/api/agent/live tool_calls 应非空"
    _typed(
        live["tool_calls"][0],
        "seq:i tool:s args:d result:d risk_verdict:s risk_reason:s duration_ms:i",
        "/api/agent/live tool_calls[0]",
    )


async def test_candles_config_watchlist_secrets_contract(client: AsyncClient):
    body = await _get(client, f"/api/candles?contract={BTC}")
    assert body["items"], "/api/candles items 应非空"
    _typed(body["items"][0], "t:n o:n h:n l:n c:n v:n", "/api/candles items[0]")
    config = await _get(client, "/api/config")
    assert isinstance(config["mode"], str)
    for section in ("llm", "risk", "scheduler"):
        assert isinstance(config.get(section), dict), f"/api/config 缺 {section} 段"
    watchlist = await _get(client, "/api/watchlist")
    _typed(watchlist, "settle:s contracts:l", "/api/watchlist")
    status = await _get(client, "/api/secrets/status")
    _typed(status, "gate_key:b llm_key:b telegram:b", "/api/secrets/status")


async def test_write_ops_contract(client: AsyncClient):
    """kill_switch、agent 启停、paper reset、手动平仓、PUT config（响应均过泄漏扫描）。"""
    body = await _post(client, "/api/kill_switch", {"enabled": True})
    _typed(body, "kill_switch:b", "POST /api/kill_switch")
    assert (await _post(client, "/api/agent/start"))["agent_running"] is True
    assert (await _post(client, "/api/agent/stop"))["agent_running"] is False
    body = await _post(client, "/api/paper/reset", {"equity": 12345})
    _typed(body, "equity:n", "POST /api/paper/reset")
    body = await _post(client, f"/api/positions/{BTC}/close")
    _typed(body, "contract:s status:s fill_price:n text:s", "POST /api/positions/{contract}/close")
    raw = await _get(client, "/api/config")
    r = await client.put("/api/config", json=raw)
    assert r.status_code == 200
    _no_leak(r.text, "PUT /api/config")
    _typed(r.json(), "saved:b needs_restart:l", "PUT /api/config")


async def test_post_secrets_contract_and_no_echo(client: AsyncClient, deps: ServerDeps):
    """POST /api/secrets：契约键齐 + 假 key 落盘后任何后续响应不回显明文。"""
    body = await _post(
        client,
        "/api/secrets",
        {"anthropic_api_key": FAKE_ANTHROPIC_KEY, "openai_api_key": FAKE_OPENAI_KEY},
    )
    _typed(body, "saved:b llm_configured:b error:s", "POST /api/secrets")
    assert FAKE_ANTHROPIC_KEY in deps.env_path.read_text(encoding="utf-8")  # 确已注入
    for path in ("/api/secrets/status", "/api/status", "/api/config"):
        await _get(client, path)  # _get 内已扫描：注入后的假 key 绝不出现在任何响应
