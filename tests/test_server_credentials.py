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
    """在每个用例前清理模型密钥环境变量并在结束后恢复原状。

    参数：无

    返回：
        Iterator[None]，生成一次控制权并负责恢复和清除用例期间的环境变量
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
    """构造隔离配置、密钥文件、数据库和热重建记录的服务器依赖。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        AsyncIterator[ServerDeps]，生成测试依赖并在结束后关闭数据库
    """
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)
    db = Database()
    await db.open(tmp_path / "test.db")
    calls: list[dict] = []

    async def fake_reconfigure() -> dict:
        """假的热重建回调：记录一次调用并谎称重建成功。

        参数：无

        返回：
            dict：{"llm_configured": True, "error": ""}，模拟 LLM 热重建成功
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
    """构造直连内存应用的异步 HTTP 测试客户端。

    参数：
        deps: ServerDeps，fake 依赖夹具，应用经 create_app 挂载到 ASGI 传输

    返回：
        AsyncIterator[AsyncClient]，yield 基于 ASGITransport 的客户端，退出时关闭客户端上下文
    """
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _file_credentials(deps: ServerDeps) -> list[dict]:
    """直接从配置文件读取已落盘凭证列表以绕过接口序列化层。

    参数：
        deps: ServerDeps，包含测试配置文件路径的服务器依赖

    返回：
        list[dict]，配置原文中的凭证定义列表；缺失时返回空列表
    """
    raw = yaml.safe_load(deps.config_path.read_text(encoding="utf-8"))
    return raw.get("llm", {}).get("credentials", [])


async def _create(client: AsyncClient, name: str = "main", **fields) -> None:
    """通过凭证端点快捷创建一条记录并立即断言请求成功。

    参数：
        client: AsyncClient，进程内异步测试客户端
        name: str，凭证名称，默认 main
        fields: dict，覆盖或补充 provider、model、api_key 等请求字段

    返回：
        None，副作用为创建凭证并校验 HTTP 200 响应
    """
    body = {"name": name, "model": "claude-sonnet-4-5", **fields}
    r = await client.post("/api/credentials", json=body)
    assert r.status_code == 200, r.text


# ---------- POST /api/credentials ----------


async def test_post_credential_with_key_writes_env_and_reconfigures(
    client: AsyncClient, deps: ServerDeps
):
    """验证带密钥创建凭证会安全写入环境、同步运行时并仅热重建一次。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置、密钥路径和热重建记录的依赖

    返回：
        None，通过断言验证响应契约、无明文泄漏、落盘和运行时同步
    """
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
    """验证不带密钥创建凭证时跳过密钥文件但仍保存配置并热重建。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置、密钥路径和热重建记录的依赖

    返回：
        None，通过断言验证 key_saved(密钥已保存)为假及配置落盘
    """
    r = await client.post("/api/credentials", json={"name": "main", "model": "m1"})
    assert r.status_code == 200
    assert r.json() == {"saved": True, "key_saved": False, "llm_configured": True, "llm_error": ""}
    env_text = deps.env_path.read_text(encoding="utf-8") if deps.env_path.exists() else ""
    assert "LLM_KEY_MAIN" not in env_text
    assert len(deps.reconfigure_calls) == 1


async def test_post_credential_materializes_default_on_first_create(
    client: AsyncClient, deps: ServerDeps
):
    """验证首次新增凭证时先把旧平铺模型配置物化为 default 再追加新项。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置文件和运行时设置的依赖

    返回：
        None，通过断言验证 default 与新增凭证的顺序和密钥映射
    """
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
    """验证新增凭证名称与已有或物化 default 重复时返回 422 且不落盘。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置文件和热重建记录的依赖

    返回：
        None，通过断言验证两类重名响应和无持久化副作用
    """
    await _create(client, "main")
    r = await client.post("/api/credentials", json={"name": "main", "model": "m2"})
    assert r.status_code == 422
    assert "已存在" in r.json()["detail"]
    r = await client.post("/api/credentials", json={"name": "default", "model": "m2"})
    assert r.status_code == 422  # default 已被物化，重名
    assert [c["name"] for c in _file_credentials(deps)] == ["default", "main"]  # 未变


async def test_post_credential_invalid_name_422(client: AsyncClient, deps: ServerDeps):
    """验证凭证名称只允许小写字母、数字和连字符。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置文件的服务器依赖

    返回：
        None，通过断言验证大写、下划线、中文和空名称均返回 422
    """
    for bad in ("Main", "my_cred", "凭证", "", "name with space"):
        r = await client.post("/api/credentials", json={"name": bad, "model": "m1"})
        assert r.status_code == 422, f"应拒绝: {bad!r}"
    assert _file_credentials(deps) == []  # 全部未落盘


