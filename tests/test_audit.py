"""审计追踪测试：tmp_path SQLite + 临时快照目录，验证一轮决策完整可回放。"""

import hashlib
import json

import pytest

from src.audit.trail import AuditTrail
from src.config import AuditConfig
from src.memory import Database, Repo

MODE = "paper"
WAKE = "timer"
SYSTEM_PROMPT = "你是交易 Agent，只在白名单内交易。"
CONTEXT = "账户权益 10000；持仓：无；BTC_USDT 最新价 50000。"
LLM_RAW = '{"tool_calls": [{"tool": "place_order"}, {"tool": "get_account"}]}'


@pytest.fixture
async def repo(tmp_path):
    db = Database()
    await db.open(tmp_path / "audit.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
def trail(repo: Repo, tmp_path) -> AuditTrail:
    return AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit")))


async def _run_full_round(trail: AuditTrail) -> str:
    """模拟完整一轮：begin → 2 次工具调用（放行+拒绝）→ end。"""
    round_id = await trail.begin_round(MODE, WAKE, SYSTEM_PROMPT, CONTEXT)
    await trail.record_tool_call(
        round_id,
        seq=0,
        tool="place_order",
        args={"contract": "BTC_USDT", "size": 1, "price": "50000"},
        risk_verdict="allow",
        risk_reason="",
        result={"order_id": "t-abc", "status": "open"},
        duration_ms=120,
    )
    await trail.record_tool_call(
        round_id,
        seq=1,
        tool="place_order",
        args={"contract": "DOGE_USDT", "size": 100},
        risk_verdict="deny",
        risk_reason="非白名单合约",
        result={"skipped": True},
        duration_ms=3,
    )
    await trail.end_round(round_id, LLM_RAW)
    return round_id


# ---------- begin / end 主表 ----------


async def test_begin_round_writes_main_row(trail: AuditTrail, repo: Repo):
    round_id = await trail.begin_round(MODE, WAKE, SYSTEM_PROMPT, CONTEXT)
    assert round_id  # uuid 非空
    row = await repo.get_audit_round(round_id)
    assert row is not None
    assert row.mode == MODE
    assert row.wake_source == WAKE
    assert row.prompt_md5 == hashlib.md5(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert row.prompt_snapshot == SYSTEM_PROMPT
    assert row.context_snapshot == CONTEXT
    assert row.ended_at is None  # 未结束


async def test_end_round_fills_llm_raw(trail: AuditTrail, repo: Repo):
    round_id = await _run_full_round(trail)
    row = await repo.get_audit_round(round_id)
    assert row.llm_raw == LLM_RAW
    assert row.ended_at is not None
    assert row.error == ""


async def test_end_round_with_error(trail: AuditTrail, repo: Repo):
    round_id = await trail.begin_round(MODE, WAKE, SYSTEM_PROMPT, CONTEXT)
    await trail.end_round(round_id, "", error="LLM 解析失败")
    row = await repo.get_audit_round(round_id)
    assert row.error == "LLM 解析失败"


# ---------- 工具调用链 ----------


async def test_tool_calls_recorded_in_order(trail: AuditTrail, repo: Repo):
    round_id = await _run_full_round(trail)
    calls = await repo.list_audit_tool_calls(round_id)
    assert len(calls) == 2
    assert [c.seq for c in calls] == [0, 1]
    first, second = calls
    assert first.tool == "place_order"
    assert json.loads(first.args_json) == {"contract": "BTC_USDT", "size": 1, "price": "50000"}
    assert first.risk_verdict == "allow"
    assert json.loads(first.result_json)["order_id"] == "t-abc"
    assert first.duration_ms == 120
    assert second.risk_verdict == "deny"
    assert second.risk_reason == "非白名单合约"


# ---------- JSON 快照 ----------


async def test_snapshot_file_replayable(trail: AuditTrail, tmp_path):
    round_id = await _run_full_round(trail)
    path = tmp_path / "audit" / f"round_{round_id}.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    # prompt / 上下文 / LLM 原始输出齐全
    assert data["round"]["prompt_snapshot"] == SYSTEM_PROMPT
    assert data["round"]["context_snapshot"] == CONTEXT
    assert data["round"]["llm_raw"] == LLM_RAW
    assert data["round"]["mode"] == MODE
    # 工具调用链（含风控判定）齐全
    assert len(data["tool_calls"]) == 2
    verdicts = {c["seq"]: c["risk_verdict"] for c in data["tool_calls"]}
    assert verdicts == {0: "allow", 1: "deny"}


async def test_snapshot_dir_created_automatically(trail: AuditTrail, tmp_path):
    audit_dir = tmp_path / "audit"
    assert not audit_dir.exists()
    await _run_full_round(trail)
    assert audit_dir.is_dir()


# ---------- get_round 读取 ----------


async def test_get_round_returns_full_record(trail: AuditTrail):
    round_id = await _run_full_round(trail)
    data = await trail.get_round(round_id)
    assert data["round"]["round_id"] == round_id
    assert len(data["tool_calls"]) == 2


async def test_get_round_unknown_returns_none(trail: AuditTrail):
    assert await trail.get_round("不存在的id") is None
