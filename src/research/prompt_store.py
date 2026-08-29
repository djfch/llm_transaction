"""研报提示词版本管理：校验 → 临时文件 → 原子替换 research_prompt.md → 版本落库（issue #113）。

与 src/review/strategy.py 同模式但独立实现（本包不 import src/review/*，允许少量
重复）；状态机对齐 strategy_versions：复盘 agent 改写只落 draft 草稿，报告成功后
经 apply_version 统一生效，失败/取消置 discarded；人工保存与回滚即时生效。

当前安全不变量：
- 写前校验：strip 后 ≥100 字符、UTF-8 体积 ≤32KB、与当前版本有差异；
  任一不过即拒绝（ResearchPromptValidationError 携带全部原因），原文件不动、不落版本；
- 文件替换走 .tmp 临时文件 + os.replace 原子提交，避免写一半的提示词被研报循环读到；
- 回滚 = 写回历史内容 + 记 created_by='rollback' 新版本（历史版本行不改写）；
  只允许回滚到 status 为 applied 的版本（草稿/已废弃版本从未走生效路径，不得直接提升）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

from src.memory.models import ResearchPromptVersion
from src.memory.repo import Repo

logger = logging.getLogger(__name__)

_MIN_CHARS = 100  # strip 后最少字符数
_MAX_BYTES = 32 * 1024  # UTF-8 体积上限 32KB

_NO_DIFF_REASON = "与当前研报提示词无差异"


def _staleness_reason(
    version: ResearchPromptVersion, latest: ResearchPromptVersion | None, current_md5: str
) -> str | None:
    """判定草稿基线是否失效（issue #113 CAS，R6-3/R7-1 加固）；失效返回原因文本，否则 None。

    判定语义同 src/review/strategy.py 的同名函数：latest 即本版本自身（轮末生效
    成功后被打断的幂等重放）→ 有效；无基线章（历史行/人工即时生效行）→ 回退旧
    id 比较；有基线章且身份基线仍在位、当前文件内容已等于本草稿正文 → 上轮写
    文件成功、置状态前中断的恢复态（R7-1），有效放行以补置状态；除此之外实读时点
    的 applied 身份已不在位（人工变更/回滚/ABA）或当前文件内容偏离实读基线
    （热编辑）均失效；兼容章额外保留旧「更高 applied 取代」判定。

    参数：
        version: ResearchPromptVersion，待生效的草稿版本
        latest: ResearchPromptVersion | None，当前最新 applied 版本
        current_md5: str，当前文件内容 md5（调用方在生效锁内采样）

    返回：
        str | None：失效原因（供告警日志）；None 表示基线有效、可生效
    """
    if latest is not None and latest.id == version.id:
        return None  # 幂等重放：该版本自身即最新生效，其内容本就异于基线
    if version.base_md5 is None:
        if latest is not None and latest.id > version.id:
            return f"已被更高的 applied 版本 v{latest.id} 取代"
        return None
    # R7-1 中断恢复态：身份基线仍在位且文件已是本草稿正文——上轮 _atomic_write
    # 成功、set_version_status(applied) 前被打断，文件与库只差一个状态位；
    # 放行让生效路径重写同内容文件（幂等）并补置 applied，不得误判为热编辑废弃
    if (
        version.base_applied_version_id is not None
        and latest is not None
        and latest.id == version.base_applied_version_id
        and current_md5 == version.md5
    ):
        return None
    if version.base_applied_version_id is not None and (
        latest is None or latest.id != version.base_applied_version_id
    ):
        current_id = f"v{latest.id}" if latest is not None else "无"
        return f"实读基线版本 v{version.base_applied_version_id} 已不在位（当前生效 {current_id}）"
    if current_md5 != version.base_md5:
        return f"当前文件内容已偏离实读基线（基线 md5={version.base_md5[:8]}…）"
    if version.base_applied_version_id is None and latest is not None and latest.id > version.id:
        return f"已被更高的 applied 版本 v{latest.id} 取代"
    return None


def content_md5(content: str) -> str:
    """研报提示词原文 md5（与 ResearchPromptLoader.body_md5 同一算法，作为版本关联键）。

    参数：
        content: str，待保存的完整文本

    返回：
        str，研报提示词正文的 md5 十六进制摘要
    """
    return hashlib.md5(content.encode("utf-8")).hexdigest()


class ResearchPromptValidationError(Exception):
    """研报提示词写前校验失败：reasons 携带全部未过项（供工具返回给 LLM 修正重试）。

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


