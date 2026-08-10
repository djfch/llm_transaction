"""技术指标端点族：指标面板/序列查询、短名单读取与人工修订、配置版本族。

指标计算与配置写操作全部经 ServerDeps.indicators 回调束注入（server 不 import market
指标实现），None 时诚实 503；版本族读端点（列表/详情/diff）经 deps.repo.indicator_config
直取，写端点（PUT 修订、rollback）走回调——镜像 routes_review 策略版本端点的形状与参数名。
"""

from __future__ import annotations

import difflib
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from src.config import load_watchlist
from src.market.intervals import GATE_CANDLE_INTERVALS
from src.memory.models import IndicatorConfigVersion
from src.review.indicator_config import IndicatorConfigValidationError
from src.server.deps import ServerDeps

# K 线周期白名单（单一数据源：Gate 全周期，与 /candles 同一校验）
_INTERVALS = frozenset(GATE_CANDLE_INTERVALS)


class _ReviseBody(BaseModel):
    """PUT /indicator_config 请求体：新短名单 + 修订原因（语义校验由 store 负责）。"""

    shortlist: list[str]
    reason: str


def _watchlist_contracts(deps: ServerDeps) -> list[str]:
    """当前生效的合约名单：运行时共享名单优先，未接线时读 watchlist.yaml（同 routes_trading）。

    参数：
        deps: ServerDeps，服务器运行依赖
    返回：
        list[str]，当前生效的合约名单：运行时共享名单优先，未接线时读 watchlist.yaml（同 routes_trading）
    """
    if deps.runtime_watchlist is not None:
        return deps.runtime_watchlist
    try:
        return load_watchlist(deps.watchlist_path).contracts
    except ValueError:
        return []  # 名单文件缺失/非法：按空名单处理，随后的合约校验统一 422


def _check_query(deps: ServerDeps, contract: str, interval: str) -> None:
    """contract 须在 watchlist、interval 白名单（与 /candles 同错误风格 422）。

    参数：
        deps: ServerDeps，服务器运行依赖
        contract: str，合约标识
        interval: str，K 线周期
    返回：
        None，contract 须在 watchlist、interval 白名单（与 /candles 同错误风格 422）
    异常：
        HTTPException，合约不在白名单或 K 线周期非法时返回 422
    """
    if contract not in _watchlist_contracts(deps):
        raise HTTPException(status_code=422, detail=f"合约不在 watchlist: {contract}")
    if interval not in _INTERVALS:
        raise HTTPException(status_code=422, detail=f"非法 K 线周期: {interval}")


def _parse_keys(keys: str | None) -> list[str] | None:
    """keys 查询参数解析：逗号分隔、去空白空段；缺省/全空 → None（回调用当前短名单）。

    参数：
        keys: str | None，逗号分隔的指标键查询参数
    返回：
        list[str] | None，keys 查询参数解析：逗号分隔、去空白空段；缺省/全空 → None（回调用当前短名单）
    """
    if keys is None:
        return None
    return [k.strip() for k in keys.split(",") if k.strip()] or None


def _version_item(version: IndicatorConfigVersion) -> dict[str, Any]:
    """版本列表项：不含 content（省流量）；全文走 /indicator_config/versions/{id}。

    参数：
        version: IndicatorConfigVersion，指标配置版本记录
    返回：
        dict[str, Any]，版本列表项：不含 content（省流量）；全文走 /indicator_config/versions/{id}
    """
    return version.model_dump(exclude={"content"})


async def _get_version_or_404(deps: ServerDeps, version_id: int) -> IndicatorConfigVersion:
    """按 ID 读取指标配置版本，不存在时按 404 处理（版本族读端点共用的取数守卫）。

    参数：
        deps: ServerDeps，服务依赖束（经其 repo.indicator_config 仓储取版本）
        version_id: int，指标配置版本 ID

    返回：
        IndicatorConfigVersion：命中的配置版本记录

    异常：
        HTTPException(404)：指定 ID 的版本不存在时抛出
    """
    version = await deps.repo.indicator_config.get_version(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"指标配置版本不存在: {version_id}")
    return version


