"""POST /api/secrets 与 PUT /api/config LLM 热重建测试（tmp_path 隔离 .env 与 config.yaml）。

冻结契约：
- POST /api/secrets → {"saved": true, "llm_configured": bool, "error": str}（永不回显明文）
- PUT /api/config 改 llm.provider/model/max_tokens/openai_base_url → 原地写回 + 热重建，
  响应在 {"saved","needs_restart"} 上追加 "llm_configured": bool、"llm_error": str
  （reconfigure 失败不 422，配置合法已落盘）
- PUT /api/config 体里 llm 段的 credentials 键被剥离忽略（凭证写权收归 /api/credentials，
  防表单旧快照整体替换静默回滚）；agents.*.credential 分配仍归 PUT 管辖并触发热重建
- 请求体校验失败的 422 响应剔除 detail[].input（密钥铁规：明文不随校验错误回显）
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
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
    """用例前后恢复环境变量原状（端点经 set_env_keys 直接写 os.environ，须手动快照恢复）。

    参数：无
    返回：
        Iterator[None]，yield 给用例执行，并在收尾恢复或删除相关 LLM 环境变量
    """
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
    """fake 依赖：tmp 配置/.env + 共享 runtime Settings + 记录调用的假 reconfigure。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        AsyncIterator[ServerDeps]，yield 热重建测试依赖，并在夹具收尾关闭数据库
    """
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)
    db = Database()
    await db.open(tmp_path / "test.db")
    calls: list[dict] = []

    async def fake_reconfigure() -> dict:
        """模拟 LLM 热重建成功：记录一次调用并回报已配置。

        参数：无
        返回：
            dict，返回该测试辅助函数构造或记录的结果
        """
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
    """创建注入测试依赖的测试客户端。

    参数：
        deps: ServerDeps，服务端测试依赖
    返回：
        AsyncIterator[AsyncClient]，yield 直连测试应用的客户端并在退出上下文时关闭
    """
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------- POST /api/secrets ----------


async def test_post_secrets_writes_env_without_leaking(client: AsyncClient, deps: ServerDeps):
    """写 .env 成功并热重建；响应契约逐字、永不回显明文；空值跳过。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
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
    """端点经 set_env_keys 落盘（spy 包装断言传参：只写 LLM key、指向 deps.env_path）。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
    calls: list[tuple[dict, Path]] = []
    real = routes_config.set_env_keys

    def spy(mapping: dict, env_path: Path) -> list:
        """记录待写入环境文件的密钥映射。

        参数：
            mapping: dict，环境变量名与密钥值映射
            env_path: Path，环境文件路径
        返回：
            list，返回该测试辅助函数构造或记录的结果
        """
        calls.append((dict(mapping), env_path))
        return real(mapping, env_path)

    monkeypatch.setattr(routes_config, "set_env_keys", spy)
    await client.post("/api/secrets", json={"anthropic_api_key": "k1"})
    assert len(calls) == 1
    assert calls[0][1] == deps.env_path
    # 只含 LLM key（密钥铁规：交易所 key 无任何前端写入端点）
    assert calls[0][0] == {"ANTHROPIC_API_KEY": "k1", "OPENAI_API_KEY": ""}


async def test_post_secrets_without_reconfigure_wiring(client: AsyncClient, deps: ServerDeps):
    """llm_reconfigure 未接线：诚实回报 agent 未接线（不假装已配置）。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    deps.llm_reconfigure = None
    r = await client.post("/api/secrets", json={})
    assert r.json() == {"saved": True, "llm_configured": False, "error": "agent 未接线"}


# ---------- PUT /api/config：llm 热键 ----------


async def test_put_config_llm_model_triggers_reconfigure(client: AsyncClient, deps: ServerDeps):
    """llm.model 移出 needs_restart：原地写回 runtime 并触发热重建。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
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
    """热重建失败不 422：配置合法已落盘，响应携带 llm_error。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    assert deps.runtime_settings is not None

    async def failing() -> dict:
        """模拟模型重配置失败。

        参数：无
        返回：
            dict，不会正常返回，用于模拟失败路径
        """
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
    """非 llm 热键变更不触发热重建，响应不追加 llm 键（旧契约不变）。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    raw = (await client.get("/api/config")).json()
    raw["risk"]["max_position_pct"] = 0.5
    r = await client.put("/api/config", json=raw)
    assert r.json() == {"saved": True, "needs_restart": []}
    assert deps.reconfigure_calls == []


# ---------- deps 默认路径 ----------


def test_env_path_defaults_to_root_env():
    """env_path 缺省指向项目根 .env（生产接线免配置）。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    deps = ServerDeps(repo=None)  # 仅取默认路径，不触碰 repo
    assert deps.env_path == ROOT / ".env"


