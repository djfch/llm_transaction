"""GET /api/agent/live 测试：仪表盘实时决策展示（当前模式最新一轮 + 已落库工具调用）。

进行中的轮由 repo 直写审计行模拟（ended_at=NULL、llm_raw 空）；in_round 来自
status_provider（调度器防重入标记），缺省 False；mode 优先 runtime_settings。
"""

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from src.config import Settings
from src.config_io import write_settings
from src.memory.db import Database
from src.memory.repo import Repo
from src.server.app import create_app
from src.server.deps import ServerDeps


@pytest.fixture
async def repo(tmp_path: Path):
    """构造指向临时数据库的 Repo 实例，测试结束后关闭数据库连接。

    参数：
        tmp_path: Path，pytest 临时目录夹具，数据库文件 test.db 落在其中

    返回：
        AsyncIterator[Repo]，yield 已打开临时数据库的仓储对象，收尾时关闭连接
    """
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


def _deps(repo: Repo, tmp_path: Path, status: dict, **overrides: Any) -> ServerDeps:
    """组装 fake 依赖：tmp 配置（默认 mode=paper）+ 指定运行时状态 dict。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 提供的临时目录夹具
        status: dict，运行时状态数据
        **overrides: Any，额外关键字参数

    返回：
        ServerDeps：绑定临时配置、运行状态与可选覆盖项的服务端依赖
    """
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)  # 默认配置（mode=paper）
    return ServerDeps(
        repo=repo,
        status_provider=lambda: status,
        config_path=config_path,
        web_dist=tmp_path / "no_dist",
        **overrides,
    )


async def _get_live(deps: ServerDeps) -> dict:
    """以测试客户端请求 GET /api/agent/live 并返回响应体。

    参数：
        deps: ServerDeps，装配好的服务端依赖，用于创建 FastAPI 应用

    返回：
        dict：响应 JSON（已断言状态码为 200）
    """
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/agent/live")
        assert r.status_code == 200
        return r.json()


async def test_live_in_progress_round(repo: Repo, tmp_path: Path):
    """进行中轮（ended_at=NULL、llm_raw 空）：返回该轮与已落库工具调用，args/result 解析为对象。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    await repo.start_audit_round("r-old", "paper", wake_source="timer", started_at=1000.0)
    await repo.finish_audit_round("r-old", llm_raw="旧轮原文", ended_at=1010.0)
    await repo.start_audit_round(
        "r-live",
        "paper",
        wake_source="price_alert",
        prompt_md5="md5-live",
        prompt_snapshot="提示词全文",
        context_snapshot="上下文全文",
        started_at=2000.0,
    )  # 不落 finish_audit_round：模拟进行中
    await repo.save_audit_tool_call(
        "r-live",
        seq=1,
        tool="get_account",
        args_json='{"verbose": true}',
        risk_verdict="allow",
        result_json='{"text": "账户概要"}',
        duration_ms=12,
    )
    await repo.save_audit_tool_call(
        "r-live",
        seq=2,
        tool="place_order",
        args_json="{}",
        risk_verdict="deny",
        risk_reason="超出单笔上限",
        result_json='{"text": "已拒绝"}',
        duration_ms=3,
    )
    body = await _get_live(_deps(repo, tmp_path, {"in_round": True}))
    assert set(body) == {"in_round", "round", "tool_calls"}  # 顶层键精确锁定
    assert body["in_round"] is True
    assert body["round"] == {  # 最新一轮（r-live 进行中），契约逐字对齐（无 mode 键）
        "round_id": "r-live",
        "wake_source": "price_alert",
        "prompt_md5": "md5-live",
        "strategy_md5": "",
        "prompt_snapshot": "提示词全文",
        "context_snapshot": "上下文全文",
        "llm_raw": "",
        "started_at": 2000.0,
        "ended_at": None,
        "error": "",
        "llm_credential_name": "",
        "llm_provider": "",
        "llm_model": "",
        "llm_thinking_effort": "",
    }
    assert body["tool_calls"] == [
        {
            "seq": 1,
            "tool": "get_account",
            "args": {"verbose": True},
            "risk_verdict": "allow",
            "risk_reason": "",
            "result": {"text": "账户概要"},
            "duration_ms": 12,
        },
        {
            "seq": 2,
            "tool": "place_order",
            "args": {},
            "risk_verdict": "deny",
            "risk_reason": "超出单笔上限",
            "result": {"text": "已拒绝"},
            "duration_ms": 3,
        },
    ]


async def test_live_no_round_returns_null(repo: Repo, tmp_path: Path):
    """无审计轮：round 为 null、tool_calls 空；status_provider 无 in_round 键时缺省 False。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    body = await _get_live(_deps(repo, tmp_path, {"uptime_seconds": 1}))
    assert body == {"in_round": False, "round": None, "tool_calls": []}