async def test_post_credential_rejects_control_chars(client: AsyncClient, deps: ServerDeps):
    """验证新增凭证拒绝密钥中的换行控制字符以阻止环境文件注入。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供隔离密钥文件的服务器依赖

    返回：
        None，通过断言验证 422 响应且未写入伪造环境变量
    """
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
    """递归收集任意 JSON 兼容结构中的全部字符串供密钥泄漏扫描。

    参数：
        obj: object，待扫描的字典、列表、字符串或其他 JSON 值

    返回：
        list[str]，按遍历顺序收集到的全部字符串
    """
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _all_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _all_strings(v)]
    return []


async def test_post_credential_422_never_echoes_api_key(client: AsyncClient):
    """验证请求校验失败的 422 响应不会在任何字符串字段回显明文密钥。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过递归字符串扫描验证明文已剔除且错误结构仍为数组
    """
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
    """验证凭证创建请求拒绝非法令牌数、空模型和缺失兼容接口地址。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置文件的服务器依赖

    返回：
        None，通过断言验证各类字段错误均为 422 且未保存凭证
    """
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


async def test_post_credential_thinking_effort_validation(
    client: AsyncClient,
    deps: ServerDeps,
):
    """验证思考程度字段拒绝非法值并正确归一、保存及回读合法档位。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置文件的服务器依赖

    返回：
        None，通过断言验证非法、空白和合法三类输入
    """
    base: dict = {"name": "main", "model": "m1"}
    for bad in (
        {"thinking_effort": "extreme"},
        {"thinking_effort": " HIGH "},
        {"thinking_effort": "none"},
    ):
        r = await client.post("/api/credentials", json={**base, **bad})
        assert r.status_code == 422, f"应拒绝: {bad}"
    assert _file_credentials(deps) == []  # 全部未落盘
    # 空白与合法档位：空白 strip 后为空串（=跟随模型默认），合法值原样保存
    r = await client.post("/api/credentials", json={**base, "thinking_effort": "  "})
    assert r.status_code == 200
    r = await client.post(
        "/api/credentials",
        json={"name": "deep", "model": "deepseek-v4-pro", "thinking_effort": "high"},
    )
    assert r.status_code == 200
    saved = _file_credentials(deps)
    by_name = {c["name"]: c for c in saved}
    assert by_name["main"]["thinking_effort"] == ""
    assert by_name["deep"]["thinking_effort"] == "high"
    # PUT 共享同一字段模型：同样拦截非法值
    r = await client.put("/api/credentials/deep", json={"model": "m2", "thinking_effort": "maxx"})
    assert r.status_code == 422


async def test_post_credential_blank_whitespace_key_is_unset(client: AsyncClient, deps: ServerDeps):
    """验证纯空白密钥按未填写处理且不会写入环境文件。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供隔离密钥文件的服务器依赖

    返回：
        None，通过断言验证成功响应、未保存标记和无密钥落盘
    """
    r = await client.post(
        "/api/credentials", json={"name": "main", "model": "m1", "api_key": "   "}
    )
    assert r.status_code == 200
    assert r.json()["key_saved"] is False
    env_text = deps.env_path.read_text(encoding="utf-8") if deps.env_path.exists() else ""
    assert "LLM_KEY_MAIN" not in env_text


async def test_post_credential_key_stripped_before_write(client: AsyncClient, deps: ServerDeps):
    """验证非空密钥写入文件和进程环境前会去除首尾空白。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供隔离密钥文件的服务器依赖

    返回：
        None，通过断言验证文件与环境变量仅包含规范化密钥
    """
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
    """验证密钥文件写入失败时返回 500 并诚实保留可观察的半完成状态。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置、运行时与热重建记录的依赖
        monkeypatch: pytest.MonkeyPatch，用于把密钥写入替换为磁盘故障桩

    返回：
        None，通过断言验证配置已落盘但密钥、运行时和热重建未完成且无泄漏
    """

    def _boom(mapping: dict, env_path: Path) -> None:
        """monkeypatch 替换 set_env_keys 的故障实现：模拟写 .env 时磁盘出错。

        参数：
            mapping: dict，待写入的 env 键值映射（不使用）
            env_path: Path，.env 目标路径（不使用）

        返回：
            None，实际不可达（返回前必然抛出 OSError）

        异常：
            OSError：无条件抛出，模拟磁盘满/权限不足导致 key 写 .env 失败
        """
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
    """验证编辑模型与兼容接口地址会落盘、热更新且保持密钥环境键不变。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置、运行时与热重建回调的依赖

    返回：
        None，通过断言验证响应、落盘字段、密钥映射与运行时同步
    """
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