async def test_post_secrets_rejects_newline_injection(client: AsyncClient, deps: ServerDeps):
    """对抗：值含换行可注入任意 .env 行（LLM_MOCK=1/GATE_API_KEY 覆写），必须 422。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    r = await client.post(
        "/api/secrets", json={"openai_api_key": "sk-x\nLLM_MOCK=1\nGATE_API_KEY=attacker"}
    )
    assert r.status_code == 422
    text = deps.env_path.read_text(encoding="utf-8") if deps.env_path.exists() else ""
    assert "LLM_MOCK" not in text and "attacker" not in text


async def test_post_secrets_rejects_nul_and_cr(client: AsyncClient):
    """NUL 与回车同样拒绝（pydantic 校验层）。

    参数：
        client: AsyncClient，测试客户端
    返回：
        None，执行断言验证目标行为
    """
    for bad in ("sk-x\0y", "sk-x\ry"):
        r = await client.post("/api/secrets", json={"anthropic_api_key": bad})
        assert r.status_code == 422, f"应拒绝: {bad!r}"
    for bad in ("sk-x\0y", "sk-x\ny"):
        r = await client.post("/api/secrets", json={"credential": "c", "api_key": bad})
        assert r.status_code == 422, f"api_key 应拒绝: {bad!r}"


# ---------- 多凭证：{credential, api_key} 形式 ----------


async def _put_two_credentials(client: AsyncClient) -> None:
    """登记两条凭证并分配：trader→main，reviewer→backup。

    参数：
        client: AsyncClient，测试客户端
    返回：
        None，返回该测试辅助函数构造或记录的结果
    """
    r = await client.post("/api/credentials", json={"name": "main", "model": "claude-sonnet-4-5"})
    assert r.status_code == 200
    r = await client.post(
        "/api/credentials",
        json={
            "name": "backup",
            "provider": "openai_compat",
            "model": "deepseek-v4-flash",
            "openai_base_url": "https://api.example.com",
        },
    )
    assert r.status_code == 200
    raw = (await client.get("/api/config")).json()
    raw["agents"] = {"trader": {"credential": "main"}, "reviewer": {"credential": "backup"}}
    r = await client.put("/api/config", json=raw)
    assert r.status_code == 200


async def test_post_secrets_credential_writes_mapped_env_key(client: AsyncClient, deps: ServerDeps):
    """{credential, api_key}：按凭证 api_key_env 写 .env（缺省推导 LLM_KEY_<NAME>）并热重建。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    await _put_two_credentials(client)
    reconfigure_before = len(deps.reconfigure_calls)
    r = await client.post("/api/secrets", json={"credential": "backup", "api_key": "sk-b-秘密"})
    assert r.status_code == 200
    assert r.json() == {"saved": True, "llm_configured": True, "error": ""}
    assert "sk-b-秘密" not in r.text  # 响应永不回显明文
    lines = deps.env_path.read_text(encoding="utf-8").splitlines()
    assert "LLM_KEY_BACKUP=sk-b-秘密" in lines
    assert os.environ["LLM_KEY_BACKUP"] == "sk-b-秘密"  # 同步环境变量
    assert len(deps.reconfigure_calls) == reconfigure_before + 1  # 触发热重建