class ResearchPromptStore:
    """研报提示词文件与版本表的统一入口（人工修改与复盘改写同走此路径）。

    隐含不变量：`_atomic_write` 与 `save_version` 之间不得插入 await
    （write→save 同步临界区，保证最新版本==当前文件）；
    提交内容入口一律把 \r\n 归一化为 \n（Windows 编辑器兼容），保证版本行 md5
    与 ResearchPromptLoader.body_md5（读回文本）一致，版本↔研报 join 不断裂。
    """

    def __init__(
        self,
        prompt_path: str | Path,
        repo: Repo,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """初始化研报提示词存储入口：绑定提示词文件路径、版本仓库与变更回调。

        参数：
            prompt_path: str | Path，研报提示词文件路径（research_prompt.md）
            repo: Repo，持久化仓库（版本表读写经其 research_prompt 子仓库）
            on_change: Callable[[], None] | None，提示词变更回调（apply/rollback
                落版本后触发，如广播 WS 事件）；省略时默认为 None，表示不接线

        返回：
            None，就地设置实例属性（保存路径、仓库与回调引用）
        """
        self._path = Path(prompt_path)
        self._repo = repo
        self._on_change = on_change
        # 生效临界区锁（issue #113 F11）：apply_version/revise_applied 全收锁，
        # 锁内重读最新 applied——旧草稿晚于人工更高版本到达时不覆盖人工内容
        self._apply_lock = asyncio.Lock()

    def _notify_change(self) -> None:
        """变更即通知（前端据此立即重拉版本面板）；未接线时静默跳过。

        参数：无

        返回：
            None，未接线时静默跳过
        """
        if self._on_change is not None:
            self._on_change()

    async def seed_if_empty(self) -> ResearchPromptVersion | None:
        """启动播种：版本表为空且提示词文件存在 → 记 v1（created_by='human'）。

        参数：无

        返回：
            ResearchPromptVersion | None：播种成功返回 v1 版本对象；
            版本表非空或文件不存在（空正文）时返回 None
        """
        if await self._repo.research_prompt.list_versions():
            return None
        current = self.current()
        if not current:
            return None
        return await self._repo.research_prompt.save_version(
            current, content_md5(current), "human", "初始版本"
        )

    def current(self) -> str:
        """当前研报提示词原文；文件不存在返回 ''。

        参数：无

        返回：
            str，当前研报提示词原文；文件不存在返回 ''
        """
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    async def sample_current_base(self) -> tuple[str, str, int | None]:
        """在生效锁内原子采样当前生效内容三元组：文件正文、其 md5、最新 applied 版本 id。

        供 LLM 读取时点给本轮草稿盖基线章（issue #113 R6-3）：正文即 LLM 所见
        内容（调用方直接嵌入展示，避免二次读文件的竞态窗口），md5 与 applied id
        一并写入草稿；轮末生效时按身份 + 文件内容双重比对，防 ABA 与热编辑漏检。

        参数：无

        返回：
            tuple[str, str, int | None]：当前文件正文（不存在为 ''）、正文 md5、
            最新 applied 版本 id（无 applied 版本时 None）
        """
        async with self._apply_lock:
            content = self.current()
            latest = await self._repo.research_prompt.latest_applied_version()
            return content, content_md5(content), latest.id if latest is not None else None

    async def current_snapshot(self) -> tuple[str, str, int | None]:
        """在生效锁内原子采样研报运行用快照：正文、正文 md5、可归因的 applied 版本 id。

        供研报 agent 构建 prompt 时点一次取齐（issue #113 R6-4）：正文/md5/版本归属
        同源同刻，避免「读正文 → 算 md5 → 反解版本」分步取样被运行途中热替换错位。
        版本归因按内容一致性确认：最新 applied 版本 md5 与当前正文一致才归因其 id，
        否则（文件热编辑、播种缺失等）返回 None。

        参数：无

        返回：
            tuple[str, str, int | None]：当前正文（文件不存在为 ''）、正文 md5、
            可归因的最新 applied 版本 id（内容不一致或无 applied 版本时 None）
        """
        async with self._apply_lock:
            content = self.current()
            md5 = content_md5(content)
            latest = await self._repo.research_prompt.latest_applied_version()
            version_id = latest.id if latest is not None and latest.md5 == md5 else None
            return content, md5, version_id

    async def revise(
        self,
        content: str,
        reason: str,
        created_by: str,
        review_report_id: int | None = None,
        base_md5: str | None = None,
        base_applied_version_id: int | None = None,
    ) -> ResearchPromptVersion:
        """校验通过后落 draft 草稿版本；文件不动，报告成功后经 apply_version 生效。

        参数：
            content: str，待保存的完整文本
            reason: str，变更原因
            created_by: str，版本创建来源
            review_report_id: int | None，关联复盘报告标识
            base_md5: str | None，草稿基线 md5（LLM 实读时点的生效内容摘要，
                issue #113 CAS/R6-3）；人工即时生效路径（revise_applied）锁内落库
                即生效、无竞态窗口，不传（None 回退旧 id 比较）
            base_applied_version_id: int | None，草稿基线的 applied 版本身份
                （issue #113 R6-3，与 base_md5 同时取自 sample_current_base）

        返回：
            ResearchPromptVersion，校验通过后落库的 draft 版本（提示词文件未改动，
            先记账后生效）；校验失败抛 ResearchPromptValidationError

        异常：
            ResearchPromptValidationError，内容未通过写前校验时抛出
        """
        content = content.replace("\r\n", "\n")  # 归一化后校验/md5/写盘/落库用同一份内容
        self._validate(content)
        return await self._repo.research_prompt.save_version(
            content,
            content_md5(content),
            created_by,
            reason,
            review_report_id,
            status="draft",
            base_md5=base_md5,
            base_applied_version_id=base_applied_version_id,
        )

    async def revise_applied(
        self, content: str, reason: str, created_by: str = "human"
    ) -> ResearchPromptVersion:
        """校验后落库并立即生效（写文件 + applied）——人工/服务端即时修改专用。

        复盘 agent 走 revise（draft）+ 报告成功后 apply_version；本方法是
        监控接口等"人按下按钮即刻生效"场景的合并入口。
        落草稿与生效全程在生效锁内完成，避免与轮末草稿生效交错（issue #113 F11）。

        参数：
            content: str，待保存的完整文本
            reason: str，变更原因
            created_by: str，版本创建来源，默认 human

        返回：
            ResearchPromptVersion：已生效（applied）的版本对象

        异常：
            ResearchPromptValidationError：内容校验失败，或新版本已被更高 applied
                版本取代（锁内防御分支，理论上不可达）时抛出
        """
        async with self._apply_lock:
            version = await self.revise(content, reason, created_by)
            applied = await self._apply_version_locked(version.id)
            if applied is None:  # 锁内落的新版本不可能被取代，防御性检查
                raise ResearchPromptValidationError(
                    [f"研报提示词版本 v{version.id} 生效失败：已被更高版本取代"]
                )
            return applied

    async def apply_version(self, version_id: int) -> ResearchPromptVersion | None:
        """把草稿（或历史）版本原子写入提示词文件并置为 applied——统一生效入口。

        全程在生效锁内；锁内重读最新 applied 版本与当前文件内容：草稿基线章
        （base_md5/base_applied_version_id，issue #113 CAS/R6-3）失效——实读时点
        的 applied 身份不在位（人工变更/回滚/ABA）或文件被热编辑偏离实读内容——
        或无基线章但已被更高 applied 版本取代时，均置 discarded 并返回 None，
        不覆盖人工新内容（issue #113 F11）。

        参数：
            version_id: int，待生效的版本编号

        返回：
            ResearchPromptVersion | None：已生效（applied）的版本对象；
            基线已失效或已被更高 applied 版本取代时返回 None
            （本版本已置 discarded）

        异常：
            ResearchPromptValidationError，目标版本不存在时抛出
        """
        async with self._apply_lock:
            return await self._apply_version_locked(version_id)

    async def _apply_version_locked(self, version_id: int) -> ResearchPromptVersion | None:
        """apply_version 的无锁核心（调用方必须已持有 _apply_lock）。

        参数：
            version_id: int，待生效的版本编号

        返回：
            ResearchPromptVersion | None：已生效版本；草稿基线已失效（CAS）
            或被更高 applied 版本取代时返回 None

        异常：
            ResearchPromptValidationError，目标版本不存在时抛出
        """
        version = await self._repo.research_prompt.get_version(version_id)
        if version is None:
            raise ResearchPromptValidationError([f"研报提示词版本 v{version_id} 不存在，无法生效"])
        latest = await self._repo.research_prompt.latest_applied_version()
        # 草稿基线 CAS（issue #113，R6-3 加固）：语义同 StrategyStore——基线章绑定
        # LLM 实读时点的文件内容 md5 与最新 applied 版本身份；人工变更/回滚/ABA
        # 导致身份不在位、或文件被热编辑偏离实读内容，均判基线失效、废弃草稿
        # 不生效，不覆盖人工内容（issue #113 F11）
        reason = _staleness_reason(version, latest, content_md5(self.current()))
        if reason is not None:
            await self._repo.research_prompt.set_version_status(version_id, "discarded")
            logger.warning("研报提示词草稿 v%d 的基线已失效（%s），废弃不生效", version_id, reason)
            return None
        self._atomic_write(version.content)
        await self._repo.research_prompt.set_version_status(version_id, "applied")
        self._notify_change()
        return await self._repo.research_prompt.get_version(version_id)

    async def discard_draft(self, version_id: int) -> None:
        """把草稿版本置为 discarded（报告失败/取消时调用）。

        参数：
            version_id: int，待废弃的草稿版本编号

        返回：
            None，就地更新数据库状态
        """
        await self._repo.research_prompt.set_version_status(version_id, "discarded")

    async def reconcile(self) -> None:
        """启动对账：提示词文件与最新 applied 版本不一致时以数据库为准恢复文件。

        堵"文件已替换、数据库落库失败/进程中断"留下的不一致窗口；
        无 applied 版本或内容一致时不做任何事。同时清理孤儿草稿：
        启动时不存在进行中的复盘轮，残留 draft 必为上轮异常遗留。

        参数：无

        返回：
            None，先废弃全部 draft，再在不一致时恢复文件并触发变更通知；一致时静默
        """
        orphans = await self._repo.research_prompt.discard_all_drafts()
        if orphans:
            logger.warning("已废弃 %d 个孤儿研报提示词草稿（上轮未正常收尾）", orphans)
        latest = await self._repo.research_prompt.latest_applied_version()
        if latest is None or latest.md5 == content_md5(self.current()):
            return
        logger.warning("研报提示词与最新生效版本不一致（v%d），以数据库为准恢复", latest.id)
        self._atomic_write(latest.content)
        self._notify_change()

    async def rollback(self, version_id: int) -> ResearchPromptVersion:
        """回滚到历史版本：写回其内容并记 created_by='rollback' 的新版本。

        只允许回滚到 status 为 applied 的版本：草稿/已废弃版本从未生效过，
        其内容未经过生效路径（apply_version）的完整检验，不允许直接提升为当前内容。
        全程在生效锁内（与 apply_version/revise_applied 互斥）：读目标→写文件→
        记新版本之间不得被并发生效插队，否则文件与库内最新 applied 版本会错位
        （issue #113 R7）。

        参数：
            version_id: int，目标历史版本标识

        返回：
            ResearchPromptVersion，回滚产生的新版本（applied）

        异常：
            ResearchPromptValidationError，目标版本不存在或状态非 applied（不可回滚）时抛出
        """
        async with self._apply_lock:
            version = await self._repo.research_prompt.get_version(version_id)
            if version is None:
                raise ResearchPromptValidationError(
                    [f"研报提示词版本 v{version_id} 不存在，无法回滚"]
                )
            if version.status != "applied":
                raise ResearchPromptValidationError(
                    [f"研报提示词版本 v{version_id} 状态为 {version.status}，只能回滚到已生效版本"]
                )
            content = version.content.replace("\r\n", "\n")  # 历史脏行归一化后写回并重算 md5
            self._atomic_write(content)
            new_version = await self._repo.research_prompt.save_version(
                content, content_md5(content), "rollback", f"回滚到 v{version_id}"
            )
            self._notify_change()
            return new_version

    async def list_versions(self) -> list[ResearchPromptVersion]:
        """列出全部研报提示词历史版本，最新版本排在最前。

        参数：无

        返回：
            list[ResearchPromptVersion]：全部版本，按版本 id 倒序（最新在前）
        """
        return await self._repo.research_prompt.list_versions()

    async def get_version(self, version_id: int) -> ResearchPromptVersion | None:
        """按版本 id 读取单个研报提示词历史版本。

        参数：
            version_id: int，版本编号

        返回：
            ResearchPromptVersion | None：对应版本；该 id 不存在时返回 None
        """
        return await self._repo.research_prompt.get_version(version_id)

    def _validate(self, content: str) -> None:
        """写前校验：收集全部未过项一次性抛出（LLM 可逐项修正）。

        参数：
            content: str，待保存的完整文本

        返回：
            None，校验通过时无副作用

        异常：
            ResearchPromptValidationError，内容存在任一校验失败项时汇总抛出
        """
        reasons: list[str] = []
        if len(content.strip()) < _MIN_CHARS:
            reasons.append(
                f"研报提示词过短：strip 后 {len(content.strip())} 字符，最少 {_MIN_CHARS} 字符"
            )
        size = len(content.encode("utf-8"))
        if size > _MAX_BYTES:
            reasons.append(
                f"研报提示词过长：UTF-8 体积 {size} 字节，上限 {_MAX_BYTES} 字节（32KB）"
            )
        if content == self.current():
            reasons.append(_NO_DIFF_REASON)
        if reasons:
            raise ResearchPromptValidationError(
                reasons, no_diff_only=(reasons == [_NO_DIFF_REASON])
            )

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
