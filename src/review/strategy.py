"""策略书版本管理：校验 → 临时文件 → 原子替换 system_prompt.md → 版本落库。

安全不变量（设计 spec §7.2）：
- 写前校验：strip 后 ≥100 字符、UTF-8 体积 ≤32KB、与当前版本有差异；
  任一不过即拒绝（StrategyValidationError 携带全部原因），原文件不动、不落版本；
- 文件替换走 .tmp 临时文件 + os.replace 原子提交，避免写一半的策略书被决策循环读到；
- 回滚 = 写回历史内容 + 记 created_by='rollback' 新版本（历史版本行不改写）。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from src.memory.models import StrategyVersion
from src.memory.repo import Repo

_MIN_CHARS = 100  # strip 后最少字符数
_MAX_BYTES = 32 * 1024  # UTF-8 体积上限 32KB


def content_md5(content: str) -> str:
    """策略书原文 md5（与 PromptLoader.body_md5 同一算法，作为版本关联键）。"""
    return hashlib.md5(content.encode("utf-8")).hexdigest()


_NO_DIFF_REASON = "与当前策略书无差异"


class StrategyValidationError(Exception):
    """策略书写前校验失败：reasons 携带全部未过项（供工具返回给 LLM 修正重试）。

    no_diff_only：唯一原因是"与当前版本无差异"时为 True（人工重复保存的幂等
    判定走此结构化字段，不做文案子串匹配）。
    """

    def __init__(self, reasons: list[str], *, no_diff_only: bool = False) -> None:
        self.reasons = reasons
        self.no_diff_only = no_diff_only
        super().__init__("；".join(reasons))


class StrategyStore:
    """策略书文件与版本表的统一入口（人工修改与复盘改写同走此路径）。

    隐含不变量：`_atomic_write` 与 `save_strategy_version` 之间不得插入 await
    （write→save 同步临界区，保证最新版本==当前文件）；
    提交内容入口一律把 \r\n 归一化为 \n（Windows 编辑器兼容），保证版本行 md5
    与 PromptLoader.body_md5（读回文本）一致，版本↔决策 join 不断裂。
    """

    def __init__(self, prompt_path: str | Path, repo: Repo) -> None:
        self._path = Path(prompt_path)
        self._repo = repo

    async def seed_if_empty(self) -> StrategyVersion | None:
        """启动播种：版本表为空且策略书文件存在 → 记 v1（created_by='human'）。"""
        if await self._repo.review.list_strategy_versions():
            return None
        current = self.current()
        if not current:
            return None
        return await self._repo.review.save_strategy_version(
            current, content_md5(current), "human", "初始版本"
        )

    def current(self) -> str:
        """当前策略书原文；文件不存在返回 ''。"""
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    async def revise(
        self, content: str, reason: str, created_by: str, report_id: int | None = None
    ) -> StrategyVersion:
        """校验通过后原子替换策略书并落新版本；校验失败抛 StrategyValidationError。"""
        content = content.replace("\r\n", "\n")  # 归一化后校验/md5/写盘/落库用同一份内容
        self._validate(content)
        self._atomic_write(content)
        return await self._repo.review.save_strategy_version(
            content, content_md5(content), created_by, reason, report_id
        )

    async def rollback(self, version_id: int) -> StrategyVersion:
        """回滚到历史版本：写回其内容并记 created_by='rollback' 的新版本。"""
        version = await self._repo.review.get_strategy_version(version_id)
        if version is None:
            raise StrategyValidationError([f"策略版本 v{version_id} 不存在，无法回滚"])
        content = version.content.replace("\r\n", "\n")  # 历史脏行归一化后写回并重算 md5
        self._atomic_write(content)
        return await self._repo.review.save_strategy_version(
            content, content_md5(content), "rollback", f"回滚到 v{version_id}"
        )

    async def list_versions(self) -> list[StrategyVersion]:
        return await self._repo.review.list_strategy_versions()

    async def get_version(self, version_id: int) -> StrategyVersion | None:
        return await self._repo.review.get_strategy_version(version_id)

    def _validate(self, content: str) -> None:
        """写前校验：收集全部未过项一次性抛出（LLM 可逐项修正）。"""
        reasons: list[str] = []
        if len(content.strip()) < _MIN_CHARS:
            reasons.append(
                f"策略书过短：strip 后 {len(content.strip())} 字符，最少 {_MIN_CHARS} 字符"
            )
        size = len(content.encode("utf-8"))
        if size > _MAX_BYTES:
            reasons.append(f"策略书过长：UTF-8 体积 {size} 字节，上限 {_MAX_BYTES} 字节（32KB）")
        if content == self.current():
            reasons.append(_NO_DIFF_REASON)
        if reasons:
            raise StrategyValidationError(reasons, no_diff_only=(reasons == [_NO_DIFF_REASON]))

    def _atomic_write(self, content: str) -> None:
        """先写同目录 .tmp 临时文件，再 os.replace 原子替换目标文件。

        newline="" 关闭写入换行转换，保证落盘字节与计算 md5 的内容逐字节一致；
        os.replace 失败残留的 .tmp 孤儿文件不清理（下次写入自然覆盖）。
        """
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8", newline="")
        os.replace(tmp, self._path)