async def test_put_trader_credential_updates_status_summary(client: AsyncClient):
    """编辑决策 Agent 当前凭证后，状态接口立即返回该凭证的完整模型摘要。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过公开凭证与状态接口断言模型、凭证名和思考强度同步生效
    """
    response = await client.put(
        "/api/credentials/default",
        json={
            "provider": "openai_compat",
            "model": "deepseek-v4-pro",
            "max_tokens": 8192,
            "openai_base_url": "https://api.deepseek.example/v1",
            "thinking_effort": "high",
        },
    )
    assert response.status_code == 200

    status = (await client.get("/api/status")).json()
    assert status["llm_credential_name"] == "default"
    assert status["llm_provider"] == "openai_compat"
    assert status["llm_model"] == "deepseek-v4-pro"
    assert status["llm_thinking_effort"] == "high"


async def test_put_unassigned_credential_keeps_trader_status(client: AsyncClient):
    """编辑未分配凭证时，首页继续展示决策 Agent 当前绑定的 default 凭证。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过公开凭证与状态接口断言未分配凭证不会污染决策状态
    """
    await _create(client, "backup", model="backup-v1")
    response = await client.put(
        "/api/credentials/backup",
        json={"provider": "anthropic", "model": "backup-v2", "thinking_effort": "max"},
    )
    assert response.status_code == 200

    status = (await client.get("/api/status")).json()
    assert status["llm_credential_name"] == "default"
    assert status["llm_provider"] == "anthropic"
    assert status["llm_model"] == "claude-sonnet-4-5"
    assert status["llm_thinking_effort"] == ""


async def test_put_credential_blank_key_keeps_env(client: AsyncClient, deps: ServerDeps):
    """验证编辑凭证时留空密钥不会覆盖文件和进程中的原密钥。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供隔离密钥文件的服务器依赖

    返回：
        None，通过断言验证未保存标记及原密钥保留
    """
    await _create(client, "main", api_key="sk-old")
    r = await client.put("/api/credentials/main", json={"model": "claude-opus-4"})
    assert r.status_code == 200
    assert r.json()["key_saved"] is False
    lines = deps.env_path.read_text(encoding="utf-8").splitlines()
    assert "LLM_KEY_MAIN=sk-old" in lines  # 原 key 原样保留
    assert os.environ["LLM_KEY_MAIN"] == "sk-old"


async def test_put_credential_replaces_key(client: AsyncClient, deps: ServerDeps):
    """验证编辑凭证时新密钥原地替换旧值、同步环境且响应不回显明文。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供隔离密钥文件的服务器依赖

    返回：
        None，通过断言验证替换后的文件、环境变量和安全响应
    """
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
    """验证编辑不存在的路径凭证名称时返回 404。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证状态码和不存在提示
    """
    r = await client.put("/api/credentials/ghost", json={"model": "m1"})
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


async def test_put_credential_default_materializes_list(client: AsyncClient, deps: ServerDeps):
    """验证编辑合成的 default 凭证会物化显式列表并保留旧密钥映射。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置与运行时设置的依赖

    返回：
        None，通过断言验证物化列表、字段更新和运行时同步
    """
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
    """验证编辑凭证同样拒绝密钥控制字符并阻止环境文件注入。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供隔离密钥文件的服务器依赖

    返回：
        None，通过断言验证 422 响应和无恶意变量落盘
    """
    await _create(client, "main")
    r = await client.put(
        "/api/credentials/main", json={"model": "m1", "api_key": "sk-x\nLLM_MOCK=1"}
    )
    assert r.status_code == 422
    text = deps.env_path.read_text(encoding="utf-8") if deps.env_path.exists() else ""
    assert "LLM_MOCK" not in text


# ---------- DELETE /api/credentials/{name} ----------


async def test_delete_credential_success(client: AsyncClient, deps: ServerDeps):
    """验证删除未被引用的凭证会落盘、同步运行时并触发一次热重建。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置、运行时与热重建记录的依赖

    返回：
        None，通过断言验证统一响应、剩余凭证和热重建次数
    """
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
    """验证被决策或复盘 agent 引用的凭证禁止删除且不产生配置变更。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供配置与热重建记录的依赖

    返回：
        None，通过断言验证合成与显式多凭证场景均返回 422 且保持原状
    """
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
    """验证删除不存在的凭证名称时返回 404。

    参数：
        client: AsyncClient，进程内异步测试客户端

    返回：
        None，通过断言验证状态码和不存在提示
    """
    r = await client.delete("/api/credentials/ghost")
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


async def test_delete_credential_keeps_env_key(client: AsyncClient, deps: ServerDeps):
    """验证删除凭证定义后环境文件中的历史密钥仍保留。

    参数：
        client: AsyncClient，进程内异步测试客户端
        deps: ServerDeps，提供隔离密钥文件的服务器依赖

    返回：
        None，通过断言验证删除成功且原环境变量行仍存在
    """
    await _create(client, "main", api_key="sk-keep")
    r = await client.delete("/api/credentials/main")
    assert r.status_code == 200
    lines = deps.env_path.read_text(encoding="utf-8").splitlines()
    assert "LLM_KEY_MAIN=sk-keep" in lines  # key 保留
