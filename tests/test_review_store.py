"""src/review/strategy.py 策略版本管理测试：tmp_path 策略书文件 + tmp_path 真实 SQLite。

覆盖：播种 v1（幂等/缺文件跳过）、revise 成功（文件/md5/版本行）、三类校验拒绝
（短/超长/无差异：原文件不动、无新版本、原因列表）、rollback（内容回写 + 新版本
created_by='rollback'）、版本不存在报错。
"""

import asyncio
import hashlib

import pytest

from src.agent.prompts import PromptLoader
from src.memory import Database, Repo
from src.review.strategy import StrategyStore, StrategyValidationError, content_md5

_INIT = "初始策略书：" + "稳健交易，控制回撤。" * 10  # ≥100 字符
_NEW = "新策略书：" + "顺势加仓，严格止损。" * 10


@pytest.fixture
async def repo(tmp_path):
    """构造指向临时 SQLite 数据库的 Repo 仓储，测试结束后关闭连接。

    参数：
        tmp_path: Path，pytest 临时目录夹具，test.db 数据库文件落在其中

    返回：
        AsyncIterator[Repo]，yield 已打开临时数据库的仓储对象，用例结束后关闭连接
    """
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
def prompt_path(tmp_path):
    """在临时目录写入初始策略书文件并返回其路径。

    参数：
        tmp_path: Path，pytest 临时目录夹具，system_prompt.md 写在其中

    返回：
        Path：内容为 _INIT 初始策略书的 system_prompt.md 文件路径
    """
    path = tmp_path / "system_prompt.md"
    path.write_text(_INIT, encoding="utf-8")
    return path


@pytest.fixture
async def store(prompt_path, repo):
    """构造绑定临时策略文件与临时数据库的 StrategyStore。

    参数：
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径
        repo: Repo，repo 夹具提供的临时数据库仓储

    返回：
        StrategyStore：策略版本管理对象，文件与数据库均隔离在临时目录
    """
    return StrategyStore(prompt_path, repo)


# ---------- 播种 ----------


async def test_seed_if_empty_creates_v1(store, repo, prompt_path):
    """版本表为空时播种生成 v1 初始版本，重复调用幂等不再插入。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径

    返回：
        None，断言 v1 的 id/created_by/reason/内容/md5 符合初始版本约定，
        二次播种返回 None 且版本表仅 1 条记录
    """
    v = await store.seed_if_empty()
    assert v is not None
    assert v.id == 1 and v.created_by == "human" and v.reason == "初始版本"
    assert v.content == _INIT
    assert v.md5 == hashlib.md5(_INIT.encode("utf-8")).hexdigest()  # 与 prompts.py 同算法
    # 幂等：版本表非空后不再播种
    assert await store.seed_if_empty() is None
    assert len(await repo.review.list_strategy_versions()) == 1


async def test_seed_skips_when_prompt_missing(tmp_path, repo):
    """策略书文件不存在时播种被跳过，不创建任何版本。

    参数：
        tmp_path: Path，pytest 临时目录夹具，用于拼出不存在的策略文件路径
        repo: Repo，repo 夹具提供的临时数据库仓储

    返回：
        None，断言 seed_if_empty 返回 None 且版本列表为空
    """
    store = StrategyStore(tmp_path / "nope.md", repo)
    assert await store.seed_if_empty() is None
    assert await repo.review.list_strategy_versions() == []


# ---------- revise ----------


async def test_revise_success(store, repo, prompt_path):
    """revise 成功路径：文件原子替换、版本字段正确、report_id 透传、版本列表倒序。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径

    返回：
        None，断言策略文件已替换为新内容、版本 md5/created_by/reason 正确、
        current() 返回新内容、report_id 落库且版本列表按最新在前排列
    """
    v = await store.revise(_NEW, "复盘改进", created_by="review_agent")
    # 草稿语义（issue #62/#73）：revise 只落 draft，文件与 current() 不变
    assert prompt_path.read_text(encoding="utf-8") != _NEW
    assert v.status == "draft" and v.md5 == content_md5(_NEW)
    applied = await store.apply_version(v.id)
    assert applied.status == "applied"
    assert prompt_path.read_text(encoding="utf-8") == _NEW  # 生效后文件已替换
    assert store.current() == _NEW
    v2 = await store.revise(_NEW + "补充条款。" * 20, "再改", "human", report_id=7)
    assert v2.report_id == 7 and v2.status == "draft"  # report_id 透传落库
    await store.apply_version(v2.id)
    assert [x.id for x in await repo.review.list_strategy_versions()] == [v2.id, v.id]  # 倒序


