"""指标短名单版本管理：校验 → 临时文件 → 原子替换 indicator_config.yaml → 版本落库。

镜像 src/review/strategy.py StrategyStore 的行为与不变量：
- 写前校验：键必须在注入的 valid_keys 内、去重后 1~8 个、reason strip 后非空、
  与当前配置有差异；任一不过即拒绝（IndicatorConfigValidationError 携带全部原因），
  原文件不动、不落版本；
- 文件替换走 .tmp 临时文件 + os.replace 原子提交，write→save 之间不插 await
  （同步临界区，保证最新版本==当前文件）；
- 回滚 = 写回历史内容 + 记 created_by='rollback' 新版本（历史版本行不改写）；
- 落盘内容与版本行 content 同源（同一份 yaml 文本），md5 算法同 content_md5。

解耦约束：本模块不 import src/market、src/agent——指标注册表（键的语义有效性）
以 valid_keys 形式由外部注入，避免层间依赖。created_by 取值与策略版本一致：
human（人工修改/初始播种）/ review_agent（复盘 agent 改写）/ rollback（回滚）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import ValidationError

from src.config import DEFAULT_INDICATOR_SHORTLIST, IndicatorConfig, load_indicator_config
from src.memory.indicator_config_repo import IndicatorConfigRepo
from src.memory.models import IndicatorConfigVersion
from src.memory.repo import Repo
from src.review.strategy import content_md5

logger = logging.getLogger(__name__)

_NO_DIFF_REASON = "与当前指标短名单无差异"


def _serialize(cfg: IndicatorConfig) -> str:
    """把指标短名单模型序列化为版本表与运行时文件共用的 YAML 文本。

    参数：
        cfg: IndicatorConfig，已校验的指标短名单配置模型

    返回：
        str，保留中文且不排序字段的 YAML 配置文本
    """
    return yaml.safe_dump(cfg.model_dump(), allow_unicode=True, sort_keys=False)


class IndicatorConfigValidationError(Exception):
    """短名单写前校验失败：reasons 携带全部未过项（供工具返回给 LLM 修正重试）。

    no_diff_only：唯一原因是"与当前配置无差异"时为 True（幂等判定走结构化字段，
    不做文案子串匹配，语义同 StrategyValidationError.no_diff_only）。
    """

    def __init__(self, reasons: list[str], *, no_diff_only: bool = False) -> None:
        """收集全部校验未过项并组装异常消息。

        参数：
            reasons: list[str]，全部未通过项的描述列表，按序拼接为异常消息
            no_diff_only: bool，唯一原因是否为"与当前配置无差异"；省略时视为 False

        返回：
            None，就地初始化实例（挂载 reasons 与 no_diff_only 属性）
        """
        self.reasons = reasons
        self.no_diff_only = no_diff_only
        super().__init__("；".join(reasons))


class IndicatorConfigStore:
    """指标短名单文件与版本表的统一入口（人工修改与复盘改写同走此路径）。"""

    def __init__(
        self,
        path: str | Path,
        repo: Repo | IndicatorConfigRepo,
        valid_keys: frozenset[str],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """初始化短名单存储入口，接线配置文件、版本仓库与变更回调。

        参数：
            path: str | Path，指标配置文件（indicator_config.yaml）路径
            repo: Repo | IndicatorConfigRepo，整体仓库（自动取其 indicator_config 子仓库）
                或子仓库本身（细粒度接线/测试用）
            valid_keys: frozenset[str]，允许写入的指标键集合，由外部注入以解耦指标注册表
            on_change: Callable[[], None] | None，配置变更回调（revise/rollback 落版本后
                触发）；省略或未接线时不通知

        返回：
            None，就地初始化实例
        """
        self._path = Path(path)
        # 接受整体 Repo（取其 indicator_config 子仓库）或子仓库本身（细粒度接线/测试用）
        self._versions = repo.indicator_config if isinstance(repo, Repo) else repo
        self._valid_keys = valid_keys
        # 配置变更回调（revise/rollback 落版本后触发）；未接线为 None
        self._on_change = on_change
        # 生效临界区锁（issue #113 F11）：apply_version 全收锁，锁内重读最新
        # applied——旧草稿晚于人工更高版本到达时不覆盖人工内容（语义同 StrategyStore）
        self._apply_lock = asyncio.Lock()

    def _notify_change(self) -> None:
        """在配置变更后调用已接线回调，未配置回调时静默跳过。

        参数：无

        返回：
            None，存在回调时同步触发配置更新通知
        """
        if self._on_change is not None:
            self._on_change()

    def load_current(self) -> IndicatorConfig:
        """读取当前生效指标短名单，运行时文件不存在时使用默认基线。

        参数：无

        返回：
            IndicatorConfig，当前文件内容或默认基线构造的配置模型
        """
        return load_indicator_config(self._path)

    async def seed_if_empty(self) -> IndicatorConfigVersion | None:
        """启动播种：版本表为空时记 v1（created_by='human'，reason='初始基线'）。

        文件不存在先用默认基线原子写文件；文件已存在则以其原文记 v1（文件不动）。

        参数：无

        返回：
            IndicatorConfigVersion | None，新建的初始版本；版本表已有记录时返回 None
        """
        if await self._versions.list_versions(limit=1):
            return None
        if not self._path.exists():
            content = _serialize(IndicatorConfig(shortlist=list(DEFAULT_INDICATOR_SHORTLIST)))
            self._atomic_write(content)
        else:
            content = self._path.read_text(encoding="utf-8")
        return await self._versions.save_version(content, content_md5(content), "human", "初始基线")

    async def revise(
        self,
        shortlist: list[str],
        created_by: str,
        reason: str,
        report_id: int | None = None,
    ) -> IndicatorConfigVersion:
        """校验通过后落 draft 草稿版本；文件不动，报告成功后经 apply_version 生效。

        参数：
            shortlist: list[str]，期望启用的指标键列表
            created_by: str，本次修订的创建者分类
            reason: str，本次修订原因
            report_id: int | None，触发修订的复盘报告编号

        返回：
            IndicatorConfigVersion，已落库的 draft 版本（短名单文件未改动，
            issue #62/#73：先记账后生效）
        """
        cfg = self._validated(shortlist, reason)
        content = _serialize(cfg)  # yaml.safe_dump 产出纯 LF，无需换行归一化
        return await self._versions.save_version(
            content, content_md5(content), created_by, reason.strip(), report_id, status="draft"
        )

    async def apply_version(self, version_id: int) -> IndicatorConfigVersion | None:
        """把草稿（或历史）版本原子写入短名单文件并置为 applied——统一生效入口。

        全程在生效锁内；锁内重读最新 applied 版本，存在 id 更大的 applied 版本时
        本版本已被取代——置 discarded 并返回 None，不覆盖人工新内容（issue #113 F11）。

        参数：
            version_id: int，待生效的版本编号

        返回：
            IndicatorConfigVersion | None：已生效（applied）的版本对象；
            已被更高 applied 版本取代时返回 None（本版本已置 discarded）

        异常：
            IndicatorConfigValidationError，目标版本不存在时抛出
        """
        async with self._apply_lock:
            return await self._apply_version_locked(version_id)

    async def _apply_version_locked(self, version_id: int) -> IndicatorConfigVersion | None:
        """apply_version 的无锁核心（调用方必须已持有 _apply_lock）。

        参数：
            version_id: int，待生效的版本编号

        返回：
            IndicatorConfigVersion | None：已生效版本；被更高 applied 版本取代时返回 None

        异常：
            IndicatorConfigValidationError，目标版本不存在时抛出
        """
        version = await self._versions.get_version(version_id)
        if version is None:
            raise IndicatorConfigValidationError([f"指标配置版本 v{version_id} 不存在，无法生效"])
        latest = await self._versions.latest_applied_version()
        if latest is not None and latest.id > version_id:
            await self._versions.set_version_status(version_id, "discarded")
            logger.warning(
                "指标配置版本 v%d 已被更高的 applied 版本 v%d 取代，废弃不生效",
                version_id,
                latest.id,
            )
            return None
        self._atomic_write(version.content)
        await self._versions.set_version_status(version_id, "applied")
        self._notify_change()
        return await self._versions.get_version(version_id)

    async def discard_draft(self, version_id: int) -> None:
        """把草稿版本置为 discarded（报告失败/取消时调用，issue #73）。

        参数：
            version_id: int，待废弃的草稿版本编号

        返回：
            None，就地更新数据库状态
        """
        await self._versions.set_version_status(version_id, "discarded")

    async def reconcile(self) -> None:
        """启动对账：短名单文件与最新 applied 版本不一致时以数据库为准恢复文件。

        堵"文件已替换、数据库落库失败/进程中断"留下的不一致窗口（issue #62）；
        无 applied 版本或内容一致时不做任何事。同时清理孤儿草稿（issue #100）。

        参数：无

        返回：
            None，先废弃全部 draft，再在不一致时恢复文件并触发变更通知；一致时静默
        """
        orphans = await self._versions.discard_all_drafts()
        if orphans:
            logger.warning("已废弃 %d 个孤儿指标配置草稿（上轮未正常收尾）", orphans)
        latest = await self._versions.latest_applied_version()
        try:
            current = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        if latest is None or latest.md5 == content_md5(current):
            return
        logger.warning("指标短名单与最新生效版本不一致（v%d），以数据库为准恢复", latest.id)
        self._atomic_write(latest.content)
        self._notify_change()

    async def rollback(self, version_id: int) -> IndicatorConfigVersion:
        """把历史指标短名单内容写回运行时文件并创建一条新的回滚版本。

        全程在生效锁内（与 apply_version 互斥）：读目标→写文件→记新版本之间
        不得被并发生效插队，否则文件与库内最新 applied 版本会错位（issue #113 R7）。

        参数：
            version_id: int，作为回滚来源的历史版本编号

        返回：
            IndicatorConfigVersion，回滚操作创建的新版本

        异常：
            IndicatorConfigValidationError: 指定历史版本不存在时抛出
        """
        async with self._apply_lock:
            version = await self._versions.get_version(version_id)
            if version is None:
                raise IndicatorConfigValidationError(
                    [f"指标配置版本 v{version_id} 不存在，无法回滚"]
                )
            content = version.content.replace("\r\n", "\n")  # 历史脏行归一化后写回并重算 md5
            self._atomic_write(content)
            new_version = await self._versions.save_version(
                content, content_md5(content), "rollback", f"回滚到 v{version_id}"
            )
            self._notify_change()
            return new_version

    async def list_versions(self, limit: int = 50) -> list[IndicatorConfigVersion]:
        """读取短名单历史版本列表（最新在前）。

        参数：
            limit: int，最多返回的条数；省略时取 50，仓库侧钳制到 1..200

        返回：
            list[IndicatorConfigVersion]：版本列表，按 id 倒序排列（最新版本在前）
        """
        return await self._versions.list_versions(limit)

    async def get_version(self, version_id: int) -> IndicatorConfigVersion | None:
        """按版本 id 读取单个短名单历史版本。

        参数：
            version_id: int，版本行 id

        返回：
            IndicatorConfigVersion | None：对应版本行；id 不存在时返回 None
        """
        return await self._versions.get_version(version_id)

    def _validated(self, shortlist: list[str], reason: str) -> IndicatorConfig:
        """写前校验：形状走 config 模型（去重/长度/字符集），键语义按 valid_keys 校验。

        收集全部未过项一次性抛出（LLM 可逐项修正）；通过则返回归一化后的模型。

        参数：
            shortlist: list[str]，待校验的指标键列表
            reason: str，本次修订原因

        返回：
            IndicatorConfig，去重并完成形状与键语义校验的配置模型

        异常：
            IndicatorConfigValidationError: 列表形状、指标键、差异或原因任一校验失败时抛出
        """
        reasons: list[str] = []
        cfg: IndicatorConfig | None = None
        try:
            cfg = IndicatorConfig(shortlist=list(shortlist))
        except ValidationError as exc:
            reasons.extend(e["msg"].removeprefix("Value error, ") for e in exc.errors())
        else:
            unknown = [k for k in cfg.shortlist if k not in self._valid_keys]
            if unknown:
                reasons.append(f"未知指标键: {', '.join(unknown)}")
            if cfg.shortlist == self.load_current().shortlist:
                reasons.append(_NO_DIFF_REASON)
        if not reason.strip():
            reasons.append("reason 不能为空")
        if reasons:
            raise IndicatorConfigValidationError(
                reasons, no_diff_only=(reasons == [_NO_DIFF_REASON])
            )
        assert cfg is not None  # 无拒绝原因时模型必然已构建成功
        return cfg

    def _atomic_write(self, content: str) -> None:
        """先写同目录 .tmp 临时文件，再 os.replace 原子替换目标文件（同 StrategyStore）。

        newline="" 关闭写入换行转换，保证落盘字节与计算 md5 的内容逐字节一致。

        参数：
            content: str，待写入运行时指标配置文件的完整 YAML 文本

        返回：
            None，先写同目录临时文件再原子替换目标文件
        """
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8", newline="")
        os.replace(tmp, self._path)
