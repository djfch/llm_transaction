"""复盘草稿通道：策略书/指标短名单/研报提示词三类草稿的统一生效与废弃（issue #62/#73/#113）。

从 review/agent.py 拆出（文件行数门禁）：三类 store 的草稿语义完全一致——
revise 只落 draft 不动文件，报告成功后经 apply_version 统一生效，失败/取消置
discarded；生效前比对最新 applied 版本编号，人工在复盘轮内保存过更高版本时旧草稿
视为已被取代，直接废弃而非覆盖人工内容（issue #100）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.audit.logger import get_logger

if TYPE_CHECKING:
    from src.memory.repo import Repo
    from src.review.tool_handlers import ReviewToolDeps

logger = get_logger(__name__)


async def _channels(
    repo: Repo, deps: ReviewToolDeps
) -> list[tuple[Any, list[int], int | None, str]]:
    """组装本轮生效通道列表：（store、草稿 id 列表、最新 applied 版本 id、通道中文名）。

    未装配的可选通道（indicator_config_store / research_prompt_store 为 None）跳过；
    通道的最新 applied 版本 id 用于"草稿已被人工更高版本取代"判定。

    参数：
        repo: Repo，持久化仓库（读取各类版本的最新 applied 记录）
        deps: ReviewToolDeps，本轮工具依赖（读取 store 与草稿 id 列表）

    返回：
        list[tuple[Any, list[int], int | None, str]]：待处理通道列表
    """
    latest_strategy = await repo.review.latest_applied_strategy_version()
    channels: list[tuple[Any, list[int], int | None, str]] = [
        (
            deps.store,
            deps.strategy_draft_ids,
            latest_strategy.id if latest_strategy is not None else None,
            "策略",
        )
    ]
    if deps.indicator_config_store is not None:
        latest_cfg = await repo.indicator_config.latest_applied_version()
        channels.append(
            (
                deps.indicator_config_store,
                deps.indicator_draft_ids,
                latest_cfg.id if latest_cfg is not None else None,
                "指标配置",
            )
        )
    if deps.research_prompt_store is not None:
        latest_prompt = await repo.research_prompt.latest_applied_version()
        channels.append(
            (
                deps.research_prompt_store,
                deps.research_prompt_draft_ids,
                latest_prompt.id if latest_prompt is not None else None,
                "研报提示词",
            )
        )
    return channels


async def apply_drafts(repo: Repo, deps: ReviewToolDeps) -> None:
    """统一生效本轮草稿：过期拒绝 + 失败收集（issue #100）。

    生效前比对最新 applied 版本编号——人工在复盘轮内保存过更高版本时，
    旧草稿视为已被取代，直接废弃而非覆盖人工内容；单个 apply 失败不中断
    其余草稿，失败 id 记入 deps.apply_failed_ids 供事件与告警暴露。

    参数：
        repo: Repo，持久化仓库
        deps: ReviewToolDeps，本轮工具依赖

    返回：
        None，生效/废弃就地完成；失败 id 就地记入 deps.apply_failed_ids
    """
    for store, draft_ids, latest_id, label in await _channels(repo, deps):
        for draft_id in draft_ids:
            if latest_id is not None and draft_id < latest_id:
                logger.warning(
                    "%s草稿 v%d 已被更高的人工版本 v%d 取代，废弃不生效",
                    label,
                    draft_id,
                    latest_id,
                )
                await store.discard_draft(draft_id)
                continue
            try:
                await store.apply_version(draft_id)
            except Exception:
                deps.apply_failed_ids.append(draft_id)
                logger.exception("%s草稿生效失败（draft_id=%s）", label, draft_id)


async def discard_drafts(deps: ReviewToolDeps) -> None:
    """报告失败/取消时废弃本轮全部草稿版本；文件从未被动过，无需回滚（issue #73）。

    参数：
        deps: ReviewToolDeps，本轮工具依赖（读取其落库的草稿 id 列表）

    返回：
        None，逐个置 discarded；单个失败只记日志不中断其余废弃
    """
    channels: list[tuple[Any, list[int], str]] = [(deps.store, deps.strategy_draft_ids, "策略")]
    if deps.indicator_config_store is not None:
        channels.append((deps.indicator_config_store, deps.indicator_draft_ids, "指标"))
    if deps.research_prompt_store is not None:
        channels.append((deps.research_prompt_store, deps.research_prompt_draft_ids, "研报提示词"))
    for store, draft_ids, label in channels:
        for draft_id in draft_ids:
            try:
                await store.discard_draft(draft_id)
            except Exception:
                logger.exception("%s草稿废弃失败 draft_id=%s", label, draft_id)
