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
    """构造指向临时 SQLite 数据库的 Repo 夹具，用后自动关闭连接。

    参数：
        tmp_path: Path，pytest 临时目录夹具，审计测试数据库文件落在其中

    返回：
        AsyncIterator[Repo]，yield 已打开临时数据库的仓储对象，测试结束后关闭连接
    """
    db = Database()
    await db.open(tmp_path / "audit.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
def trail(repo: Repo, tmp_path) -> AuditTrail:
    """构造使用临时数据库与临时快照目录的 AuditTrail 夹具。

    参数：
        repo: Repo，临时数据库仓储夹具，审计主表与工具调用记录写入其中
        tmp_path: Path，pytest 临时目录夹具，JSON 快照目录落在其 audit 子目录

    返回：
        AuditTrail：绑定临时仓储与快照目录的审计追踪对象
    """
    return AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit")))


async def _run_full_round(trail: AuditTrail) -> str:
    """模拟完整一轮：begin → 2 次工具调用（放行+拒绝）→ end。

    参数：
        trail: AuditTrail，待写入或查询的审计轨迹实例

    返回：
        str，本轮完整审计链路共用的轮次编号
    """
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
    """校验 begin_round 把一轮决策的完整输入写入审计主表且尚未结束。

    参数：
        trail: AuditTrail，审计追踪夹具，调用 begin_round 开启一轮
        repo: Repo，临时数据库仓储夹具，读回主表记录核对

    返回：
        None，断言 round_id 非空，主表记录的 mode、wake_source、prompt 的 md5 与
        快照、上下文快照均与输入一致，且 ended_at 为 None（未结束）
    """
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
    """校验 end_round 后主表补齐 LLM 原始输出与结束时间且无错误。

    参数：
        trail: AuditTrail，审计追踪夹具，跑完一轮完整流程
        repo: Repo，临时数据库仓储夹具，读回主表记录核对

    返回：
        None，断言主表 llm_raw 等于 LLM 原始输出、ended_at 已写入且 error 为空串
    """
    round_id = await _run_full_round(trail)
    row = await repo.get_audit_round(round_id)
    assert row.llm_raw == LLM_RAW
    assert row.ended_at is not None
    assert row.error == ""


async def test_end_round_with_error(trail: AuditTrail, repo: Repo):
    """校验带错误信息结束一轮时错误内容写入主表。

    参数：
        trail: AuditTrail，审计追踪夹具，以 error 参数结束一轮
        repo: Repo，临时数据库仓储夹具，读回主表记录核对

    返回：
        None，断言主表 error 字段等于传入的"LLM 解析失败"
    """
    round_id = await trail.begin_round(MODE, WAKE, SYSTEM_PROMPT, CONTEXT)
    await trail.end_round(round_id, "", error="LLM 解析失败")
    row = await repo.get_audit_round(round_id)
    assert row.error == "LLM 解析失败"


# ---------- 工具调用链 ----------


async def test_tool_calls_recorded_in_order(trail: AuditTrail, repo: Repo):
    """校验一轮内两次工具调用按 seq 顺序完整落库，含风控判定与结果。

    参数：
        trail: AuditTrail，审计追踪夹具，跑完一轮含放行与拒绝各一次的流程
        repo: Repo，临时数据库仓储夹具，读回工具调用记录核对

    返回：
        None，断言两次调用按 seq 0、1 排列，调用参数、风控判定（allow/deny）、
        拒绝原因、结果内容与耗时均与记录时一致
    """
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
    """校验一轮结束后生成的 JSON 快照文件内容完整、可回放整轮决策。

    参数：
        trail: AuditTrail，审计追踪夹具，跑完一轮完整流程生成快照
        tmp_path: Path，pytest 临时目录夹具，定位快照文件路径

    返回：
        None，断言快照文件存在，且其中 prompt、上下文、LLM 原始输出、模式与
        两次工具调用的风控判定（seq 0 放行、seq 1 拒绝）均与输入一致
    """
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
    """校验快照目录不存在时由 AuditTrail 自动创建。

    参数：
        trail: AuditTrail，审计追踪夹具，其快照目录预先不存在
        tmp_path: Path，pytest 临时目录夹具，检查 audit 子目录是否生成

    返回：
        None，断言跑完一轮前 audit 目录不存在、跑完后被自动创建为目录
    """
    audit_dir = tmp_path / "audit"
    assert not audit_dir.exists()
    await _run_full_round(trail)
    assert audit_dir.is_dir()


# ---------- get_round 读取 ----------


async def test_get_round_returns_full_record(trail: AuditTrail):
    """校验 get_round 能读回一整轮的完整记录（主表 + 工具调用链）。

    参数：
        trail: AuditTrail，审计追踪夹具，跑完一轮后读回该轮记录

    返回：
        None，断言返回的主表 round_id 与本轮一致且工具调用链含 2 条记录
    """
    round_id = await _run_full_round(trail)
    data = await trail.get_round(round_id)
    assert data["round"]["round_id"] == round_id
    assert len(data["tool_calls"]) == 2


async def test_get_round_unknown_returns_none(trail: AuditTrail):
    """校验查询不存在的 round_id 时 get_round 返回 None 而非报错。

    参数：
        trail: AuditTrail，审计追踪夹具，传入未记录过的 round_id 查询

    返回：
        None，断言 get_round 对未知 round_id 返回 None
    """
    assert await trail.get_round("不存在的id") is None
