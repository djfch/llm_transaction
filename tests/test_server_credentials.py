"""凭证管理端点测试：POST/PUT/DELETE /api/credentials（tmp_path 隔离 .env 与 config.yaml）。

冻结契约：
- 统一响应 {"saved": true, "key_saved": bool, "llm_configured": bool, "llm_error": str}（永不回显明文）
- POST：重名 422、name 非法（^[a-z0-9-]+$）422、空 credentials 首次创建物化 default 再追加
- PUT：未知名 404、api_key_env 不变、编辑 default 物化列表、api_key 留空不动 .env
- DELETE：未知名 404、被 agents.trader/reviewer 引用 422、.env 里的 key 保留不删
三个端点写盘 + 原地写回 runtime 后都只热重建一次（reconfigure 失败不 422）。
请求体校验补齐：model 去空白后非空、max_tokens ≥ 1、openai_compat 必须有 openai_base_url；
api_key 纯空白按未填处理、非空写 strip 后的值；422 响应剔除 detail[].input（密钥铁规）；
key 写 .env 抛 OSError → 500（凭证定义已落盘的半完成态诚实回报）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from src.config import Settings
from src.config_io import write_settings
from src.memory.db import Database
from src.memory.repo import Repo
from src.server import routes_credentials
from src.server.app import create_app
from src.server.deps import ServerDeps


@pytest.fixture(autouse=True)
def _clean_env():
    """用例前后恢复环境变量原状（端点经 set_env_keys 直接写 os.environ，须手动快照恢复）。"""
    names = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
    names += [k for k in os.environ if k.startswith("LLM_KEY_")]
    saved = {k: os.environ.get(k) for k in names}
    for k in names:
        os.environ.pop(k, None)
    yield
    for k in names:
        if saved[k] is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = saved[k]  # type: ignore[index]
    for k in [k for k in os.environ if k.startswith("LLM_KEY_") and k not in saved]:
        os.environ.pop(k, None)  # 用例期间新增的 LLM_KEY_* 一并清除


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


def _file_credentials(deps: ServerDeps) -> list[dict]:
    """直接读 config.yaml 原文里的 llm.credentials（断言落盘内容，不经过端点）。"""
    raw = yaml.safe_load(deps.config_path.read_text(encoding="utf-8"))
    return raw.get("llm", {}).get("credentials", [])


async def _create(client: AsyncClient, name: str = "main", **fields) -> None:
    """快捷创建一条凭证并断言成功（缺省不含 key）。"""
    body = {"name": name, "model": "claude-sonnet-4-5", **fields}
    r = await client.post("/api/credentials", json=body)
    assert r.status_code == 200, r.text


# ---------- POST /api/credentials ----------


async def test_post_credential_with_key_writes_env_and_reconfigures(
    client: AsyncClient, deps: ServerDeps
):
    """含 key 创建：统一响应契约逐字；key 写进推导的 env 键并同步环境变量；
    响应无明文；落盘 + 原地写回 runtime；只热重建一次。"""
    r = await client.post(
        "/api/credentials",
        json={"name": "main", "model": "claude-sonnet-4-5", "api_key": "sk-main-秘密"},
    )
    assert r.status_code == 200
    assert r.json() == {"saved": True, "key_saved": True, "llm_configured": True, "llm_error": ""}
    assert "sk-main-秘密" not in r.text  # 响应永不回显明文
    lines = deps.env_path.read_text(encoding="utf-8").splitlines()
    assert "LLM_KEY_MAIN=sk-main-秘密" in lines  # key 写进正确 env 键
    assert os.environ["LLM_KEY_MAIN"] == "sk-main-秘密"
    assert len(deps.reconfigure_calls) == 1  # 一次请求只热重建一次
    creds = {c["name"]: c for c in _file_credentials(deps)}
    assert creds["main"]["provider"] == "anthropic"
    assert creds["main"]["api_key_env"] == "LLM_KEY_MAIN"
    assert deps.runtime_settings is not None
    assert [c.name for c in deps.runtime_settings.llm.credentials] == ["default", "main"]


async def test_post_credential_without_key_skips_env(client: AsyncClient, deps: ServerDeps):
    """不含 key：key_saved=False 且 .env 不落 key；配置仍落盘并热重建。"""
    r = await client.post("/api/credentials", json={"name": "main", "model": "m1"})
    assert r.status_code == 200
    assert r.json() == {"saved": True, "key_saved": False, "llm_configured": True, "llm_error": ""}
    env_text = deps.env_path.read_text(encoding="utf-8") if deps.env_path.exists() else ""
    assert "LLM_KEY_MAIN" not in env_text
    assert len(deps.reconfigure_calls) == 1


async def test_post_credential_materializes_default_on_first_create(
    client: AsyncClient, deps: ServerDeps
):
    """空 credentials 首次创建：物化 default（保留旧平铺字段与 ANTHROPIC_API_KEY）再追加。"""
    await _create(client, "main")
    creds = _file_credentials(deps)
    assert [c["name"] for c in creds] == ["default", "main"]
    default = creds[0]
    assert default["provider"] == "anthropic"
    assert default["model"] == "claude-sonnet-4-5"  # 来自旧平铺 llm.model
    assert default["api_key_env"] == "ANTHROPIC_API_KEY"  # 旧键名保留
    assert deps.runtime_settings is not None
    assert deps.runtime_settings.agents.trader.credential == "default"  # 引用不受影响


async def test_post_credential_duplicate_name_422(client: AsyncClient, deps: ServerDeps):
    """重名 422；与物化的 default 重名同样 422；配置不落盘。"""
    await _create(client, "main")
    r = await client.post("/api/credentials", json={"name": "main", "model": "m2"})
    assert r.status_code == 422
    assert "已存在" in r.json()["detail"]
    r = await client.post("/api/credentials", json={"name": "default", "model": "m2"})
    assert r.status_code == 422  # default 已被物化，重名
    assert [c["name"] for c in _file_credentials(deps)] == ["default", "main"]  # 未变


async def test_post_credential_invalid_name_422(client: AsyncClient, deps: ServerDeps):
    """name 必须匹配 ^[a-z0-9-]+$（与前端一致）：大写/下划线/中文/空串都 422。"""
    for bad in ("Main", "my_cred", "凭证", "", "name with space"):
        r = await client.post("/api/credentials", json={"name": bad, "model": "m1"})
        assert r.status_code == 422, f"应拒绝: {bad!r}"
    assert _file_credentials(deps) == []  # 全部未落盘


async def test_post_credential_rejects_control_chars(client: AsyncClient, deps: ServerDeps):
    """对抗：api_key 含换行可注入任意 .env 行（GATE_API_KEY 覆写），必须 422。"""
    r = await client.post(
        "/api/credentials",
        json={"name": "main", "model": "m1", "api_key": "sk-x\nGATE_API_KEY=attacker"},
    )
    assert r.status_code == 422
    for bad in ("sk-x\0y", "sk-x\ry"):
        r = await client.post(
            "/api/credentials", json={"name": "main", "model": "m1", "api_key": bad}
        )
        assert r.status_code == 422, f"api_key 应拒绝: {bad!r}"
    text = deps.env_path.read_text(encoding="utf-8") if deps.env_path.exists() else ""
    assert "attacker" not in text
    assert _file_credentials(deps) == []  # 请求体校验层拦截，配置不落盘


# ---------- 请求体校验补齐与 422 明文回显防护 ----------


def _all_strings(obj: object) -> list[str]:
    """递归收集 JSON 结构里的全部字符串（泄漏扫描用）。"""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _all_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _all_strings(v)]
    return []


async def test_post_credential_422_never_echoes_api_key(client: AsyncClient):
    """422 响应不得回显明文：全局处理器必须剔除 detail[].input 并保留数组结构。"""
    secret = "sk-凭证明文-51db08"
    r = await client.post(
        "/api/credentials",
        json={"name": "main", "model": "m1", "api_key": f"{secret}\nGATE_API_KEY=attacker"},
    )
    assert r.status_code == 422
    assert secret not in r.text  # 逐字断言：响应体任何位置不含明文
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail
    for err in detail:
        assert "input" not in err  # 明文载体被剔除，其余字段保留
        assert "msg" in err and "loc" in err
    for s in _all_strings(r.json()):
        assert secret not in s


async def test_post_credential_field_validation(client: AsyncClient, deps: ServerDeps):
    """请求体补齐校验：max_tokens<1、model 空/纯空白、openai_compat 缺/空白 base_url → 422。"""
    base: dict = {"name": "main", "model": "m1"}
    for bad in (
        {"max_tokens": 0},
        {"max_tokens": -5},
        {"model": ""},
        {"model": "   "},
        {"provider": "openai_compat"},  # 缺 openai_base_url
        {"provider": "openai_compat", "openai_base_url": "   "},  # 空白 base_url
    ):
        r = await client.post("/api/credentials", json={**base, **bad})
        assert r.status_code == 422, f"应拒绝: {bad}"
    assert _file_credentials(deps) == []  # 全部未落盘
    # openai_compat 错误文案为中文且指明字段
    r = await client.post("/api/credentials", json={**base, "provider": "openai_compat"})
    msgs = [e["msg"] for e in r.json()["detail"]]
    assert any("openai_base_url" in m and "不能为空" in m for m in msgs)
    # PUT 共享同一字段模型：同样拦截
    await _create(client, "main")
    r = await client.put("/api/credentials/main", json={"model": "m2", "max_tokens": 0})
    assert r.status_code == 422


async def test_post_credential_blank_whitespace_key_is_unset(client: AsyncClient, deps: ServerDeps):
    """api_key 纯空白按未填处理：200、key_saved=false、.env 不落该键。"""
    r = await client.post(
        "/api/credentials", json={"name": "main", "model": "m1", "api_key": "   "}
    )
    assert r.status_code == 200
    assert r.json()["key_saved"] is False
    env_text = deps.env_path.read_text(encoding="utf-8") if deps.env_path.exists() else ""
    assert "LLM_KEY_MAIN" not in env_text


async def test_post_credential_key_stripped_before_write(client: AsyncClient, deps: ServerDeps):
    """api_key 带首尾空白：.env 与 os.environ 写入 strip 后的值。"""
    r = await client.post(
        "/api/credentials", json={"name": "main", "model": "m1", "api_key": "  sk-pad-秘密  "}
    )
    assert r.status_code == 200
    assert r.json()["key_saved"] is True
    lines = deps.env_path.read_text(encoding="utf-8").splitlines()
    assert "LLM_KEY_MAIN=sk-pad-秘密" in lines  # strip 后的值落盘
    assert os.environ["LLM_KEY_MAIN"] == "sk-pad-秘密"


async def test_post_credential_env_write_oserror_500(
    client: AsyncClient, deps: ServerDeps, monkeypatch: pytest.MonkeyPatch
):
    """key 写 .env 抛 OSError（磁盘满/权限）→ 500 诚实回报半完成态：凭证定义已落盘，
    detail 指引经编辑端点补 key，响应不回显明文。"""

    def _boom(mapping: dict, env_path: Path) -> None:
        raise OSError("磁盘已满")

    monkeypatch.setattr(routes_credentials, "set_env_keys", _boom)
    r = await client.post(
        "/api/credentials", json={"name": "main", "model": "m1", "api_key": "sk-x"}
    )
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "key 写入 .env 失败" in detail
    assert "PUT /api/credentials/main" in detail  # 指引用编辑功能补 key
    assert "sk-x" not in r.text  # 响应永不回显明文
    assert [c["name"] for c in _file_credentials(deps)] == ["default", "main"]  # 定义已落盘


# ---------- PUT /api/credentials/{name} ----------


async def test_put_credential_updates_model_and_base_url(client: AsyncClient, deps: ServerDeps):
    """改 model + openai_base_url：落盘 + 原地写回 runtime 并热重建；api_key_env 不变。"""
    await _create(client, "backup", provider="openai_compat", openai_base_url="https://a.example")
    r = await client.put(
        "/api/credentials/backup",
        json={
            "provider": "openai_compat",
            "model": "deepseek-v4-flash",
            "max_tokens": 8192,
            "openai_base_url": "https://b.example",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"saved": True, "key_saved": False, "llm_configured": True, "llm_error": ""}
    cred = {c["name"]: c for c in _file_credentials(deps)}["backup"]
    assert cred["model"] == "deepseek-v4-flash"
    assert cred["max_tokens"] == 8192
    assert cred["openai_base_url"] == "https://b.example"
    assert cred["api_key_env"] == "LLM_KEY_BACKUP"  # api_key_env 保持不变
    assert deps.runtime_settings is not None
    runtime = {c.name: c for c in deps.runtime_settings.llm.credentials}["backup"]
    assert runtime.model == "deepseek-v4-flash"  # 运行时原地生效


async def test_put_credential_blank_key_keeps_env(client: AsyncClient, deps: ServerDeps):
    """api_key 留空：不动 .env（key_saved=False），原 key 保留。"""
    await _create(client, "main", api_key="sk-old")
    r = await client.put("/api/credentials/main", json={"model": "claude-opus-4"})
    assert r.status_code == 200
    assert r.json()["key_saved"] is False
    lines = deps.env_path.read_text(encoding="utf-8").splitlines()
    assert "LLM_KEY_MAIN=sk-old" in lines  # 原 key 原样保留
    assert os.environ["LLM_KEY_MAIN"] == "sk-old"


async def test_put_credential_replaces_key(client: AsyncClient, deps: ServerDeps):
    """换 key：同一 env 键原地替换并同步环境变量；响应无明文。"""
    await _create(client, "main", api_key="sk-old")
    r = await client.put(
        "/api/credentials/main", json={"model": "claude-opus-4", "api_key": "sk-new-秘密"}
    )
    assert r.status_code == 200
    assert r.json()["key_saved"] is True
    assert "sk-new-秘密" not in r.text  # 响应永不回显明文
    lines = deps.env_path.read_text(encoding="utf-8").splitlines()
    assert "LLM_KEY_MAIN=sk-new-秘密" in lines
    assert not any(line == "LLM_KEY_MAIN=sk-old" for line in lines)
    assert os.environ["LLM_KEY_MAIN"] == "sk-new-秘密"


async def test_put_credential_unknown_name_404(client: AsyncClient):
    """未知名 404（请求体无 name，路径参数即身份）。"""
    r = await client.put("/api/credentials/ghost", json={"model": "m1"})
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


async def test_put_credential_default_materializes_list(client: AsyncClient, deps: ServerDeps):
    """编辑 default 合成凭证：自然物化列表；api_key_env 保持 ANTHROPIC_API_KEY 不变。"""
    r = await client.put(
        "/api/credentials/default",
        json={"model": "claude-opus-4", "max_tokens": 8192},
    )
    assert r.status_code == 200
    creds = _file_credentials(deps)
    assert [c["name"] for c in creds] == ["default"]  # 物化为显式列表
    assert creds[0]["model"] == "claude-opus-4"
    assert creds[0]["max_tokens"] == 8192
    assert creds[0]["api_key_env"] == "ANTHROPIC_API_KEY"  # 旧键名保留（key 位置不动）
    assert deps.runtime_settings is not None
    assert [c.name for c in deps.runtime_settings.llm.credentials] == ["default"]
    assert deps.runtime_settings.llm.credentials[0].model == "claude-opus-4"


async def test_put_credential_rejects_control_chars(client: AsyncClient, deps: ServerDeps):
    """PUT 的 api_key 同样拒绝控制字符（与 SecretsBody 同一防护）。"""
    await _create(client, "main")
    r = await client.put(
        "/api/credentials/main", json={"model": "m1", "api_key": "sk-x\nLLM_MOCK=1"}
    )
    assert r.status_code == 422
    text = deps.env_path.read_text(encoding="utf-8") if deps.env_path.exists() else ""
    assert "LLM_MOCK" not in text


# ---------- DELETE /api/credentials/{name} ----------


async def test_delete_credential_success(client: AsyncClient, deps: ServerDeps):
    """删除未被引用的凭证：落盘 + 原地写回 runtime 并热重建（key_saved=False）。"""
    await _create(client, "main")
    reconfigure_before = len(deps.reconfigure_calls)
    r = await client.delete("/api/credentials/main")
    assert r.status_code == 200
    assert r.json() == {"saved": True, "key_saved": False, "llm_configured": True, "llm_error": ""}
    assert [c["name"] for c in _file_credentials(deps)] == ["default"]
    assert deps.runtime_settings is not None
    assert [c.name for c in deps.runtime_settings.llm.credentials] == ["default"]
    assert len(deps.reconfigure_calls) == reconfigure_before + 1  # 删后热重建


async def test_delete_credential_referenced_422(client: AsyncClient, deps: ServerDeps):
    """被 agents.trader/reviewer 引用 422（提示先解除引用）；配置不落盘、不热重建。"""
    r = await client.delete("/api/credentials/default")  # 两个 agent 都引用 default
    assert r.status_code == 422
    assert "引用" in r.json()["detail"]
    assert _file_credentials(deps) == []  # 未落盘（default 仍是合成态）
    assert deps.reconfigure_calls == []
    # 多凭证场景：登记 main/backup 并分配后，删除被引用的 backup 同样 422
    # （凭证登记走专用端点：PUT /api/config 已剥离 llm.credentials 键；首次创建物化 default）
    await _create(client, "main", model="m1")
    await _create(client, "backup", model="m2")
    raw = (await client.get("/api/config")).json()
    raw["agents"] = {"trader": {"credential": "main"}, "reviewer": {"credential": "backup"}}
    assert (await client.put("/api/config", json=raw)).status_code == 200
    r = await client.delete("/api/credentials/backup")
    assert r.status_code == 422
    assert [c["name"] for c in _file_credentials(deps)] == ["default", "main", "backup"]  # 未变


async def test_delete_credential_unknown_name_404(client: AsyncClient):
    """未知名 404。"""
    r = await client.delete("/api/credentials/ghost")
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


async def test_delete_credential_keeps_env_key(client: AsyncClient, deps: ServerDeps):
    """删除后 .env 里的 key 保留不删（与现状一致）。"""
    await _create(client, "main", api_key="sk-keep")
    r = await client.delete("/api/credentials/main")
    assert r.status_code == 200
    lines = deps.env_path.read_text(encoding="utf-8").splitlines()
    assert "LLM_KEY_MAIN=sk-keep" in lines  # key 保留
