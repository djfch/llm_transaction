"""策略书版本管理：校验 → 临时文件 → 原子替换 system_prompt.md → 版本落库。

当前安全不变量：
- 写前校验：strip 后 ≥100 字符、UTF-8 体积 ≤32KB、与当前版本有差异；
  任一不过即拒绝（StrategyValidationError 携带全部原因），原文件不动、不落版本；
- 文件替换走 .tmp 临时文件 + os.replace 原子提交，避免写一半的策略书被决策循环读到；
- 回滚 = 写回历史内容 + 记 created_by='rollback' 新版本（历史版本行不改写）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

from src.memory.models import StrategyVersion
from src.memory.repo import Repo

logger = logging.getLogger(__name__)

_MIN_CHARS = 100  # strip 后最少字符数
_MAX_BYTES = 32 * 1024  # UTF-8 体积上限 32KB


def content_md5(content: str) -> str:
    """策略书原文 md5（与 PromptLoader.body_md5 同一算法，作为版本关联键）。

    参数：
        content: str，待保存的完整文本
    返回：
        str，策略书原文 md5（与 PromptLoader.body_md5 同一算法，作为版本关联键）
    """
    return hashlib.md5(content.encode("utf-8")).hexdigest()


_NO_DIFF_REASON = "与当前策略书无差异"


class StrategyValidationError(Exception):
    """策略书写前校验失败：reasons 携带全部未过项（供工具返回给 LLM 修正重试）。

    no_diff_only：唯一原因是"与当前版本无差异"时为 True（人工重复保存的幂等
    判定走此结构化字段，不做文案子串匹配）。
    """

    def __init__(self, reasons: list[str], *, no_diff_only: bool = False) -> None:
        """初始化校验失败异常：保存全部未过原因，并用中文分号拼成异常消息。

        参数：
            reasons: list[str]，全部未通过校验的原因列表（逐项展示给 LLM 修正重试）
            no_diff_only: bool，唯一原因是否为"与当前版本无差异"（人工重复保存的
                幂等判定依据），省略时默认为 False

        返回：
            None，就地设置实例属性 reasons 与 no_diff_only
        """
        self.reasons = reasons
        self.no_diff_only = no_diff_only
        super().__init__("；".join(reasons))


class StrategyStore:
    """策略书文件与版本表的统一入口（人工修改与复盘改写同走此路径）。

    隐含不变量：`_atomic_write` 与 `save_strategy_version` 之间不得插入 await
    （write→save 同步临界区，保证最新版本==当前文件）；
    提交内容入口一律把 \r\n 归一化为 \n（Windows 编辑器兼容），保证版本行 md5
    与 PromptLoader.body_md5（读回文本）一致，版本↔决策 join 不断裂。
    复盘改写走草稿模式（issue #62/#73）：revise 只落 draft 版本不动文件，
    报告成功后经 apply_version 统一生效；人工 rollback 仍即时生效。
    """

    def __init__(
        self,
        prompt_path: str | Path,
        repo: Repo,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """初始化策略书存储入口：绑定策略书文件路径、版本仓库与变更回调。

        参数：
            prompt_path: str | Path，策略书文件路径（system_prompt.md）
            repo: Repo，持久化仓库（版本表读写经其 review 子仓库）
            on_change: Callable[[], None] | None，策略书变更回调（revise/rollback
                落版本后触发，如广播 WS 事件）；省略时默认为 None，表示不接线

        返回：
            None，就地设置实例属性（保存路径、仓库与回调引用）
        """
        self._path = Path(prompt_path)
        self._repo = repo
        # 策略书变更回调（revise/rollback 落版本后触发，如广播 WS 事件）；未接线为 None
        self._on_change = on_change
        # 生效临界区锁（issue #113 F11）：apply_version/revise_applied 全收锁，
        # 锁内重读最新 applied——旧草稿晚于人工更高版本到达时不覆盖人工内容
        self._apply_lock = asyncio.Lock()

    def _notify_change(self) -> None:
        """变更即通知（前端据此立即重拉策略面板）；未接线时静默跳过。

        参数：无
        返回：
            None，变更即通知（前端据此立即重拉策略面板）；未接线时静默跳过
        """
        if self._on_change is not None:
            self._on_change()

    async def seed_if_empty(self) -> StrategyVersion | None:
        """启动播种：版本表为空且策略书文件存在 → 记 v1（created_by='human'）。

        参数：无
        返回：
            StrategyVersion | None，启动播种：版本表为空且策略书文件存在 → 记 v1（created_by='human'）
        """
        if await self._repo.review.list_strategy_versions():
            return None
        current = self.current()
        if not current:
            return None
        return await self._repo.review.save_strategy_version(
            current, content_md5(current), "human", "初始版本"
        )

    def current(self) -> str:
        """当前策略书原文；文件不存在返回 ''。

        参数：无
        返回：
            str，当前策略书原文；文件不存在返回 ''
        """
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    async def revise(
        self, content: str, reason: str, created_by: str, report_id: int | None = None
    ) -> StrategyVersion:
        """校验通过后落 draft 草稿版本；文件不动，报告成功后经 apply_version 生效。

        参数：
            content: str，待保存的完整文本
            reason: str，清空或变更原因
            created_by: str，版本创建来源
            report_id: int | None，关联报告标识
        返回：
            StrategyVersion，校验通过后落库的 draft 版本（策略书文件未改动，
            issue #62/#73：先记账后生效）；校验失败抛 StrategyValidationError
        """
        content = content.replace("\r\n", "\n")  # 归一化后校验/md5/写盘/落库用同一份内容
        self._validate(content)
        return await self._repo.review.save_strategy_version(
            content,
            content_md5(content),
            created_by,
            reason,
            report_id,
            status="draft",
        )

    async def revise_applied(
        self, content: str, reason: str, created_by: str = "human"
    ) -> StrategyVersion:
        """校验后落库并立即生效（写文件 + applied）——人工/服务端即时修改专用。

        复盘 agent 走 revise（draft）+ 报告成功后 apply_version；本方法是
        监控接口等"人按下按钮即刻生效"场景的合并入口（issue #62/#73 语义分界）。
        落草稿与生效全程在生效锁内完成，避免与轮末草稿生效交错（issue #113 F11）。

        参数：
            content: str，待保存的完整文本
            reason: str，变更原因
            created_by: str，版本创建来源，默认 human

        返回：
            StrategyVersion：已生效（applied）的版本对象

        异常：
            StrategyValidationError：内容校验失败，或新版本已被更高 applied 版本
                取代（锁内防御分支，理论上不可达）时抛出
        """
        async with self._apply_lock:
            version = await self.revise(content, reason, created_by)
            applied = await self._apply_version_locked(version.id)
            if applied is None:  # 锁内落的新版本不可能被取代，防御性检查
                raise StrategyValidationError(
                    [f"策略版本 v{version.id} 生效失败：已被更高版本取代"]
                )
            return applied

    async def apply_version(self, version_id: int) -> StrategyVersion | None:
        """把草稿（或历史）版本原子写入策略书文件并置为 applied——统一生效入口。

        全程在生效锁内；锁内重读最新 applied 版本，存在 id 更大的 applied 版本时
        本版本已被取代——置 discarded 并返回 None，不覆盖人工新内容（issue #113 F11）。

        参数：
            version_id: int，待生效的版本编号

        返回：
            StrategyVersion | None：已生效的版本对象；已被更高 applied 版本取代时
            返回 None（本版本已置 discarded）

        异常：
            StrategyValidationError，目标版本不存在时抛出
        """
        async with self._apply_lock:
            return await self._apply_version_locked(version_id)

    async def _apply_version_locked(self, version_id: int) -> StrategyVersion | None:
        """apply_version 的无锁核心（调用方必须已持有 _apply_lock）。

        参数：
            version_id: int，待生效的版本编号

        返回：
            StrategyVersion | None：已生效版本；被更高 applied 版本取代时返回 None

        异常：
            StrategyValidationError，目标版本不存在时抛出
        """
        version = await self._repo.review.get_strategy_version(version_id)
        if version is None:
            raise StrategyValidationError([f"策略版本 v{version_id} 不存在，无法生效"])
        latest = await self._repo.review.latest_applied_strategy_version()
        if latest is not None and latest.id > version_id:
            await self._repo.review.set_version_status(version_id, "discarded")
            logger.warning(
                "策略版本 v%d 已被更高的 applied 版本 v%d 取代，废弃不生效",
                version_id,
                latest.id,
            )
            return None
        self._atomic_write(version.content)
        await self._repo.review.set_version_status(version_id, "applied")
        self._notify_change()
        return await self._repo.review.get_strategy_version(version_id)

    async def discard_draft(self, version_id: int) -> None:
        """把草稿版本置为 discarded（报告失败/取消时调用，issue #73）。

        参数：
            version_id: int，待废弃的草稿版本编号

        返回：
            None，就地更新数据库状态
        """
        await self._repo.review.set_version_status(version_id, "discarded")

    async def reconcile(self) -> None:
        """启动对账：策略书文件与最新 applied 版本不一致时以数据库为准恢复文件。

        堵"文件已替换、数据库落库失败/进程中断"留下的不一致窗口（issue #62）；
        无 applied 版本或内容一致时不做任何事。同时清理孤儿草稿（issue #100）：
        启动时不存在进行中的复盘轮，残留 draft 必为上轮异常遗留。

        参数：无

        返回：
            None，先废弃全部 draft，再在不一致时恢复文件并触发变更通知；一致时静默
        """
        orphans = await self._repo.review.discard_all_drafts()
        if orphans:
            logger.warning("已废弃 %d 个孤儿策略草稿（上轮未正常收尾）", orphans)
        latest = await self._repo.review.latest_applied_strategy_version()
        if latest is None or latest.md5 == content_md5(self.current()):
            return
        logger.warning("策略书与最新生效版本不一致（v%d），以数据库为准恢复", latest.id)
        self._atomic_write(latest.content)
        self._notify_change()

    async def rollback(self, version_id: int) -> StrategyVersion:
        """回滚到历史版本：写回其内容并记 created_by='rollback' 的新版本。

        全程在生效锁内（与 apply_version/revise_applied 互斥）：读目标→写文件→
        记新版本之间不得被并发生效插队，否则文件与库内最新 applied 版本会错位
        （issue #113 R7）。

        参数：
            version_id: int，目标历史版本标识
        返回：
            StrategyVersion，回滚到历史版本：写回其内容并记 created_by='rollback' 的新版本
        异常：
            StrategyValidationError，目标策略版本不存在时抛出
        """
        async with self._apply_lock:
            version = await self._repo.review.get_strategy_version(version_id)
            if version is None:
                raise StrategyValidationError([f"策略版本 v{version_id} 不存在，无法回滚"])
            content = version.content.replace("\r\n", "\n")  # 历史脏行归一化后写回并重算 md5
            self._atomic_write(content)
            new_version = await self._repo.review.save_strategy_version(
                content, content_md5(content), "rollback", f"回滚到 v{version_id}"
            )
            self._notify_change()
            return new_version

    async def list_versions(self) -> list[StrategyVersion]:
        """列出全部策略书历史版本，最新版本排在最前。

        参数：无

        返回：
            list[StrategyVersion]：全部策略书版本，按版本 id 倒序（最新在前）
        """
        return await self._repo.review.list_strategy_versions()

    async def get_version(self, version_id: int) -> StrategyVersion | None:
        """按版本 id 读取单个策略书历史版本。

        参数：
            version_id: int，策略书版本 id

        返回：
            StrategyVersion | None：对应版本；该 id 不存在时返回 None
        """
        return await self._repo.review.get_strategy_version(version_id)

    def _validate(self, content: str) -> None:
        """写前校验：收集全部未过项一次性抛出（LLM 可逐项修正）。

        参数：
            content: str，待保存的完整文本
        返回：
            None，写前校验：收集全部未过项一次性抛出（LLM 可逐项修正）
        异常：
            StrategyValidationError，策略内容存在任一校验失败项时汇总抛出
        """
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

        参数：
            content: str，待保存的完整文本
        返回：
            None，先写同目录 .tmp 临时文件，再 os.replace 原子替换目标文件
        """
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8", newline="")
        os.replace(tmp, self._path)
