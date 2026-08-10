"""技术指标端点族行为测试：fake 指标回调束注入（tmp_path 隔离，真实 repo 供版本族）。

覆盖：
- GET /api/indicators：面板 + shortlist 透传；contract/interval 422；未接线 503；
- GET /api/indicators/series：keys 缺省（None）/逗号解析、limit 默认 100、上限 1000 与越界 422、
  未知 key 映 422、未接线 503；
- GET /api/indicator_config：shortlist+available 透传、未接线 503；
- PUT /api/indicator_config：成功 {"ok","version_id"}、校验失败 422 拼接原因、
  body 形状非法 422、未接线 503；
- 版本族：versions 列表不含 content、详情含 content、404；diff 纯文本/404/422；
  rollback 200/404/503。
"""

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from src.memory.db import Database
from src.memory.repo import Repo
from src.review.indicator_config import IndicatorConfigValidationError
from src.server.app import create_app
from src.server.deps import ServerDeps

_PANEL = {
    "contract": "BTC_USDT",
    "interval": "1h",
    "time": None,
    "indicators": {},
    "shortlist": ["ema20", "rsi14"],
}


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    """构造指向临时数据库的 Repo 实例，测试结束后关闭数据库连接。

    参数：
        tmp_path: Path，pytest 临时目录夹具，SQLite 数据库文件落在其中

    返回：
        AsyncIterator[Repo]：异步生成器，yield 已打开临时数据库的仓储对象
    """
    db = Database()
    await db.open(tmp_path / "t.db")
    yield Repo(db)
    await db.close()


