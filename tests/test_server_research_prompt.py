"""研报提示词版本端点行为测试（issue #113 C8）：fake 依赖注入（tmp_path 隔离真实配置与 DB）。

覆盖：
- GET /api/research/prompt：文件存在返回原文（PlainText），不存在返回空串；
- PUT /api/research/prompt：接线走 research_prompt_save（原文回显、校验失败 422、
  无差异幂等仍 200），未接线直写提示词文件（与 /api/strategy 未接线口径一致）；
- GET /api/research/prompt/versions(+{id})：最新在前、列表不含 content、详情含全文、404；
- GET /api/research/prompt/diff：unified diff 文本、版本不存在 404、参数非法 422；
- POST /api/research/prompt/rollback/{id}：成功 200、版本不存在 404、未接线 503；
- ResearchComponents 写回调接真实 ResearchPromptStore 的全链路（保存生效/幂等/422/回滚）。
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from src.config_io import write_settings
from src.memory.db import Database
from src.memory.repo import Repo
from src.research.prompt_store import ResearchPromptStore, ResearchPromptValidationError
from src.research.setup import ResearchComponents
from src.server.app import create_app
from src.server.deps import ServerDeps


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    """提供指向临时数据库的 Repo 实例，用毕自动关闭连接。

    参数：
        tmp_path: Path，pytest 临时目录夹具，数据库文件落在其中

    返回：
        AsyncIterator[Repo]：已打开临时数据库的仓储对象
    """
    db = Database()
    await db.open(tmp_path / "t.db")
    yield Repo(db)
    await db.close()


def _deps(repo: Repo, tmp_path: Path, **overrides: Any) -> ServerDeps:
    """组装 fake 依赖：tmp 配置 + tmp 研报提示词路径 + 指定回调覆盖。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 提供的临时目录
        **overrides: Any，覆盖默认依赖的键值

    返回：
        ServerDeps，使用临时配置与临时研报提示词路径并合并指定回调覆盖的服务端依赖
    """
    config_path = tmp_path / "config.yaml"
    write_settings({}, config_path)  # 默认配置（mode=paper）
    return ServerDeps(
        repo=repo,
        config_path=config_path,
        prompt_path=tmp_path / "system_prompt.md",
        research_prompt_path=tmp_path / "research_prompt.md",
        web_dist=tmp_path / "no_dist",
        **overrides,
    )


def _client_of(deps: ServerDeps) -> AsyncClient:
    """构造挂在 fake 应用上的异步 HTTP 测试客户端。

    参数：
        deps: ServerDeps，由 _deps 组装的服务端依赖（fake 仓储与配置）

    返回：
        AsyncClient：以 ASGI 传输直连 create_app 应用的 httpx 客户端
    """
    return AsyncClient(transport=ASGITransport(app=create_app(deps)), base_url="http://test")


# ---------- GET /api/research/prompt ----------


async def test_get_research_prompt_reads_file(repo: Repo, tmp_path: Path):
    """提示词读取：文件不存在返回空串，存在返回原文且为 text/plain。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证空态与原文两种行为
    """
    async with _client_of(_deps(repo, tmp_path)) as c:
        r = await c.get("/api/research/prompt")
        assert r.status_code == 200 and r.text == ""

    (tmp_path / "research_prompt.md").write_text("研报提示词原文", encoding="utf-8")
    async with _client_of(_deps(repo, tmp_path)) as c:
        r = await c.get("/api/research/prompt")
        assert r.status_code == 200 and r.text == "研报提示词原文"
        assert r.headers["content-type"].startswith("text/plain")


# ---------- PUT /api/research/prompt ----------


async def test_put_research_prompt_via_save_callback(repo: Repo, tmp_path: Path):
    """接线后保存回调收到全文且响应原样回显（PlainText 契约与 /api/strategy 一致）。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证回调透传与原文回显
    """
    saved: list[str] = []

    async def _save(content: str) -> dict:
        """记录待保存提示词全文并返回固定版本编号。

        参数：
            content: str，端点收到的研报提示词全文

        返回：
            dict，表示保存成功且版本编号为 5
        """
        saved.append(content)
        return {"saved": True, "version": 5}

    async with _client_of(_deps(repo, tmp_path, research_prompt_save=_save)) as c:
        body = "新研报提示词全文。" * 30
        r = await c.put("/api/research/prompt", content=body)
        assert r.status_code == 200 and r.text == body
        assert saved == [body]


async def test_put_research_prompt_validation_error_maps_422(repo: Repo, tmp_path: Path):
    """校验失败被映射为含完整原因的 422 响应，且不写提示词文件。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证状态码、原因文案与文件未落地
    """

    async def _reject(content: str) -> dict:
        """模拟提示词正文过短的校验失败。

        参数：
            content: str，待保存提示词正文，本桩不读取其内容

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            ResearchPromptValidationError：每次调用均携带提示词过短原因抛出
        """
        raise ResearchPromptValidationError(["研报提示词过短：strip 后 2 字符，最少 100 字符"])

    async with _client_of(_deps(repo, tmp_path, research_prompt_save=_reject)) as c:
        r = await c.put("/api/research/prompt", content="太短")
        assert r.status_code == 422
        assert "研报提示词过短" in r.json()["detail"]
    assert not (tmp_path / "research_prompt.md").exists()  # 校验失败不写文件


async def test_put_research_prompt_no_diff_idempotent(repo: Repo, tmp_path: Path):
    """内容与当前版本无差异时（版本号为 None）仍按幂等成功返回原文。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证幂等保存的成功状态与纯文本响应
    """

    async def _no_diff(content: str) -> dict:
        """模拟提示词与当前版本无差异的幂等结果。

        参数：
            content: str，待保存提示词正文，本桩不读取其内容

        返回：
            dict，表示保存成功但没有生成新版本
        """
        return {"saved": True, "version": None}

    async with _client_of(_deps(repo, tmp_path, research_prompt_save=_no_diff)) as c:
        body = "与当前一致的研报提示词。" * 20
        r = await c.put("/api/research/prompt", content=body)
        assert r.status_code == 200 and r.text == body


async def test_put_research_prompt_unwired_writes_file(repo: Repo, tmp_path: Path):
    """未接线（fake deps）时 PUT 直接写入提示词文件并回显原文。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证直写文件行为
    """
    async with _client_of(_deps(repo, tmp_path)) as c:
        body = "未接线直写的研报提示词。" * 30
        r = await c.put("/api/research/prompt", content=body)
        assert r.status_code == 200 and r.text == body
    assert (tmp_path / "research_prompt.md").read_text(encoding="utf-8") == body


# ---------- GET /api/research/prompt/versions(+{id}) 与 diff ----------


async def test_research_prompt_versions_list_detail_and_diff(repo: Repo, tmp_path: Path):
    """验证版本列表、详情和差异端点的字段、状态、文本与错误响应。

    参数：
        repo: Repo，用于预置两个研报提示词版本的仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证倒序列表、status 字段、详情全文、差异文本及 404、422
    """
    v1 = await repo.research_prompt.save_version("提示词 v1：保守。", "md5-v1", "human", "初始版本")
    v2 = await repo.research_prompt.save_version(
        "提示词 v2：进取。", "md5-v2", "review_agent", "复盘草稿", status="draft"
    )
    async with _client_of(_deps(repo, tmp_path)) as c:
        items = (await c.get("/api/research/prompt/versions")).json()["items"]
        assert [i["id"] for i in items] == [v2.id, v1.id]  # 最新在前
        assert all("content" not in i for i in items)  # 列表不含全文
        assert items[0]["status"] == "draft" and items[1]["status"] == "applied"
        detail = (await c.get(f"/api/research/prompt/versions/{v1.id}")).json()
        assert detail["content"] == "提示词 v1：保守。"
        r404 = await c.get("/api/research/prompt/versions/999")
        assert r404.status_code == 404
        assert "研报提示词版本不存在" in r404.json()["detail"]

        r = await c.get(f"/api/research/prompt/diff?from={v1.id}&to={v2.id}")
        assert r.status_code == 200
        assert "--- v1" in r.text and "+++ v2" in r.text
        assert "-提示词 v1：保守。" in r.text and "+提示词 v2：进取。" in r.text
        assert (await c.get(f"/api/research/prompt/diff?from=999&to={v2.id}")).status_code == 404
        assert (await c.get(f"/api/research/prompt/diff?from=abc&to={v2.id}")).status_code == 422
        assert (await c.get(f"/api/research/prompt/diff?to={v2.id}")).status_code == 422  # 缺 from


# ---------- POST /api/research/prompt/rollback/{id} ----------


async def test_research_prompt_rollback_status_mapping(repo: Repo, tmp_path: Path):
    """验证提示词回滚端点的成功、版本不存在和未接线状态映射。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证 200、404 与 503 三类响应
    """

    async def _ok(version_id: int) -> dict:
        """模拟成功回滚到指定研报提示词版本。

        参数：
            version_id: int，请求回滚到的历史版本编号

        返回：
            dict，包含目标版本与固定新版本编号 3
        """
        return {"rolled_back_to": version_id, "version": 3}

    async def _missing(version_id: int) -> dict:
        """模拟目标研报提示词版本不存在。

        参数：
            version_id: int，请求回滚到的版本编号

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            ResearchPromptValidationError：每次调用均携带版本不存在原因抛出
        """
        raise ResearchPromptValidationError([f"研报提示词版本 v{version_id} 不存在，无法回滚"])

    async with _client_of(_deps(repo, tmp_path, research_prompt_rollback=_ok)) as c:
        r = await c.post("/api/research/prompt/rollback/1")
        assert r.status_code == 200
        assert r.json() == {"rolled_back_to": 1, "version": 3}
    async with _client_of(_deps(repo, tmp_path, research_prompt_rollback=_missing)) as c:
        r = await c.post("/api/research/prompt/rollback/9")
        assert r.status_code == 404
        assert "无法回滚" in r.json()["detail"]
    async with _client_of(_deps(repo, tmp_path)) as c:  # 未接线
        r = await c.post("/api/research/prompt/rollback/1")
        assert r.status_code == 503
        assert "未接线" in r.json()["detail"]


# ---------- ResearchComponents 写回调接真实 ResearchPromptStore 全链路 ----------


async def test_research_prompt_callbacks_full_chain_via_store(repo: Repo, tmp_path: Path):
    """端点 → ResearchComponents 写回调 → 真实 ResearchPromptStore 的全链路行为。

    覆盖：播种 v1；PUT 保存即生效（文件原子替换 + applied 版本落库）；
    同内容重复保存幂等（不产新版本仍 200）；过短 422；回滚生成 rollback
    新版本并恢复文件内容。

    参数：
        repo: Repo，连接测试数据库的仓储实例
        tmp_path: Path，pytest 临时目录（隔离提示词文件）

    返回：
        None，通过断言验证上述全链路行为
    """
    path = tmp_path / "research_prompt.md"
    initial = "初始研报提示词。" * 30
    path.write_text(initial, encoding="utf-8")
    store = ResearchPromptStore(path, repo)
    seeded = await store.seed_if_empty()
    assert seeded is not None
    # 本用例只演练提示词写回调，agent/聚合器/调度器不参与（运行时不触碰）
    components = ResearchComponents(
        agent=None,  # type: ignore[arg-type]
        data_provider=None,  # type: ignore[arg-type]
        scheduler=None,  # type: ignore[arg-type]
        prompt_store=store,
    )
    deps = _deps(
        repo,
        tmp_path,
        research_prompt_save=components.research_prompt_save,
        research_prompt_rollback=components.research_prompt_rollback,
    )
    async with _client_of(deps) as c:
        new_content = "改进后的研报提示词。" * 30
        r = await c.put("/api/research/prompt", content=new_content)
        assert r.status_code == 200 and r.text == new_content
        assert path.read_text(encoding="utf-8") == new_content  # 保存即生效
        versions = await repo.research_prompt.list_versions()
        assert len(versions) == 2
        assert versions[0].status == "applied"
        assert versions[0].created_by == "human" and versions[0].reason == "前端手动保存"

        r = await c.put("/api/research/prompt", content=new_content)  # 幂等：无差异不产新版本
        assert r.status_code == 200
        assert len(await repo.research_prompt.list_versions()) == 2

        r = await c.put("/api/research/prompt", content="太短")
        assert r.status_code == 422

        r = await c.post(f"/api/research/prompt/rollback/{seeded.id}")
        assert r.status_code == 200
        assert r.json()["rolled_back_to"] == seeded.id
        assert path.read_text(encoding="utf-8") == initial
        versions = await repo.research_prompt.list_versions()
        assert len(versions) == 3
        assert versions[0].created_by == "rollback" and versions[0].status == "applied"


async def test_research_prompt_save_requires_store(repo: Repo, tmp_path: Path):
    """组件束未装配 prompt_store 时写回调抛 RuntimeError（防御分支，生产由 bootstrap 恒装配）。

    参数：
        repo: Repo，连接测试数据库的仓储实例（本用例仅用于满足组件束构造语境）
        tmp_path: Path，pytest 临时目录（本用例不使用，保持签名一致）

    返回：
        None，通过断言验证 RuntimeError 抛出
    """
    components = ResearchComponents(
        agent=None,  # type: ignore[arg-type]
        data_provider=None,  # type: ignore[arg-type]
        scheduler=None,  # type: ignore[arg-type]
        prompt_store=None,
    )
    with pytest.raises(RuntimeError, match="研报提示词版本存储未装配"):
        await components.research_prompt_save("任意内容")
    with pytest.raises(RuntimeError, match="研报提示词版本存储未装配"):
        await components.research_prompt_rollback(1)