async def test_post_secrets_credential_and_legacy_fields_mix(client: AsyncClient, deps: ServerDeps):
    """credential 形式与旧字段可同请求混用，两者都落盘。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    await _put_two_credentials(client)
    r = await client.post(
        "/api/secrets",
        json={"credential": "main", "api_key": "sk-m", "anthropic_api_key": "sk-ant-old"},
    )
    assert r.status_code == 200
    lines = deps.env_path.read_text(encoding="utf-8").splitlines()
    assert "LLM_KEY_MAIN=sk-m" in lines
    assert "ANTHROPIC_API_KEY=sk-ant-old" in lines


async def test_post_secrets_unknown_credential_422(client: AsyncClient, deps: ServerDeps):
    """credential 名不存在 → 422 凭证不存在，.env 不被写入。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    r = await client.post("/api/secrets", json={"credential": "ghost", "api_key": "sk-x"})
    assert r.status_code == 422
    assert "凭证不存在" in r.json()["detail"]
    assert not deps.env_path.exists() or "sk-x" not in deps.env_path.read_text(encoding="utf-8")


# ---------- 422 明文回显防护（密钥铁规） ----------


def _all_strings(obj: object) -> list[str]:
    """递归收集 JSON 结构里的全部字符串（泄漏扫描用）。

    参数：
        obj: object，待递归遍历的 JSON 值
    返回：
        list[str]，返回该测试辅助函数构造或记录的结果
    """
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _all_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _all_strings(v)]
    return []


async def test_post_secrets_422_never_echoes_api_key(client: AsyncClient):
    """422 不得回显明文：剔除 detail[].input，其余字段与数组结构保持不变。

    参数：
        client: AsyncClient，测试客户端
    返回：
        None，执行断言验证目标行为
    """
    secret = "sk-逐字明文-9f27ac"
    r = await client.post(
        "/api/secrets", json={"anthropic_api_key": f"{secret}\nGATE_API_KEY=attacker"}
    )
    assert r.status_code == 422
    assert secret not in r.text  # 逐字断言：响应体任何位置不含明文
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail  # detail 数组结构保持
    for err in detail:
        assert "input" not in err  # 明文载体被剔除
        assert "msg" in err and "loc" in err  # 其余字段保留
    for s in _all_strings(r.json()):
        assert secret not in s


# ---------- GET /api/secrets/status：credentials 数组 ----------


async def test_secrets_status_default_credential(client: AsyncClient):
    """旧配置（无 credentials）：status 含一条合成的 default 凭证，两个 agent 都引用它。

    参数：
        client: AsyncClient，测试客户端
    返回：
        None，执行断言验证目标行为
    """
    r = await client.get("/api/secrets/status")
    assert r.status_code == 200
    body = r.json()
    assert body["gate_key"] is False and body["llm_key"] is False and body["telegram"] is False
    assert body["credentials"] == [
        {
            "name": "default",
            "provider": "anthropic",
            "model": "claude-sonnet-4-5",
            "api_key_env": "ANTHROPIC_API_KEY",
            "key_configured": False,
            "used_by": ["trader", "reviewer"],
        }
    ]


async def test_secrets_status_lists_credentials_without_plaintext(
    client: AsyncClient, deps: ServerDeps
):
    """多凭证：status 列出各凭证的 key 配置状态与被引用 agent，永不回显明文。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    await _put_two_credentials(client)
    assert deps.runtime_settings is not None  # status 数据源为运行时共享实例
    os.environ["LLM_KEY_BACKUP"] = "sk-secret-backup"
    r = await client.get("/api/secrets/status")
    assert r.status_code == 200
    assert "sk-secret-backup" not in r.text  # 无明文
    creds = {c["name"]: c for c in r.json()["credentials"]}
    assert creds["main"] == {
        "name": "main",
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "api_key_env": "LLM_KEY_MAIN",
        "key_configured": False,
        "used_by": ["trader"],
    }
    assert creds["backup"]["key_configured"] is True
    assert creds["backup"]["api_key_env"] == "LLM_KEY_BACKUP"
    assert creds["backup"]["used_by"] == ["reviewer"]


async def test_secrets_status_llm_key_true_with_only_llm_key_prefix(
    client: AsyncClient, deps: ServerDeps
):
    """llm_key = 任一生效凭证的 api_key_env 已配置（多凭证下不限于两个旧键名）。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    await _put_two_credentials(client)
    os.environ["LLM_KEY_BACKUP"] = "sk-only-llm-key"
    r = await client.get("/api/secrets/status")
    assert r.status_code == 200
    body = r.json()
    assert body["llm_key"] is True  # 仅 LLM_KEY_BACKUP 已配置即为 true
    assert "sk-only-llm-key" not in r.text