def create_indicators_router(deps: ServerDeps) -> APIRouter:
    """创建技术指标端点族路由：指标面板/序列查询、短名单读取与修订、配置版本族。

    参数：
        deps: ServerDeps，服务依赖束（指标回调束与配置版本仓储经其注入，
            回调束未接线时各端点诚实 503）

    返回：
        APIRouter：挂载全部 /api 指标相关端点的路由器
    """
    router = APIRouter(prefix="/api")

    @router.get("/indicators")
    async def get_indicators(
        contract: str = Query(...), interval: str = Query("1h")
    ) -> dict[str, Any]:
        """全指标面板（含当前短名单）：回调束未接线 503。

        参数：
            contract: str，合约标识
            interval: str，K 线周期
        返回：
            dict[str, Any]，全指标面板（含当前短名单）：回调束未接线 503
        异常：
            HTTPException，指标服务未接线时返回 503
        """
        _check_query(deps, contract, interval)
        if deps.indicators is None:
            raise HTTPException(status_code=503, detail="指标服务未接线（agent 未装配）")
        return deps.indicators.panel(contract, interval)

    @router.get("/indicators/series")
    async def get_indicator_series(
        contract: str = Query(...),
        interval: str = Query("1h"),
        limit: int = Query(
            100, ge=1, le=1000
        ),  # 上限与 /candles 一致（KlinePanel 15m 窗口 700 根）
        keys: str | None = Query(None),
    ) -> dict[str, Any]:
        """指标逐根序列：keys 缺省=当前短名单；未知 key 422；回调束未接线 503。

        参数：
            contract: str，合约标识
            interval: str，K 线周期
            limit: int，返回的 K 线或指标点数量
            keys: str | None，逗号分隔的指标键查询参数
        返回：
            dict[str, Any]，指标逐根序列：keys 缺省=当前短名单；未知 key 422；回调束未接线 503
        异常：
            HTTPException，指标服务未接线时返回 503，指标键或查询参数非法时返回 422
        """
        _check_query(deps, contract, interval)
        if deps.indicators is None:
            raise HTTPException(status_code=503, detail="指标服务未接线（agent 未装配）")
        try:
            return deps.indicators.series(contract, interval, _parse_keys(keys), limit)
        except ValueError as exc:  # 未知指标 key（装配层按注册表判定）
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/indicator_config")
    async def get_indicator_config() -> dict[str, Any]:
        """当前短名单 + 注册表全集（available）；回调束未接线 503。

        参数：无
        返回：
            dict[str, Any]，当前短名单 + 注册表全集（available）；回调束未接线 503
        异常：
            HTTPException，指标配置服务未接线时返回 503
        """
        if deps.indicators is None:
            raise HTTPException(status_code=503, detail="指标配置未接线（agent 未装配）")
        return deps.indicators.config_get()

    @router.put("/indicator_config")
    async def put_indicator_config(body: _ReviseBody) -> dict[str, Any]:
        """人工修订短名单（created_by='human'）：成功 {"ok","version_id"}；校验失败 422。

        参数：
            body: _ReviseBody，请求体数据
        返回：
            dict[str, Any]，人工修订短名单（created_by='human'）：成功 {"ok","version_id"}；校验失败 422
        异常：
            HTTPException，指标配置服务未接线时返回 503，短名单校验失败时返回 422
        """
        if deps.indicators is None:
            raise HTTPException(status_code=503, detail="指标配置未接线（agent 未装配）")
        try:
            return await deps.indicators.config_revise(body.shortlist, body.reason)
        except IndicatorConfigValidationError as exc:
            raise HTTPException(status_code=422, detail="；".join(exc.reasons)) from exc

    @router.get("/indicator_config/versions")
    async def list_indicator_config_versions() -> dict[str, Any]:
        """配置版本列表（最新在前）：不含 content，省流量。

        参数：无
        返回：
            dict[str, Any]，配置版本列表（最新在前）：不含 content，省流量
        """
        versions = await deps.repo.indicator_config.list_versions()
        return {"items": [_version_item(v) for v in versions]}

    @router.get("/indicator_config/versions/{version_id}")
    async def get_indicator_config_version(version_id: int) -> dict[str, Any]:
        """配置版本详情：含 content 全文。

        参数：
            version_id: int，指标配置版本标识
        返回：
            dict[str, Any]，配置版本详情：含 content 全文
        """
        return (await _get_version_or_404(deps, version_id)).model_dump()

    @router.get("/indicator_config/diff", response_class=PlainTextResponse)
    async def diff_indicator_config_versions(
        from_id: int = Query(alias="from"), to: int = Query()
    ) -> str:
        """两版本配置 unified diff（纯文本）；参数非法 422、版本不存在 404。

        参数：
            from_id: int，差异比较的起始版本标识
            to: int，差异比较的目标版本标识
        返回：
            str，两版本配置 unified diff（纯文本）；参数非法 422、版本不存在 404
        """
        from_version = await _get_version_or_404(deps, from_id)
        to_version = await _get_version_or_404(deps, to)
        return "\n".join(
            difflib.unified_diff(
                from_version.content.splitlines(),
                to_version.content.splitlines(),
                fromfile=f"v{from_id}",
                tofile=f"v{to}",
                lineterm="",
            )
        )

    @router.post("/indicator_config/rollback/{version_id}")
    async def rollback_indicator_config(version_id: int) -> dict[str, Any]:
        """回滚到指定配置版本：回调束未接线 503；版本不存在 404。

        参数：
            version_id: int，指标配置版本标识
        返回：
            dict[str, Any]，回滚到指定配置版本：回调束未接线 503；版本不存在 404
        异常：
            HTTPException，版本管理服务未接线时返回 503，目标版本不存在时返回 404
        """
        if deps.indicators is None:
            raise HTTPException(status_code=503, detail="指标配置版本管理未接线（agent 未装配）")
        try:
            return await deps.indicators.config_rollback(version_id)
        except IndicatorConfigValidationError as exc:  # 回滚的唯一校验失败即版本不存在
            raise HTTPException(status_code=404, detail="；".join(exc.reasons)) from exc

    return router
