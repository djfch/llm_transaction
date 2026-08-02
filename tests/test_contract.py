"""前后端契约测试：冻结前端消费的全部 REST 端点响应键与类型（进常规 pytest 套件）。

契约表（与 web/src/api/types.ts 对齐，前端类型变更必须同步本表）：
- GET  /api/status → mode/uptime_seconds/kill_switch/agent_running/llm_provider/llm_model/llm_configured
- GET  /api/account → available/unrealised_pnl/equity（equity 必在，前端 AccountInfo 契约）
- GET  /api/positions → 元素含 contract/size/entry_price/mark_price/leverage/margin/unrealised_pnl/liq_price/stop_loss_price/take_profit_price
- GET  /api/portfolio → as_of/account/positions（同一时点的账户与持仓组合快照）
- GET  /api/trades → items[] 含 contract/size/price/fee/pnl/source/round_id；顶层 total/offset/limit
- GET  /api/equity → initial_equity/baseline_source/points[].t,equity
- GET  /api/notes → items[] 含 content/created_at/round_id；顶层 total/offset/limit
- GET  /api/alerts → 元素含 id/contract/direction/price/active/created_at（LLM 价格唤醒，内存唯一存储，触发即移除，active 恒真）
- GET  /api/daily_stats → realized_pnl/orders_today/max_orders_per_day（风控口径当日统计）
- GET  /api/rounds → items[] 含 round_id/strategy_md5 且 audit 摘要含 round_id/prompt_md5/started_at/ended_at/error；顶层 total/offset/limit
- GET  /api/rounds/{id} → round 字段展平到顶层（round_id/strategy_md5/prompt_snapshot/llm_raw 等）
       + tool_calls[] 含 seq/tool/args/risk_verdict/risk_reason/result/duration_ms（args/result 为已解析对象）
- GET  /api/agent/live → in_round/round（可为 null）/tool_calls[] 含 seq/tool/args/risk_verdict/risk_reason/result/duration_ms
- GET  /api/review/reports → items[] 含 id/period_start/period_end/stats_json/report_md(截断200字符)/strategy_action/new_version_id/error/created_at；顶层 total
- GET  /api/review/reports/{id} → 同列表项 9 键，report_md 为全文
- POST /api/review/run → started/ok（409 复盘进行中；503 LLM 未配置或未接线）
- GET  /api/strategy/versions → items[] 含 id/md5/created_by/reason/report_id/created_at（不含 content，省流量）
- GET  /api/strategy/versions/{id} → 同列表项键 + content 全文
- GET  /api/strategy/diff?from=&to= → PlainText unified diff（契约只断状态码）
- POST /api/strategy/rollback/{id} → rolled_back_to/version（404 版本不存在；503 未接线）
- GET  /api/candles → items[] 含 t/o/h/l/c/v
- GET  /api/config → mode 且含 llm/risk/scheduler 段；GET /api/watchlist → settle/contracts
- GET  /api/secrets/status → gate_key/llm_key/telegram 全布尔
- POST /api/kill_switch → kill_switch；POST /api/agent/start|stop → agent_running
- POST /api/paper/reset → equity；POST /api/positions/{contract}/close → contract/status/fill_price/text
- POST /api/secrets → saved/llm_configured/error；PUT /api/config → saved/needs_restart

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
from src.market.triggers import TriggerManager
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


async def _review_run() -> dict:
    return {
        "started": True,
        "ok": True,
        "report_id": 1,
        "round_id": "rv-round",
        "strategy_action": "none",
        "new_version_id": None,
    }


async def _strategy_rollback(version_id: int) -> dict:
    return {"rolled_back_to": version_id, "version": 3}


# 长复盘报告（>200 字符）：验证列表 report_md 截断、详情全文
_LONG_REPORT_MD = "# 复盘报告\n\n" + "区间成交 3 笔，胜率 66.7%，最大回撤可控。" * 20


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
    # 复盘种子：两个策略版本 + 一份关联 v2 的报告（版本↔报告互相关联的生产路径）
    await repo.review.save_strategy_version("策略书 v1：保守止损。", "md5-v1", "human", "初始版本")
    v2 = await repo.review.save_strategy_version(
        "策略书 v2：收紧止损。", "md5-v2", "review_agent", "复盘改写"
    )
    report = await repo.review.save_review_report(
        1000.0, 2000.0, '{"trades":3}', _LONG_REPORT_MD, "rewrite", new_version_id=v2.id
    )
    await repo.review.attach_report_to_version(v2.id, report.id)


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
    triggers = TriggerManager(lambda t, price: None)  # 内存预警线索引（唯一存储）
    triggers.add(BTC, ">=", D("62000"))  # 种子一条，保证 /api/alerts 非空
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
        review_run=_review_run,
        strategy_rollback=_strategy_rollback,
        alerts_provider=lambda: triggers.list(),
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
    portfolio = await _get(client, "/api/portfolio")
    _typed(portfolio, "as_of:n account:d positions:l", "/api/portfolio")
    _typed(portfolio["account"], "available:n unrealised_pnl:n equity:n", "/api/portfolio account")
    assert portfolio["positions"], "/api/portfolio positions 应非空"


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
    _typed(notes, "total:i offset:i limit:i", "/api/notes")
    _typed(notes["items"][0], "content:s created_at:n round_id:s", "/api/notes items[0]")
    alerts = await _get(client, "/api/alerts")
    assert alerts, "/api/alerts 应非空"
    _typed(alerts[0], "id:i contract:s direction:s price:n active:b created_at:n", "/api/alerts[0]")
    daily = await _get(client, "/api/daily_stats")
    _typed(daily, "realized_pnl:n orders_today:i max_orders_per_day:i", "/api/daily_stats")


async def test_rounds_contract(client: AsyncClient):
    body = await _get(client, "/api/rounds")
    assert body["items"], "/api/rounds items 应非空"
    _typed(body, "total:i offset:i limit:i", "/api/rounds")
    item = body["items"][0]
    _typed(item, "round_id:s strategy_md5:s", "/api/rounds items[0]")
    _typed(item["audit"], "round_id:s prompt_md5:s started_at:n ended_at:n error:s", "audit 摘要")
    detail = await _get(client, "/api/rounds/r1")
    # 契约：round 展平到顶层（前端 RoundDetail 读顶层 round_id/prompt_snapshot/llm_raw）
    assert detail["round_id"] == "r1"
    _typed(detail, "prompt_snapshot:s llm_raw:s strategy_md5:s", "/api/rounds/r1 展平字段")
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


_REPORT_SPEC = (
    "id:i period_start:n period_end:n stats_json:s report_md:s "
    "strategy_action:s new_version_id:i error:s created_at:n"
)


async def test_review_strategy_contract(client: AsyncClient):
    """复盘/策略版本端点契约：响应键与类型冻结（diff 为 PlainText，只断状态码）。"""
    body = await _get(client, "/api/review/reports?limit=10&offset=0")
    _typed(body, "total:i", "/api/review/reports")
    assert body["items"], "/api/review/reports items 应非空"
    item = body["items"][0]
    _typed(item, _REPORT_SPEC, "/api/review/reports items[0]")
    assert len(item["report_md"]) == 200  # 列表截断 200 字符（省流量，键名不变）
    detail = await _get(client, f"/api/review/reports/{item['id']}")
    _typed(detail, _REPORT_SPEC, "/api/review/reports/{id}")
    assert len(detail["report_md"]) > 200  # 详情给全文

    run = await _post(client, "/api/review/run")
    _typed(run, "started:b ok:b", "POST /api/review/run")

    versions = await _get(client, "/api/strategy/versions")
    assert versions["items"], "/api/strategy/versions items 应非空"
    v_item = versions["items"][0]
    _typed(
        v_item,
        "id:i md5:s created_by:s reason:s report_id:i created_at:n",
        "/api/strategy/versions items[0]",
    )
    assert "content" not in v_item  # 列表不含全文（省流量）
    v_detail = await _get(client, f"/api/strategy/versions/{v_item['id']}")
    _typed(
        v_detail,
        "id:i content:s md5:s created_by:s reason:s report_id:i created_at:n",
        "/api/strategy/versions/{id}",
    )

    # diff 为 PlainText：契约只断状态码（+ 泄漏扫描）
    r = await client.get(f"/api/strategy/diff?from={versions['items'][1]['id']}&to={v_item['id']}")
    assert r.status_code == 200, f"GET /api/strategy/diff → {r.status_code}"
    _no_leak(r.text, "GET /api/strategy/diff")

    rollback = await _post(client, "/api/strategy/rollback/1")
    _typed(rollback, "rolled_back_to:i version:i", "POST /api/strategy/rollback/{id}")