async def test_live_finished_latest_round(repo: Repo, tmp_path: Path):
    """最新轮已结束（空闲常态）：ended_at 为时间戳、llm_raw 非空、in_round=False。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    await repo.start_audit_round("r1", "paper", wake_source="timer", started_at=1000.0)
    await repo.finish_audit_round("r1", llm_raw="LLM 原文", ended_at=1010.0)
    body = await _get_live(_deps(repo, tmp_path, {"in_round": False}))
    assert set(body) == {"in_round", "round", "tool_calls"}
    assert body["in_round"] is False
    assert body["round"] == {
        "round_id": "r1",
        "wake_source": "timer",
        "prompt_md5": "",
        "strategy_md5": "",
        "prompt_snapshot": "",
        "context_snapshot": "",
        "llm_raw": "LLM 原文",
        "started_at": 1000.0,
        "ended_at": 1010.0,
        "error": "",
        "llm_credential_name": "",
        "llm_provider": "",
        "llm_model": "",
        "llm_thinking_effort": "",
    }
    assert body["tool_calls"] == []


async def test_live_invalid_json_kept_as_string(repo: Repo, tmp_path: Path):
    """args/result 非法 JSON 时保留原字符串（不抛错、不丢信息）。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    await repo.start_audit_round("r1", "paper", started_at=1.0)
    await repo.save_audit_tool_call(
        "r1", seq=1, tool="get_account", args_json="{非法", result_json="纯文本结果"
    )
    call = (await _get_live(_deps(repo, tmp_path, {})))["tool_calls"][0]
    assert call["args"] == "{非法"
    assert call["result"] == "纯文本结果"


async def test_live_prefers_runtime_settings_mode(repo: Repo, tmp_path: Path):
    """mode 优先取 runtime_settings（配置文件 mode 不同也不影响口径）。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    await repo.start_audit_round("r-paper", "paper", started_at=1000.0)
    await repo.start_audit_round("r-testnet", "testnet", started_at=2000.0)
    deps = _deps(repo, tmp_path, {}, runtime_settings=Settings(mode="testnet"))
    body = await _get_live(deps)
    assert body["round"]["round_id"] == "r-testnet"


async def test_live_excludes_review_research_rounds(repo: Repo, tmp_path: Path):
    """trader 视图只看交易轮：更新的复盘/研报轮不污染 /api/agent/live（各有专属端点）。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    await repo.start_audit_round("r-trader", "paper", wake_source="timer", started_at=1000.0)
    await repo.finish_audit_round("r-trader", llm_raw="交易轮原文", ended_at=1010.0)
    # 更新的复盘/研报轮（started_at 更大，生产中研报每天三轮、trader 60min 一轮）
    await repo.start_audit_round("r-review", "paper", wake_source="review", started_at=2000.0)
    await repo.start_audit_round("r-research", "paper", wake_source="research", started_at=3000.0)
    body = await _get_live(_deps(repo, tmp_path, {}))
    assert body["round"]["round_id"] == "r-trader"
    assert body["round"]["ended_at"] == 1010.0
