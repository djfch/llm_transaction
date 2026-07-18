"""POST /api/secrets 与 PUT /api/config LLM 热重建测试（tmp_path 隔离 .env 与 config.yaml）。

冻结契约：
- POST /api/secrets → {"saved": true, "llm_configured": bool, "error": str}（永不回显明文）
- PUT /api/config 改 llm.provider/model/max_tokens/openai_base_url → 原地写回 + 热重建，
  响应在 {"saved","needs_restart"} 上追加 "llm_configured": bool、"llm_error": str
  （reconfigure 失败不 422，配置合法已落盘）
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.config import ROOT, Settings
from src.config_io import write_settings
from src.memory.db import Database
from src.memory.repo import Repo
from src.server import routes_config
from src.server.app import create_app
from src.server.deps import ServerDeps


@pytest.fixture(autouse=True)
def _clean_env():
    """用例前后恢复环境变量原状（端点经 set_env_keys 直接写 os.environ，须手动快照恢复）。"""
    names = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    saved = {k: os.environ.get(k) for k in names}
    for k in names:
        os.environ.pop(k, None)
    yield
    for k in names:
        if saved[k] is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = saved[k]  # type: ignore[index]


@pytest.fixture
async def deps(tmp_path: Path):
    """fake 依赖：tmp 配置/.env + 共享 runtime Settings + 记录调用的假 reconfigure。"""
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)
    db = Database()
    await db.open(tmp_path / "test.db")
    calls: list[dict] = []

    async def fake_reconfigure() -> dict:
        calls.append({})
        return {"llm_configured": True, "error": ""}

    d = ServerDeps(
        repo=Repo(db),
        config_path=config_path,
        watchlist_path=tmp_path / "watchlist.yaml",
        prompt_path=tmp_path / "system_prompt.md",
        web_dist=tmp_path / "no_dist",
        env_path=tmp_path / ".env",
        runtime_settings=Settings(),
        llm_reconfigure=fake_reconfigure,
    )
    d.reconfigure_calls = calls  # 测试断言用
    yield d
    await db.close()


@pytest.fixture
async def client(deps: ServerDeps):
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------- POST /api/secrets ----------


async def test_post_secrets_writes_env_without_leaking(client: AsyncClient, deps: ServerDeps):
    """写 .env 成功并热重建；响应契约逐字、永不回显明文；空值跳过。"""
    r = await client.post(
        "/api/secrets",
        json={"anthropic_api_key": "sk-ant-秘密-xyz", "openai_api_key": ""},
    )
    assert r.status_code == 200
    assert r.json() == {"saved": True, "llm_configured": True, "error": ""}
    assert "sk-ant-秘密-xyz" not in r.text  # 响应永不回显明文
    lines = deps.env_path.read_text(encoding="utf-8").splitlines()
    assert "ANTHROPIC_API_KEY=sk-ant-秘密-xyz" in lines
    assert not any(line.startswith("OPENAI_API_KEY=") for line in lines)  # 空值跳过
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-秘密-xyz"  # 同步环境变量


async def test_post_secrets_calls_set_env_keys(
    client: AsyncClient, deps: ServerDeps, monkeypatch: pytest.MonkeyPatch
):
    """端点经 set_env_keys 落盘（spy 包装断言传参：只写 LLM key、指向 deps.env_path）。"""
    calls: list[tuple[dict, Path]] = []
    real = routes_config.set_env_keys

    def spy(mapping: dict, env_path: Path) -> list:
        calls.append((dict(mapping), env_path))
        return real(mapping, env_path)

    monkeypatch.setattr(routes_config, "set_env_keys", spy)
    await client.post("/api/secrets", json={"anthropic_api_key": "k1"})
    assert len(calls) == 1
    assert calls[0][1] == deps.env_path
    # 只含 LLM key（密钥铁规：交易所 key 无任何前端写入端点）
    assert calls[0][0] == {"ANTHROPIC_API_KEY": "k1", "OPENAI_API_KEY": ""}


async def test_post_secrets_without_reconfigure_wiring(client: AsyncClient, deps: ServerDeps):
    """llm_reconfigure 未接线：诚实回报 agent 未接线（不假装已配置）。"""
    deps.llm_reconfigure = None
    r = await client.post("/api/secrets", json={})
    assert r.json() == {"saved": True, "llm_configured": False, "error": "agent 未接线"}


# ---------- PUT /api/config：llm 热键 ----------


async def test_put_config_llm_model_triggers_reconfigure(client: AsyncClient, deps: ServerDeps):
    """llm.model 移出 needs_restart：原地写回 runtime 并触发热重建。"""
    raw = (await client.get("/api/config")).json()
    raw["llm"]["model"] = "claude-opus-4"
    r = await client.put("/api/config", json=raw)
    assert r.status_code == 200
    assert r.json() == {
        "saved": True,
        "needs_restart": [],
        "llm_configured": True,
        "llm_error": "",
    }
    assert deps.runtime_settings is not None
    assert deps.runtime_settings.llm.model == "claude-opus-4"  # 运行时原地生效
    assert len(deps.reconfigure_calls) == 1


async def test_put_config_llm_reconfigure_error_not_422(client: AsyncClient, deps: ServerDeps):
    """热重建失败不 422：配置合法已落盘，响应携带 llm_error。"""
    assert deps.runtime_settings is not None

    async def failing() -> dict:
        return {"llm_configured": False, "error": "缺少 ANTHROPIC_API_KEY 环境变量"}

    deps.llm_reconfigure = failing
    raw = (await client.get("/api/config")).json()
    raw["llm"]["model"] = "claude-opus-4"
    r = await client.put("/api/config", json=raw)
    assert r.status_code == 200
    body = r.json()
    assert body["saved"] is True and body["needs_restart"] == []
    assert body["llm_configured"] is False
    assert "ANTHROPIC_API_KEY" in body["llm_error"]
    assert deps.runtime_settings.llm.model == "claude-opus-4"  # 配置仍原地写回


async def test_put_config_non_llm_change_skips_reconfigure(client: AsyncClient, deps: ServerDeps):
    """非 llm 热键变更不触发热重建，响应不追加 llm 键（旧契约不变）。"""
    raw = (await client.get("/api/config")).json()
    raw["risk"]["max_position_pct"] = 0.5
    r = await client.put("/api/config", json=raw)
    assert r.json() == {"saved": True, "needs_restart": []}
    assert deps.reconfigure_calls == []


# ---------- deps 默认路径 ----------


def test_env_path_defaults_to_root_env():
    """env_path 缺省指向项目根 .env（生产接线免配置）。"""
    deps = ServerDeps(repo=None)  # 仅取默认路径，不触碰 repo
    assert deps.env_path == ROOT / ".env"


async def test_post_secrets_rejects_newline_injection(client: AsyncClient, deps: ServerDeps):
    """对抗：值含换行可注入任意 .env 行（LLM_MOCK=1/GATE_API_KEY 覆写），必须 422。"""
    r = await client.post(
        "/api/secrets", json={"openai_api_key": "sk-x\nLLM_MOCK=1\nGATE_API_KEY=attacker"}
    )
    assert r.status_code == 422
    text = deps.env_path.read_text(encoding="utf-8") if deps.env_path.exists() else ""
    assert "LLM_MOCK" not in text and "attacker" not in text


async def test_post_secrets_rejects_nul_and_cr(client: AsyncClient):
    """NUL 与回车同样拒绝（pydantic 校验层）。"""
    for bad in ("sk-x\0y", "sk-x\ry"):
        r = await client.post("/api/secrets", json={"anthropic_api_key": bad})
        assert r.status_code == 422, f"应拒绝: {bad!r}"