async def test_revise_rejects_too_short(store, repo, prompt_path):
    """验证策略正文过短时修订被拒绝且文件与版本表均保持不变。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储
        prompt_path: Path，当前策略书文件路径

    返回：
        None，通过断言验证校验原因、原文件内容和空版本列表
    """
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("太短了", "r", "review_agent")
    assert any("100" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT  # 原文件不动
    assert await repo.review.list_strategy_versions() == []  # 无新版本


async def test_revise_rejects_too_long(store, repo, prompt_path):
    """验证策略正文超过字节上限时修订被拒绝且不产生任何持久化变更。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储
        prompt_path: Path，当前策略书文件路径

    返回：
        None，通过断言验证 32KB 原因、原文件内容和空版本列表
    """
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("长" * 12000, "r", "review_agent")  # 36000 字节 > 32KB
    assert any("32KB" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT
    assert await repo.review.list_strategy_versions() == []


async def test_revise_rejects_no_diff(store, repo, prompt_path):
    """验证提交与当前策略完全相同时被判为无差异且不新增版本。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储
        prompt_path: Path，当前策略书文件路径

    返回：
        None，通过断言验证无差异原因、原文件内容和空版本列表
    """
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise(_INIT, "r", "review_agent")
    assert any("无差异" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT
    assert await repo.review.list_strategy_versions() == []


async def test_revise_collects_all_reasons(tmp_path, repo):
    """验证一次修订同时违反多条规则时完整收集全部校验原因。

    参数：
        tmp_path: Path，pytest 临时目录
        repo: Repo，临时数据库仓储

    返回：
        None，通过断言验证过短与无差异两个原因同时返回
    """
    path = tmp_path / "system_prompt.md"
    path.write_text("短内容", encoding="utf-8")  # <100 字符
    store = StrategyStore(path, repo)
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("短内容", "r", "review_agent")  # 既过短又与当前无差异
    assert len(exc_info.value.reasons) == 2


# ---------- 换行归一化（Windows CRLF） ----------


async def test_revise_normalizes_crlf_content(store, repo, prompt_path):
    """验证修订时把 CRLF 归一为 LF 并保持文件、版本与提示词摘要一致。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，当前策略书文件路径

    返回：
        None，通过断言验证无回车字节且三处 md5(内容摘要)一致
    """
    content = "新策略书：\r\n" + "顺势加仓，严格止损。\r\n" * 10
    v = await store.revise(content, "Windows 编辑器提交", created_by="human")
    await store.apply_version(v.id)  # 草稿生效后才写文件（issue #62/#73）
    assert b"\r" not in prompt_path.read_bytes()  # 文件字节无 \r
    normalized = content.replace("\r\n", "\n")
    assert v.md5 == hashlib.md5(normalized.encode("utf-8")).hexdigest()
    assert PromptLoader(prompt_path).body_md5() == v.md5


async def test_rollback_normalizes_historical_crlf(store, repo, prompt_path):
    """验证回滚历史 CRLF 策略时同样归一换行并重算新版本摘要。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，用于直接写入历史脏版本的仓储
        prompt_path: Path，回滚后写入的策略书文件路径

    返回：
        None，通过断言验证文件无回车且版本摘要与提示词摘要一致
    """
    dirty = "脏策略书：\r\n" + "历史遗留内容。\r\n" * 10
    v1 = await repo.review.save_strategy_version(dirty, "md5-dirty", "human", "历史版本")
    v3 = await store.rollback(v1.id)
    assert b"\r" not in prompt_path.read_bytes()
    assert v3.md5 == content_md5(dirty.replace("\r\n", "\n"))
    assert PromptLoader(prompt_path).body_md5() == v3.md5


# ---------- rollback ----------


async def test_rollback_writes_back_and_records(store, repo, prompt_path):
    """验证回滚会回写目标内容并额外记录一条来源明确的新版本。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储
        prompt_path: Path，当前策略书文件路径

    返回：
        None，通过断言验证回写内容、回滚元数据、摘要与版本倒序
    """
    v1 = await store.seed_if_empty()
    v2 = await store.revise(_NEW, "改进", "review_agent")
    v3 = await store.rollback(v1.id)
    assert prompt_path.read_text(encoding="utf-8") == v1.content  # 内容回写
    assert v3.created_by == "rollback" and v3.reason == f"回滚到 v{v1.id}"
    assert v3.md5 == v1.md5  # 回写内容与原版本同 md5
    versions = await repo.review.list_strategy_versions()
    assert [v.id for v in versions] == [v3.id, v2.id, v1.id]  # 最新在前


async def test_rollback_missing_version(store):
    """验证回滚不存在的版本编号时返回明确的策略校验错误。

    参数：
        store: StrategyStore，策略版本管理对象

    返回：
        None，通过断言验证异常消息包含版本不存在提示
    """
    with pytest.raises(StrategyValidationError, match="不存在"):
        await store.rollback(999)


# ---------- 结构化判定字段 no_diff_only ----------


async def test_validation_error_no_diff_only_flag(store, repo, prompt_path):
    """验证 no_diff_only(仅无差异)只在唯一原因确为无差异时成立。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，当前策略书文件路径，本用例通过 store 间接使用

    返回：
        None，通过断言区分无差异、过短和版本不存在三种校验错误
    """
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise(_INIT, "r", "human")  # 唯一原因是无差异
    assert exc_info.value.no_diff_only is True
    with pytest.raises(StrategyValidationError) as exc_info2:
        await store.revise("太短了", "r", "human")  # 唯一原因是过短
    assert exc_info2.value.no_diff_only is False
    with pytest.raises(StrategyValidationError) as exc_info3:
        await store.rollback(999)  # 版本不存在
    assert exc_info3.value.no_diff_only is False


async def test_validation_error_no_diff_only_false_when_mixed(tmp_path, repo):
    """验证无差异与过短并存时不把混合错误误判为仅无差异。

    参数：
        tmp_path: Path，pytest 临时目录
        repo: Repo，临时数据库仓储

    返回：
        None，通过断言验证两个原因同时存在且 no_diff_only 为 False
    """
    path = tmp_path / "system_prompt.md"
    path.write_text("短内容", encoding="utf-8")
    store = StrategyStore(path, repo)
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("短内容", "r", "review_agent")
    assert len(exc_info.value.reasons) == 2
    assert exc_info.value.no_diff_only is False


# ---------- 变更回调 on_change（WS strategy_updated 接线点） ----------


async def test_on_change_fired_on_revise_and_rollback(prompt_path, repo):
    """验证修订与回滚成功时触发变更回调而播种及校验失败时不触发。

    参数：
        prompt_path: Path，当前策略书文件路径
        repo: Repo，临时数据库仓储

    返回：
        None，通过断言验证不同操作后的累计回调次数
    """
    calls: list[int] = []
    store = StrategyStore(prompt_path, repo, on_change=lambda: calls.append(1))
    v1 = await store.seed_if_empty()
    assert calls == []  # 播种不算变更（启动时无前端需要通知）
    draft = await store.revise(_NEW, "改进", "review_agent")
    assert calls == []  # 草稿落库不算变更（文件未动）
    await store.apply_version(draft.id)
    assert len(calls) == 1
    await store.rollback(v1.id)
    assert len(calls) == 2
    with pytest.raises(StrategyValidationError):
        await store.revise("太短了", "r", "review_agent")  # 校验拒绝：不触发
    with pytest.raises(StrategyValidationError):
        await store.rollback(999)  # 版本不存在：不触发
    assert len(calls) == 2


async def test_on_change_absent_is_noop(store, repo):
    """验证未接入变更回调时策略修订仍可正常完成。

    参数：
        store: StrategyStore，未配置 on_change 的策略版本管理对象
        repo: Repo，临时数据库仓储，本用例通过 store 间接使用

    返回：
        None，通过断言验证新版本摘要与新正文一致
    """
    v = await store.revise(_NEW, "改进", "review_agent")
    assert v.md5 == content_md5(_NEW)


# ---------- 生效锁取代检测（issue #113 F11） ----------


async def test_apply_version_yields_to_newer_applied(store, repo, prompt_path):
    """旧草稿生效时若已存在更高 id 的 applied 版本，则被取代置 discarded 且不覆盖文件。

    串行模拟"复盘草稿与人工即时修改交错"的竞态：草稿 v2 落库后，人工
    revise_applied 先生效 v3，轮末再 apply v2 时锁内重读到 v3，放弃生效。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径

    返回：
        None，断言 apply 返回 None、文件保留人工内容、旧草稿状态为 discarded
    """
    await store.seed_if_empty()
    draft = await store.revise(_NEW, "复盘改进", "review_agent")
    human = await store.revise_applied("人工策略书：" + "人工优先，覆盖草稿。" * 10, "人工修改")
    applied = await store.apply_version(draft.id)
    assert applied is None  # 已被更高 applied 版本取代
    assert store.current() == human.content  # 文件保留人工内容
    assert (await store.get_version(draft.id)).status == "discarded"
    assert (await store.get_version(human.id)).status == "applied"


async def test_apply_drafts_skips_superseded_without_failure(store, repo, prompt_path):
    """apply_drafts 遇被取代草稿（apply_version 返回 None）只跳过，不计入失败列表。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，当前策略书文件路径

    返回：
        None，断言 apply_failed_ids 为空、被取代草稿为 discarded、文件保留人工内容
    """
    from types import SimpleNamespace

    from src.review.drafts import apply_drafts

    await store.seed_if_empty()
    draft = await store.revise(_NEW, "复盘改进", "review_agent")
    await store.revise_applied("人工策略书：" + "人工优先，覆盖草稿。" * 10, "人工修改")
    deps = SimpleNamespace(
        store=store,
        strategy_draft_ids=[draft.id],
        indicator_config_store=None,
        research_prompt_store=None,
        apply_failed_ids=[],
    )
    await apply_drafts(deps)
    assert deps.apply_failed_ids == []  # 被取代不算失败
    assert (await store.get_version(draft.id)).status == "discarded"
    assert store.current().startswith("人工策略书")


async def test_rollback_and_apply_interleave_keeps_file_consistent(
    store, repo, prompt_path, monkeypatch
):
    """rollback 与 apply_version 并发时全程互斥：文件始终等于库内最新 applied 版本内容。

    在 rollback 记新版本前插入延时制造确定性交错：无锁时 apply_version 会插队先生效
    草稿，rollback 随后落库更高 id 的回滚版本而文件停在草稿内容——文件与库内最新
    applied 错位；rollback 收进生效锁后两者串行，二者必然一致（issue #113 R7）。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径
        monkeypatch: MonkeyPatch，用于给记版本方法插入延时

    返回：
        None，断言文件内容与库内最新 applied 版本内容一致
    """
    await store.seed_if_empty()
    v1 = (await store.list_versions())[0]
    draft = await store.revise(_NEW, "复盘改进", "review_agent")
    original_save = repo.review.save_strategy_version

    async def slow_save(*args, **kwargs):
        """记版本前延时 50ms，制造 rollback 写文件后、记版本前的插队窗口。

        参数：
            *args: 原 save_strategy_version 的位置参数
            **kwargs: 原 save_strategy_version 的关键字参数

        返回：
            原 save_strategy_version 的返回（透传）
        """
        await asyncio.sleep(0.05)
        return await original_save(*args, **kwargs)

    monkeypatch.setattr(repo.review, "save_strategy_version", slow_save)
    await asyncio.gather(store.rollback(v1.id), store.apply_version(draft.id))
    latest = await repo.review.latest_applied_strategy_version()
    assert latest is not None
    assert store.current() == latest.content


async def test_apply_drafts_failure_records_channel_and_formats(store, repo, monkeypatch):
    """apply 抛异常时按 (通道键, 草稿 id) 记入失败列表，格式化助手指明文件名（R9）。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，临时数据库仓储，本用例通过 store 间接使用
        monkeypatch: MonkeyPatch，用于让 apply_version 抛持久性异常

    返回：
        None，断言失败条目带 strategy 通道、文件未被改动、文件名助手输出正确
    """
    from types import SimpleNamespace

    from src.review.drafts import apply_drafts, apply_failed_files, format_apply_failures

    await store.seed_if_empty()
    draft = await store.revise(_NEW, "复盘改进", "review_agent")

    async def _boom(version_id):
        """模拟磁盘满等持久性失败：任何生效调用直接抛错。

        参数：
            version_id: int，待生效的版本编号（本桩不使用）

        返回：
            无正常返回，恒抛 RuntimeError

        异常：
            RuntimeError：模拟磁盘满等持久性失败
        """
        raise RuntimeError("磁盘已满")

    monkeypatch.setattr(store, "apply_version", _boom)
    deps = SimpleNamespace(
        store=store,
        strategy_draft_ids=[draft.id],
        indicator_config_store=None,
        research_prompt_store=None,
        apply_failed_ids=[],
    )
    await apply_drafts(deps)
    assert deps.apply_failed_ids == [("strategy", draft.id)]
    assert apply_failed_files(deps.apply_failed_ids) == ["system_prompt.md"]
    assert format_apply_failures(deps.apply_failed_ids) == f"system_prompt.md 草稿 v{draft.id}"
    assert store.current().startswith("初始策略书")  # 生效失败，文件未被改动


# ---------- 草稿基线 CAS（issue #113） ----------


async def test_cas_discards_stale_draft_after_human_intervenes(
    store, repo, prompt_path, monkeypatch, caplog
):
    """交错复现竞态：人工持锁落库让出期间 agent 草稿以更大 id 插队，轮末生效被 CAS 废弃。

    人工 revise_applied(C) 在生效锁内落库后、写文件前让出事件循环，复盘整改
    revise(B, base_md5=md5(初始内容)) 无锁落 draft v3（id 大于人工 v2）；
    无 CAS 时旧 id 比较（v3 > v2）不会判取代，陈旧 B 会覆盖人工 C（PR #114 R6）。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径
        monkeypatch: MonkeyPatch，用于在人工落库点插入确定性让出
        caplog: LogCaptureFixture，用于断言基线取代告警

    返回：
        None，断言草稿 apply 返回 None、置 discarded、文件保留人工内容、告警落日志
    """
    import logging

    await store.seed_if_empty()
    base = content_md5(_INIT)
    human_content = "人工策略书：" + "人工优先，覆盖草稿。" * 10
    human_saved = asyncio.Event()
    agent_done = asyncio.Event()
    original_save = repo.review.save_strategy_version

    async def gated_save(content, md5, created_by, *args, **kwargs):
        """人工落库完成后让出事件循环，等 agent 草稿插队落库后再放行。

        参数：
            content: str，版本内容（透传）
            md5: str，内容摘要（透传）
            created_by: str，创建来源；为 "human" 时触发让出门（agent 草稿不拦）
            *args: 其余位置参数（透传）
            **kwargs: 其余关键字参数（透传）

        返回：
            原 save_strategy_version 的返回（透传）
        """
        version = await original_save(content, md5, created_by, *args, **kwargs)
        if created_by == "human":
            human_saved.set()  # 人工 draft 已落库但仍持生效锁、尚未写文件置 applied
            await agent_done.wait()
        return version

    monkeypatch.setattr(repo.review, "save_strategy_version", gated_save)
    human_task = asyncio.create_task(store.revise_applied(human_content, "人工修改"))
    await human_saved.wait()
    draft = await store.revise(_NEW, "复盘改进", "review_agent", base_md5=base)
    assert draft.id > 2  # agent 草稿以更大 id 插队（v1 种子、v2 人工、v3 草稿）
    agent_done.set()
    human = await human_task
    assert human.status == "applied"
    with caplog.at_level(logging.WARNING):
        applied = await store.apply_version(draft.id)
    assert applied is None  # 基线已失效（人工变更取代实读基线）
    assert (await store.get_version(draft.id)).status == "discarded"
    assert store.current() == human_content  # 陈旧草稿未覆盖人工内容
    assert any("基线已失效" in r.message for r in caplog.records)


async def test_cas_applies_draft_when_base_matches(store, repo, prompt_path):
    """草稿基线与最新 applied 内容一致时正常生效（CAS 正路径）。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径

    返回：
        None，断言草稿生效、文件替换、版本行 base_md5 落库
    """
    v1 = await store.seed_if_empty()
    draft = await store.revise(_NEW, "复盘改进", "review_agent", base_md5=v1.md5)
    assert draft.base_md5 == v1.md5
    applied = await store.apply_version(draft.id)
    assert applied is not None and applied.status == "applied"
    assert prompt_path.read_text(encoding="utf-8") == _NEW


async def test_cas_null_base_falls_back_to_id_compare(store, repo, prompt_path):
    """base_md5 为 NULL 的历史/人工行回退旧 id 比较：id 更大的草稿照常生效。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径

    返回：
        None，断言无基线草稿按旧行为生效覆盖、文件为新内容
    """
    await store.seed_if_empty()
    await store.revise_applied("人工策略书：" + "人工优先，覆盖草稿。" * 10, "人工修改")
    draft = await store.revise(_NEW, "复盘改进", "review_agent")  # 不盖基线章
    assert draft.base_md5 is None
    applied = await store.apply_version(draft.id)
    assert applied is not None and applied.status == "applied"  # id 更大，按旧行为生效
    assert prompt_path.read_text(encoding="utf-8") == _NEW


async def test_cas_replay_of_applied_draft_is_idempotent(store, repo, prompt_path):
    """已生效草稿的幂等重放不被 CAS 误判：latest 即本版本自身时跳过比对。

    轮末生效成功后被打断、启动重放 apply_drafts 的场景（_complete_interrupted）：
    草稿自身即最新 applied，其内容必然异于基线，无 latest.id == version_id 豁免
    会把已生效草稿误判废弃。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径

    返回：
        None，断言二次 apply 仍返回生效版本、文件与状态保持不变
    """
    v1 = await store.seed_if_empty()
    draft = await store.revise(_NEW, "复盘改进", "review_agent", base_md5=v1.md5)
    first = await store.apply_version(draft.id)
    assert first is not None and first.status == "applied"
    second = await store.apply_version(draft.id)  # 幂等重放
    assert second is not None and second.status == "applied"
    assert prompt_path.read_text(encoding="utf-8") == _NEW
    assert (await store.get_version(draft.id)).status == "applied"


async def test_sample_current_base(store, repo, prompt_path):
    """读取时点基线采样（issue #113 R6-3）：返回（文件正文, 正文 md5, 最新 applied id）三元组。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径

    返回：
        None，断言无版本/seed 后/人工生效后三种状态下采样三元组的正文、md5 与 id
    """
    assert await store.sample_current_base() == (_INIT, content_md5(_INIT), None)  # 无版本
    v1 = await store.seed_if_empty()
    assert await store.sample_current_base() == (_INIT, v1.md5, v1.id)  # 有 applied
    human = "人工策略书：" + "人工优先，覆盖草稿。" * 10
    v2 = await store.revise_applied(human, "人工修改")
    assert await store.sample_current_base() == (human, v2.md5, v2.id)


async def test_cas_discards_draft_when_base_version_aba_replaced(store, repo, prompt_path, caplog):
    """基线版本 ABA 回绕：草稿基于 v1(A) 实读盖章后，人工改 B(v2) 又回滚 A(v3)，轮末废弃。

    内容回绕后当前文件 md5 与基线一致（都是 A），仅凭 md5 比对的旧 CAS 会放行；
    新 CAS 按实读基线身份检出失效（base_applied_version_id=v1 ≠ 当前生效 v3，
    issue #113 R6-3）。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径
        caplog: LogCaptureFixture，用于断言基线失效告警

    返回：
        None，断言草稿 apply 返回 None、置 discarded、文件保留回滚后内容、告警落日志
    """
    import logging

    v1 = await store.seed_if_empty()
    _, base_md5, base_vid = await store.sample_current_base()
    draft = await store.revise(
        _NEW, "复盘改进", "review_agent", base_md5=base_md5, base_applied_version_id=base_vid
    )
    await store.revise_applied("人工策略书：" + "人工优先，覆盖草稿。" * 10, "人工改 B")  # v2
    await store.rollback(v1.id)  # v3：内容回绕为 A，id 前进
    with caplog.at_level(logging.WARNING):
        applied = await store.apply_version(draft.id)
    assert applied is None
    assert (await store.get_version(draft.id)).status == "discarded"
    assert store.current() == _INIT  # 文件保留回滚后的 A
    assert any("基线已失效" in r.message and "已不在位" in r.message for r in caplog.records)


async def test_cas_discards_draft_when_file_hot_edited(store, repo, prompt_path, caplog):
    """文件热编辑：草稿盖章后文件被绕过 store 直接改写（库中 applied 不动），轮末废弃。

    实读基线身份仍在位（v1），但当前文件内容 md5 已偏离实读基线——防"库不变、
    文件被外部编辑"的漏检（issue #113 R6-3）。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径
        caplog: LogCaptureFixture，用于断言基线失效告警

    返回：
        None，断言草稿 apply 返回 None、置 discarded、热编辑内容不被覆盖、告警落日志
    """
    import logging

    await store.seed_if_empty()
    _, base_md5, base_vid = await store.sample_current_base()
    draft = await store.revise(
        _NEW, "复盘改进", "review_agent", base_md5=base_md5, base_applied_version_id=base_vid
    )
    hot = "热编辑策略书：" + "绕过存储直接改文件。" * 10
    prompt_path.write_text(hot, encoding="utf-8")  # 热编辑：动文件不动库
    with caplog.at_level(logging.WARNING):
        applied = await store.apply_version(draft.id)
    assert applied is None
    assert (await store.get_version(draft.id)).status == "discarded"
    assert store.current() == hot  # 热编辑内容不被草稿覆盖
    assert any("基线已失效" in r.message and "偏离实读基线" in r.message for r in caplog.records)


async def test_interrupted_after_write_recovers_on_apply_retry(
    store, repo, prompt_path, monkeypatch
):
    """写文件成功、置状态前中断的草稿：重试识别恢复态补置 applied，不误判热编辑废弃（R7-1）。

    首轮 apply 在原子写成功后让 set_version_status("applied") 抛错，留下「文件已是
    草稿正文、库内仍是旧 applied + 本草稿 draft」的中断现场；随后经 apply_drafts
    重试（_complete_interrupted 的幂等收尾路径）：身份基线仍在位且文件内容已等于
    草稿正文 → 恢复态放行，幂等重写同内容文件并补置 applied，失败集合保持为空
    （轮末事件 applied=True 名实相符）。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径
        monkeypatch: MonkeyPatch，用于让首次置 applied 抛错模拟进程中断

    返回：
        None，断言重试后草稿 applied、文件与库内最新 applied 一致、失败集合为空
    """
    from types import SimpleNamespace

    from src.review.drafts import apply_drafts

    await store.seed_if_empty()
    _, base_md5, base_vid = await store.sample_current_base()
    draft = await store.revise(
        _NEW, "复盘改进", "review_agent", base_md5=base_md5, base_applied_version_id=base_vid
    )
    original_set = repo.review.set_version_status
    tripped = {"done": False}

    async def flaky_set_status(version_id, status):
        """首次置 applied 时模拟「写文件已成功、状态未落库」的进程中断。

        参数：
            version_id: int，版本编号（透传）
            status: str，目标状态（透传）

        返回：
            原 set_version_status 的返回（透传）

        异常：
            RuntimeError：首次置 applied 时抛出，模拟置状态前进程中断
        """
        if status == "applied" and not tripped["done"]:
            tripped["done"] = True
            raise RuntimeError("模拟置状态前进程中断")
        return await original_set(version_id, status)

    monkeypatch.setattr(repo.review, "set_version_status", flaky_set_status)
    with pytest.raises(RuntimeError):
        await store.apply_version(draft.id)
    # 中断现场：文件已是草稿正文，库内仍是 v1 applied + 本草稿 draft
    assert store.current() == _NEW
    assert (await store.get_version(draft.id)).status == "draft"
    deps = SimpleNamespace(
        store=store,
        strategy_draft_ids=[draft.id],
        indicator_config_store=None,
        research_prompt_store=None,
        apply_failed_ids=[],
    )
    await apply_drafts(deps)  # _complete_interrupted 的幂等重试路径
    assert deps.apply_failed_ids == []  # 轮末事件 applied=True 名实相符
    assert (await store.get_version(draft.id)).status == "applied"
    latest = await repo.review.latest_applied_strategy_version()
    assert latest is not None and latest.id == draft.id
    assert store.current() == latest.content == _NEW