# ---------- PUT /api/config：agent 分配热键 ----------


async def test_put_config_agent_assignment_triggers_reconfigure(
    client: AsyncClient, deps: ServerDeps
):
    """agents.*.credential 变更：原地写回 runtime 并触发热重建（凭证登记走专用端点）。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    assert deps.runtime_settings is not None
    await _put_two_credentials(client)  # 专用端点登记凭证 + PUT 分配 trader→main / reviewer→backup
    runtime = deps.runtime_settings
    assert [c.name for c in runtime.llm.credentials] == ["default", "main", "backup"]  # 原地生效
    assert runtime.agents.trader.credential == "main"
    assert runtime.agents.reviewer.credential == "backup"

    calls_before = len(deps.reconfigure_calls)
    raw = (await client.get("/api/config")).json()
    raw["agents"]["trader"]["credential"] = "backup"  # 决策 agent 改分配
    r = await client.put("/api/config", json=raw)
    assert r.status_code == 200
    assert r.json()["saved"] is True and r.json()["llm_configured"] is True
    assert len(deps.reconfigure_calls) == calls_before + 1  # 分配变化同样触发热重建
    assert deps.runtime_settings.agents.trader.credential == "backup"


async def test_put_config_agents_unknown_credential_422(client: AsyncClient):
    """agent 引用不存在的凭证：PUT /api/config 映 422，配置落不了盘。

    参数：
        client: AsyncClient，测试客户端
    返回：
        None，执行断言验证目标行为
    """
    r = await client.post("/api/credentials", json={"name": "main", "model": "m1"})
    assert r.status_code == 200  # 经专用端点登记 main（PUT 不再受理 credentials）
    raw = (await client.get("/api/config")).json()
    raw["agents"] = {"trader": {"credential": "main"}, "reviewer": {"credential": "ghost"}}
    r = await client.put("/api/config", json=raw)
    assert r.status_code == 422
    assert "不存在的凭证" in r.json()["detail"]


async def test_put_config_ignores_llm_credentials_snapshot(client: AsyncClient, deps: ServerDeps):
    """PUT /api/config 忽略 llm.credentials，只让专用凭证端点拥有写权。

    参数：
        client: AsyncClient，测试客户端
        deps: ServerDeps，服务端测试依赖
    返回：
        None，执行断言验证目标行为
    """
    r = await client.post("/api/credentials", json={"name": "main", "model": "m1"})
    assert r.status_code == 200  # 磁盘与 runtime 凭证列表现为 [default, main]
    calls_before = len(deps.reconfigure_calls)

    # 仅 credentials 旧快照变化的 PUT：完全无副作用（saved=true，不热重建）
    raw = (await client.get("/api/config")).json()
    raw["llm"]["credentials"] = [{"name": "ghost", "provider": "anthropic", "model": "m9"}]
    r = await client.put("/api/config", json=raw)
    assert r.status_code == 200
    assert r.json() == {"saved": True, "needs_restart": []}
    assert len(deps.reconfigure_calls) == calls_before  # 不触发不必要热重建
    saved = yaml.safe_load(deps.config_path.read_text(encoding="utf-8"))
    assert [c["name"] for c in saved["llm"]["credentials"]] == ["default", "main"]  # 磁盘不变
    assert deps.runtime_settings is not None
    assert [c.name for c in deps.runtime_settings.llm.credentials] == [
        "default",
        "main",
    ]  # runtime 不变

    # 同请求捎带 llm.model：model 正常生效且只热重建一次，credentials 仍不动
    raw = (await client.get("/api/config")).json()
    raw["llm"]["credentials"] = [{"name": "ghost2", "provider": "anthropic", "model": "m9"}]
    raw["llm"]["model"] = "claude-opus-4"
    r = await client.put("/api/config", json=raw)
    assert r.status_code == 200
    assert r.json()["saved"] is True
    assert len(deps.reconfigure_calls) == calls_before + 1
    assert deps.runtime_settings.llm.model == "claude-opus-4"
    saved = yaml.safe_load(deps.config_path.read_text(encoding="utf-8"))
    assert [c["name"] for c in saved["llm"]["credentials"]] == ["default", "main"]