def _bundle(**overrides: Any) -> SimpleNamespace:
    """构造默认可用且支持逐项覆盖的指标回调束测试替身。

    参数：
        overrides: Any，按名称覆盖默认指标回调或任务对象

    返回：
        SimpleNamespace，满足 IndicatorBundle 鸭子类型的回调集合
    """

    async def _revise(shortlist: list[str], reason: str) -> dict:
        """模拟成功保存指标短名单并返回固定版本编号。

        参数：
            shortlist: list[str]，请求保存的指标键列表
            reason: str，本次调整原因，本桩不读取其内容

        返回：
            dict，表示保存成功且版本编号为 1 的结果
        """
        return {"ok": True, "version_id": 1}

    async def _rollback(version_id: int) -> dict:
        """模拟成功回滚到指定指标配置版本。

        参数：
            version_id: int，请求回滚到的历史版本编号

        返回：
            dict，包含目标版本编号与新生成版本编号 2
        """
        return {"rolled_back_to": version_id, "version_id": 2}

    defaults: dict[str, Any] = {
        "panel": lambda contract, interval: {**_PANEL, "contract": contract, "interval": interval},
        "series": lambda contract, interval, keys, limit: {
            "contract": contract,
            "interval": interval,
            "series": {},
        },
        "config_get": lambda: {"shortlist": ["ema20"], "available": [{"key": "ema20"}]},
        "config_revise": _revise,
        "config_rollback": _rollback,
        "oi_task": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _deps(repo: Repo, tmp_path: Path, **overrides: Any) -> ServerDeps:
    """组装仅允许 BTC_USDT 且跳过静态托管的服务器测试依赖。

    参数：
        repo: Repo，端点读写版本数据使用的仓储
        tmp_path: Path，pytest 临时目录
        overrides: Any，按名称覆盖默认 ServerDeps 字段

    返回：
        ServerDeps，可注入测试应用的依赖集合
    """
    return ServerDeps(
        repo=repo,
        runtime_watchlist=["BTC_USDT"],
        web_dist=tmp_path / "no_dist",
        **overrides,
    )


def _client_of(deps: ServerDeps) -> AsyncClient:
    """为指定依赖创建直接调用 ASGI 应用的异步 HTTP 客户端。

    参数：
        deps: ServerDeps，待注入应用的服务器依赖

    返回：
        AsyncClient，以 http://test 为基址的进程内测试客户端
    """
    return AsyncClient(transport=ASGITransport(app=create_app(deps)), base_url="http://test")


# ---------- GET /api/indicators ----------


async def test_indicators_panel_ok(repo: Repo, tmp_path: Path):
    """验证指标面板端点把回调结果原样返回。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证成功状态码与完整面板载荷
    """
    async with _client_of(_deps(repo, tmp_path, indicators=_bundle())) as c:
        r = await c.get("/api/indicators?contract=BTC_USDT&interval=1h")
        assert r.status_code == 200
        assert r.json() == _PANEL  # 面板原样透传（shortlist 已由回调束合并）


async def test_indicators_panel_validation(repo: Repo, tmp_path: Path):
    """验证指标面板端点拒绝非法合约和周期并区分未接线状态。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证 422 校验错误和 503 未接线响应
    """
    async with _client_of(_deps(repo, tmp_path, indicators=_bundle())) as c:
        r = await c.get("/api/indicators?contract=DOGE_USDT&interval=1h")
        assert r.status_code == 422 and "watchlist" in r.json()["detail"]
        r = await c.get("/api/indicators?contract=BTC_USDT&interval=7m")
        assert r.status_code == 422 and "周期" in r.json()["detail"]
    async with _client_of(_deps(repo, tmp_path)) as c:  # 未接线：先过校验再 503
        assert (await c.get("/api/indicators?contract=BTC_USDT")).status_code == 503
        assert (await c.get("/api/indicators?contract=DOGE_USDT")).status_code == 422


# ---------- GET /api/indicators/series ----------


async def test_series_args_parsing(repo: Repo, tmp_path: Path):
    """验证指标序列端点正确解析缺省值、逗号键列表和历史数量。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证回调实际收到的参数
    """
    calls: list[tuple] = []

    def _series(contract: str, interval: str, keys: list[str] | None, limit: int) -> dict:
        """记录序列查询参数并返回空序列响应。

        参数：
            contract: str，请求查询的合约
            interval: str，K 线时间周期
            keys: list[str] | None，指标键列表或缺省值
            limit: int，请求的历史点数

        返回：
            dict，带合约和周期的空序列载荷
        """
        calls.append((contract, interval, keys, limit))
        return {"contract": contract, "interval": interval, "series": {}}

    deps = _deps(repo, tmp_path, indicators=_bundle(series=_series))
    async with _client_of(deps) as c:
        r = await c.get("/api/indicators/series?contract=BTC_USDT")
        assert r.status_code == 200
        assert calls[-1] == ("BTC_USDT", "1h", None, 100)  # 缺省：keys=None、limit=100
        await c.get(
            "/api/indicators/series?contract=BTC_USDT&interval=15m&limit=50&keys=ema9,, macd "
        )
        assert calls[-1] == ("BTC_USDT", "15m", ["ema9", "macd"], 50)  # 逗号解析去空白空段
        await c.get("/api/indicators/series?contract=BTC_USDT&keys=")
        assert calls[-1][2] is None  # 全空 keys 视为缺省


async def test_series_validation(repo: Repo, tmp_path: Path):
    """验证指标序列端点的数量、合约、指标键与未接线错误映射。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证合法边界及 422、503 响应
    """
    async with _client_of(_deps(repo, tmp_path, indicators=_bundle())) as c:
        base = "/api/indicators/series?contract=BTC_USDT"
        assert (await c.get(f"{base}&limit=0")).status_code == 422
        assert (await c.get(f"{base}&limit=1001")).status_code == 422
        assert (await c.get(f"{base}&limit=abc")).status_code == 422
        assert (await c.get(f"{base}&limit=700")).status_code == 200  # 前端 15m 图表窗口
        assert (await c.get(f"{base}&limit=1000")).status_code == 200  # 上限与 /candles 对齐
        assert (await c.get("/api/indicators/series?contract=NOPE")).status_code == 422

    def _bad_keys(contract: str, interval: str, keys: list[str] | None, limit: int) -> dict:
        """模拟指标服务拒绝未知指标键。

        参数：
            contract: str，请求合约，本桩不读取其内容
            interval: str，请求周期，本桩不读取其内容
            keys: list[str] | None，请求指标键，本桩不读取其内容
            limit: int，请求历史点数，本桩不读取其内容

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            ValueError: 每次调用均抛出未知指标错误
        """
        raise ValueError("未知指标: 'foo'")

    deps = _deps(repo, tmp_path, indicators=_bundle(series=_bad_keys))
    async with _client_of(deps) as c:
        r = await c.get("/api/indicators/series?contract=BTC_USDT&keys=foo")
        assert r.status_code == 422 and "未知指标" in r.json()["detail"]
    async with _client_of(_deps(repo, tmp_path)) as c:  # 未接线
        assert (await c.get("/api/indicators/series?contract=BTC_USDT")).status_code == 503


# ---------- GET / PUT /api/indicator_config ----------


async def test_indicator_config_get(repo: Repo, tmp_path: Path):
    """验证指标配置读取端点返回短名单和可选项并处理未接线状态。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证成功载荷与 503 响应
    """
    async with _client_of(_deps(repo, tmp_path, indicators=_bundle())) as c:
        r = await c.get("/api/indicator_config")
        assert r.status_code == 200
        assert r.json() == {"shortlist": ["ema20"], "available": [{"key": "ema20"}]}
    async with _client_of(_deps(repo, tmp_path)) as c:
        assert (await c.get("/api/indicator_config")).status_code == 503


async def test_indicator_config_put(repo: Repo, tmp_path: Path):
    """验证指标配置保存端点的参数透传、请求校验与业务错误映射。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证成功、422 与 503 三类响应
    """
    calls: list[tuple] = []

    async def _revise(shortlist: list[str], reason: str) -> dict:
        """记录端点传入的短名单与原因并返回固定成功结果。

        参数：
            shortlist: list[str]，请求保存的指标键列表
            reason: str，本次人工调整原因

        返回：
            dict，版本编号为 7 的保存成功结果
        """
        calls.append((shortlist, reason))
        return {"ok": True, "version_id": 7}

    async with _client_of(_deps(repo, tmp_path, indicators=_bundle(config_revise=_revise))) as c:
        r = await c.put("/api/indicator_config", json={"shortlist": ["ema9"], "reason": "人工调整"})
        assert r.status_code == 200 and r.json() == {"ok": True, "version_id": 7}
        assert calls == [(["ema9"], "人工调整")]  # 参数透传（created_by='human' 由回调束负责）
        assert (
            await c.put("/api/indicator_config", json={"shortlist": ["ema9"]})
        ).status_code == 422
        body = {"shortlist": "ema9", "reason": "x"}
        assert (await c.put("/api/indicator_config", json=body)).status_code == 422

    async def _reject(shortlist: list[str], reason: str) -> dict:
        """模拟指标配置同时违反未知键与空原因两条规则。

        参数：
            shortlist: list[str]，请求保存的指标键列表
            reason: str，本次调整原因

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            IndicatorConfigValidationError: 每次调用均携带两条校验原因抛出
        """
        raise IndicatorConfigValidationError(["未知指标键: foo", "reason 不能为空"])

    async with _client_of(_deps(repo, tmp_path, indicators=_bundle(config_revise=_reject))) as c:
        r = await c.put("/api/indicator_config", json={"shortlist": ["foo"], "reason": " "})
        assert r.status_code == 422
        assert r.json()["detail"] == "未知指标键: foo；reason 不能为空"  # 全部原因拼接
    async with _client_of(_deps(repo, tmp_path)) as c:  # 未接线
        r = await c.put("/api/indicator_config", json={"shortlist": ["ema9"], "reason": "x"})
        assert r.status_code == 503


# ---------- 版本族：versions / diff / rollback ----------


async def test_indicator_config_versions_and_diff(repo: Repo, tmp_path: Path):
    """验证指标配置版本列表、详情与差异端点的字段和错误语义。

    参数：
        repo: Repo，用于预置两条指标配置版本的仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证倒序列表、详情正文、差异文本及 404、422 响应
    """
    v1 = await repo.indicator_config.save_version("shortlist:\n- ema9\n", "md5-1", "human", "初始")
    v2 = await repo.indicator_config.save_version(
        "shortlist:\n- ema9\n- rsi14\n", "md5-2", "human", "调整"
    )
    async with _client_of(_deps(repo, tmp_path)) as c:  # 版本族读端点不依赖回调束
        items = (await c.get("/api/indicator_config/versions")).json()["items"]
        assert [i["id"] for i in items] == [v2.id, v1.id]  # 最新在前
        assert all("content" not in i for i in items)  # 列表不含全文
        detail = (await c.get(f"/api/indicator_config/versions/{v1.id}")).json()
        assert detail["content"] == "shortlist:\n- ema9\n"
        assert (await c.get("/api/indicator_config/versions/999")).status_code == 404

        r = await c.get(f"/api/indicator_config/diff?from={v1.id}&to={v2.id}")
        assert r.status_code == 200
        assert "+- rsi14" in r.text and f"--- v{v1.id}" in r.text
        assert (await c.get(f"/api/indicator_config/diff?from=999&to={v2.id}")).status_code == 404
        assert (await c.get(f"/api/indicator_config/diff?to={v2.id}")).status_code == 422  # 缺 from


async def test_indicator_config_rollback(repo: Repo, tmp_path: Path):
    """验证指标配置回滚端点的成功、版本不存在与未接线响应。

    参数：
        repo: Repo，临时数据库仓储
        tmp_path: Path，pytest 临时目录

    返回：
        None，通过断言验证 200、404 与 503 三类状态
    """

    async def _missing(version_id: int) -> dict:
        """模拟回滚目标指标配置版本不存在。

        参数：
            version_id: int，请求回滚到的版本编号

        返回：
            dict，本函数始终在返回前抛出异常

        异常：
            IndicatorConfigValidationError: 每次调用均携带版本不存在原因抛出
        """
        raise IndicatorConfigValidationError([f"指标配置版本 v{version_id} 不存在，无法回滚"])

    async with _client_of(_deps(repo, tmp_path, indicators=_bundle())) as c:
        r = await c.post("/api/indicator_config/rollback/1")
        assert r.status_code == 200
        assert r.json() == {"rolled_back_to": 1, "version_id": 2}
    deps = _deps(repo, tmp_path, indicators=_bundle(config_rollback=_missing))
    async with _client_of(deps) as c:
        r = await c.post("/api/indicator_config/rollback/9")
        assert r.status_code == 404 and "不存在" in r.json()["detail"]
    async with _client_of(_deps(repo, tmp_path)) as c:  # 未接线
        assert (await c.post("/api/indicator_config/rollback/1")).status_code == 503
