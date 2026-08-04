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

_NO_DIFF_REASON = "与当前指标短名单无差异"


def _serialize(cfg: IndicatorConfig) -> str:
    """短名单序列化为 yaml 文本（版本表 content 与落盘内容同源，保证 md5 一致）。"""
    return yaml.safe_dump(cfg.model_dump(), allow_unicode=True, sort_keys=False)


class IndicatorConfigValidationError(Exception):
    """短名单写前校验失败：reasons 携带全部未过项（供工具返回给 LLM 修正重试）。

    no_diff_only：唯一原因是"与当前配置无差异"时为 True（幂等判定走结构化字段，
    不做文案子串匹配，语义同 StrategyValidationError.no_diff_only）。
    """

    def __init__(self, reasons: list[str], *, no_diff_only: bool = False) -> None:
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
        self._path = Path(path)
        # 接受整体 Repo（取其 indicator_config 子仓库）或子仓库本身（细粒度接线/测试用）
        self._versions = repo.indicator_config if isinstance(repo, Repo) else repo
        self._valid_keys = valid_keys
        # 配置变更回调（revise/rollback 落版本后触发）；未接线为 None
        self._on_change = on_change

    def _notify_change(self) -> None:
        """变更即通知；未接线时静默跳过。"""
        if self._on_change is not None:
            self._on_change()

    def load_current(self) -> IndicatorConfig:
        """当前生效配置；文件不存在返回默认基线（与 load_indicator_config 同语义）。"""
        return load_indicator_config(self._path)

    async def seed_if_empty(self) -> IndicatorConfigVersion | None:
        """启动播种：版本表为空时记 v1（created_by='human'，reason='初始基线'）。

        文件不存在先用默认基线原子写文件；文件已存在则以其原文记 v1（文件不动）。
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
        """校验通过后原子替换配置文件并落新版本；校验失败抛 IndicatorConfigValidationError。"""
        cfg = self._validated(shortlist, reason)
        content = _serialize(cfg)  # yaml.safe_dump 产出纯 LF，无需换行归一化
        self._atomic_write(content)
        version = await self._versions.save_version(
            content, content_md5(content), created_by, reason.strip(), report_id
        )
        self._notify_change()
        return version

    async def rollback(self, version_id: int) -> IndicatorConfigVersion:
        """回滚到历史版本：写回其内容并记 created_by='rollback' 的新版本。"""
        version = await self._versions.get_version(version_id)
        if version is None:
            raise IndicatorConfigValidationError([f"指标配置版本 v{version_id} 不存在，无法回滚"])
        content = version.content.replace("\r\n", "\n")  # 历史脏行归一化后写回并重算 md5
        self._atomic_write(content)
        new_version = await self._versions.save_version(
            content, content_md5(content), "rollback", f"回滚到 v{version_id}"
        )
        self._notify_change()
        return new_version

    async def list_versions(self, limit: int = 50) -> list[IndicatorConfigVersion]:
        return await self._versions.list_versions(limit)

    async def get_version(self, version_id: int) -> IndicatorConfigVersion | None:
        return await self._versions.get_version(version_id)

    def _validated(self, shortlist: list[str], reason: str) -> IndicatorConfig:
        """写前校验：形状走 config 模型（去重/长度/字符集），键语义按 valid_keys 校验。

        收集全部未过项一次性抛出（LLM 可逐项修正）；通过则返回归一化后的模型。
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
        """
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8", newline="")
        os.replace(tmp, self._path)
