"""复盘侧研报复盘工具测试（issue #113）：候选/案例/历史/提交四工具契约。

覆盖：未读案例提交被拒、LLM 提交 outcome 被拒、依据 1:1 校验（漏/重/越界）、
同轮重复提交更新草稿、K 线来源未配置降级、案例材料完整性与已读登记；
R5-2 人工授权重评分派（无授权拒绝/授权放行 manual 草稿/unreviewable 结案三枚举约束/
结案豁免数据门槛/非结案仍受门槛/窗口到期保留/候选清单尾部待办段）。
"""

from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest

from src.gateway.base import Candle
from src.memory import Database, Repo
from src.review import tool_research as tool_research_module
from src.review.strategy import StrategyStore
from src.review.tool_handlers import ReviewToolDeps
from src.review.tool_research import REASONING_QUALITIES
from src.review.tools import ReviewToolRegistry
from tests.research_helpers import save_report_fixture

_INIT = "初始策略书：" + "稳健交易，控制回撤。" * 10


class _StubCandles:
    """内存 K 线桩：满足 AsyncCandleSource 异步结构协议。"""

    def __init__(self, candles: list[Candle]) -> None:
        """保存固定返回的 K 线列表。

        参数：
            candles: list[Candle]，被调用时返回的 K 线
        """
        self._candles = candles

    async def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """返回固定 K 线列表。

        参数：
            contract: str，合约名
            interval: str，K 线周期
            limit: int | None，最近 N 根
            from_ts: int | None，起始时间戳
            to_ts: int | None，结束时间戳

        返回：
            list[Candle]：初始化时给定的 K 线
        """
        return self._candles


class _WindowCandles:
    """按 from/to 窗口动态生成完整 15m K 线的桩：submit 门禁（F2）需要 complete 客观结果。"""

    async def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """返回覆盖 [from_ts, to_ts) 的连续 15m K 线（纯 limit 查询返回空）。

        参数：
            contract: str，合约名
            interval: str，K 线周期
            limit: int | None，最近 N 根
            from_ts: int | None，窗口起始时间戳
            to_ts: int | None，窗口结束时间戳

        返回：
            list[Candle]：窗口内每 900 秒一根的上行 K 线
        """
        if from_ts is None or to_ts is None:
            return []
        return [
            Candle(
                t=t,
                o=Decimal("100"),
                h=Decimal("110"),
                l=Decimal("90"),
                c=Decimal("105"),
                v=Decimal("1"),
            )
            for t in range(from_ts, to_ts, 900)
        ]


class _PerContractCandles:
    """按合约区分可用性的 K 线桩：empty_contracts 内合约返回空，其余返回完整窗口（R10 测试用）。"""

    def __init__(self, empty_contracts: set[str]) -> None:
        """保存返回空 K 线的合约集合。

        参数：
            empty_contracts: set[str]，模拟行情不可用的合约名集合
        """
        self._empty = empty_contracts

    async def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """空集合内合约或缺窗口参数返回空列表，否则返回窗口内完整 15m K 线。

        参数：
            contract: str，合约名
            interval: str，K 线周期
            limit: int | None，最近 N 根
            from_ts: int | None，窗口起始时间戳
            to_ts: int | None，窗口结束时间戳

        返回：
            list[Candle]：窗口内每 900 秒一根的上行 K 线，或空列表
        """
        if from_ts is None or to_ts is None or contract in self._empty:
            return []
        return [
            Candle(
                t=t,
                o=Decimal("100"),
                h=Decimal("110"),
                l=Decimal("90"),
                c=Decimal("105"),
                v=Decimal("1"),
            )
            for t in range(from_ts, to_ts, 900)
        ]


@pytest.fixture
async def deps(tmp_path) -> ReviewToolDeps:
    """组装复盘工具依赖（临时数据库 + 策略 store + 空 K 线桩）。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        ReviewToolDeps：绑定临时资源的复盘工具依赖（candle_source 默认返回空列表）
    """
    db = Database()
    await db.open(tmp_path / "review_research.db")
    repo = Repo(db)
    prompt = tmp_path / "system_prompt.md"
    prompt.write_text(_INIT, encoding="utf-8")
    store = StrategyStore(prompt, repo)
    await store.seed_if_empty()
    return ReviewToolDeps(repo=repo, store=store, mode="paper", candle_source=_StubCandles([]))


@pytest.fixture
def registry(deps: ReviewToolDeps) -> ReviewToolRegistry:
    """组装复盘工具注册表。

    参数：
        deps: ReviewToolDeps，复盘工具依赖

    返回：
        ReviewToolRegistry：绑定依赖的注册表
    """
    return ReviewToolRegistry(deps)


async def _seed_reviewable_report(deps: ReviewToolDeps) -> int:
    """造一份 horizon=当日、已到期、含两条依据与归一化记录的研报，返回 report_id。

    参数：
        deps: ReviewToolDeps，复盘工具依赖

    返回：
        int：新建研报的编号（created_at 回拨 25 小时使其 horizon 窗口到期）
    """
    raw = {"asset_views": [], "policy_adjustments": ["BTC_USDT: 结构延续结论由高置信降为中置信"]}
    report = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        direction="偏多",
        confidence="中",
        horizon="当日",
        narrative="结构向上。",
        evidence_json=json.dumps(
            [
                {"point": "EMA向上", "source": "市场快照"},
                {"point": "加息预期降温", "source": "金十快讯"},
            ],
            ensure_ascii=False,
        ),
        raw_json=json.dumps(raw, ensure_ascii=False),
        round_id="round-research-1",
    )
    ts = time.time() - 25 * 3600
    await deps.repo._conn.execute(
        "UPDATE research_reports SET created_at=? WHERE id=?", (ts, report.id)
    )
    await deps.repo._conn.execute(
        "UPDATE research_asset_views SET created_at=? WHERE report_id=?", (ts, report.id)
    )
    await deps.repo.start_audit_round(
        "round-research-1", "paper", wake_source="research", context_snapshot="研报轮上下文内容"
    )
    await deps.repo._conn.commit()
    return report.id


def _valid_review_args(report_id: int) -> dict:
    """构造一份合法的 submit_research_review 参数（两条依据评价 1:1 对应）。

    参数：
        report_id: int，目标研报编号

    返回：
        dict：合法的工具参数（枚举评价+理由文本+双枚举逐条依据评价）
    """
    return {
        "report_id": report_id,
        "contract": "BTC_USDT",
        "direction_relation": "realized",
        "direction_reason": "窗口内价格上行，与研报方向一致",
        "reasoning_quality": "sound",
        "reasoning_review": "当时论据提取与表达方法无明显缺陷",
        "evidence_reviews": [
            {
                "evidence_index": 0,
                "fact_status": "confirmed",
                "reasoning_status": "supported",
                "explanation": "市场快照核对：EMA 信号属实",
            },
            {
                "evidence_index": 1,
                "fact_status": "unverifiable",
                "reasoning_status": "unverifiable",
                "explanation": "快讯原文已不可得，无法核实宏观依据",
            },
        ],
        "confidence_assessment": "appropriate",
        "confidence_reason": "中置信与证据强度匹配",
        "improvement_advice": "宏观依据应附可核实出处",
    }


async def test_candidates_empty_then_listed(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """候选工具：无候选时提示；存在到期研报后按契约列出。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言两种状态下的返回文本
    """
    empty = await registry.execute("list_research_review_candidates", {})
    assert "无已到期的研报复盘候选" in empty

    report_id = await _seed_reviewable_report(deps)
    # R10 后候选须经客观数据可用性预检：空 K 线桩会让候选被跳过，换成完整窗口桩
    deps.candle_source = _WindowCandles()
    listed = await registry.execute("list_research_review_candidates", {})
    assert f"研报#{report_id}/BTC_USDT" in listed
    assert "方向=偏多" in listed and "horizon=当日" in listed


async def test_candidates_skip_unavailable_and_page(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """R10：数据不可用候选被跳过并计数；limit=1 时分页扫描取到后续可用候选。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言返回含可用候选行与跳过计数；不可用候选不作为候选行列出
        （仅在跳过说明中点名身份）
    """
    deps.candle_source = _PerContractCandles({"BTC_USDT"})
    # BTC_USDT 到期更早（25h 前）排序靠前但 K 线为空 → unavailable 被跳过；
    # ETH_USDT 到期稍晚（24.5h 前）K 线完整 → complete 被列出
    btc_id = await _seed_reviewable_report(deps)
    eth = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        contract="ETH_USDT",
        direction="偏空",
        confidence="中",
        horizon="当日",
        narrative="结构向下。",
        round_id="round-research-2",
    )
    ts = time.time() - 24.5 * 3600
    await deps.repo._conn.execute(
        "UPDATE research_reports SET created_at=? WHERE id=?", (ts, eth.id)
    )
    await deps.repo._conn.execute(
        "UPDATE research_asset_views SET created_at=? WHERE report_id=?", (ts, eth.id)
    )
    await deps.repo._conn.commit()

    text = await registry.execute("list_research_review_candidates", {"limit": 1})
    assert f"研报#{eth.id}/ETH_USDT" in text
    assert f"- 研报#{btc_id}/BTC_USDT" not in text  # 不作为候选行列出（仅在跳过说明中点名）
    assert "另跳过 1 条" in text


async def test_candidates_skip_unqualified_partial(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """V2：候选预检与提交门禁同口径——不达 partial_acceptable 门槛的 partial 也被跳过。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言缺尾段候选被跳过、身份被列出，且文案只指引留待后续轮次
        （R5-1：不再引导 unreviewable 结案）
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _TruncatedWindowCandles(80)  # 覆盖 82.3% 但尾部缺 16 根（V1 端点约束）
    text = await registry.execute("list_research_review_candidates", {})
    assert "均不达提交门槛" in text
    assert f"研报#{report_id}/BTC_USDT" in text  # 跳过者身份列出
    assert "留待后续轮次" in text
    assert "unreviewable" not in text  # R5-1：自动复盘路径删除结案逃生口引导


async def test_candidates_scan_budget_exhausted_uses_in_memory_lease(
    deps: ReviewToolDeps, registry: ReviewToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6-1：扫描预算用尽时游标只推进内存 lease 不落库；续扫走内存，扫尾也只写内存。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表
        monkeypatch: pytest.MonkeyPatch，把扫描预算钳小为 2 条

    返回：
        None，断言首轮预算提示（不再给 offset）、库中游标未被列出动作触碰、
        内存 lease 推进到第 2 条已预检候选、第二次调用（不传游标参数）从内存
        续扫到第 3 条可用候选、扫到候选集尾部后内存 lease 重置为 None（报告
        成功才由 bundle 事务把游标 ack 落库——本测试不提交报告，库中恒为 None）
    """
    monkeypatch.setattr(tool_research_module, "MAX_CANDIDATE_SCAN", 2)
    # BTC/ETH 数据不可用（空 K 线），SOL 完整可用；到期时刻 BTC 最早、SOL 最晚
    deps.candle_source = _PerContractCandles({"BTC_USDT", "ETH_USDT"})
    await _seed_reviewable_report(deps)
    eth = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        contract="ETH_USDT",
        direction="偏空",
        confidence="中",
        horizon="当日",
        narrative="结构向下。",
        round_id="round-research-2",
    )
    sol = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        contract="SOL_USDT",
        direction="偏多",
        confidence="中",
        horizon="当日",
        narrative="结构向上。",
        round_id="round-research-3",
    )
    for rid, hours in ((eth.id, 24.5), (sol.id, 24.0)):
        ts = time.time() - hours * 3600
        await deps.repo._conn.execute(
            "UPDATE research_reports SET created_at=? WHERE id=?", (ts, rid)
        )
        await deps.repo._conn.execute(
            "UPDATE research_asset_views SET created_at=? WHERE report_id=?", (ts, rid)
        )
    await deps.repo._conn.commit()

    first = await registry.execute("list_research_review_candidates", {"limit": 5})
    assert "扫描预算 2 条已用尽" in first
    assert "已记住续扫位置" in first
    assert "offset" not in first  # R5-3：不再暴露 offset 游标
    assert "SOL_USDT" not in first
    # R6-1：列出只推进内存 lease——库中游标不动（报告成功才由 bundle 事务 ack）；
    # 内存推进到最后一条已预检候选（ETH_USDT 到期晚于 BTC_USDT）
    assert await deps.repo.research_review.get_scan_cursor() is None
    assert deps.scan_cursor is not None
    assert deps.scan_cursor[1] == eth.id and deps.scan_cursor[2] == "ETH_USDT"
    assert deps.scan_tail is False

    # 第二次调用不传任何游标参数：自动从内存 lease 续扫取到 SOL_USDT；
    # 其后无更多候选（批量不足页大小）→ 扫到尾部，内存 lease 重置为 None，库中仍不动
    resumed = await registry.execute("list_research_review_candidates", {"limit": 5})
    assert f"研报#{sol.id}/SOL_USDT" in resumed
    assert "扫描预算" not in resumed
    assert deps.scan_cursor is None and deps.scan_tail is True
    assert await deps.repo.research_review.get_scan_cursor() is None


async def test_get_case_full_material_and_registration(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """案例工具返回原文/依据编号/快照/归一化记录/客观结果，并登记已读缓存。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言材料五要素与 loaded_research_cases 登记内容
    """
    report_id = await _seed_reviewable_report(deps)
    text = await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )

    assert "结构向上" in text
    assert "[0] EMA向上" in text and "[1] 加息预期降温" in text
    assert "研报轮上下文内容" in text
    assert "结构延续结论由高置信降为中置信" in text
    assert "客观行情结果" in text and "unavailable" in text  # K 线桩返回空列表
    case = deps.loaded_research_cases[(report_id, "BTC_USDT")]
    assert case["evidence_count"] == 2
    assert case["outcome"]["data_status"] == "unavailable"

    missing = await registry.execute(
        "get_research_review_case", {"report_id": 999999, "contract": "BTC_USDT"}
    )
    assert "未找到" in missing


async def test_get_case_includes_causal_links_and_window(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """案例材料含当时提交的因果链（只读），已读登记含案例窗口 created_at/window_end。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言因果链段渲染、无链占位与窗口登记字段
    """
    from src.research.payload_v2 import HORIZON_SECONDS

    report_id = await _seed_reviewable_report(deps)
    await deps.repo.research.save_causal_link(
        report_id=report_id,
        chain_json='[{"node": "非农数据"}, {"node": "观望"}]',
        confidence=0.6,
        topic="非农",
    )
    text = await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    assert "当时提交的因果链（只读" in text
    assert "[链#" in text and "[非农]" in text
    assert "非农数据 → 观望" in text
    assert "置信度 0.6" in text

    case = deps.loaded_research_cases[(report_id, "BTC_USDT")]
    report = await deps.repo.research.get_report(report_id)
    assert case["created_at"] == report.created_at
    assert case["window_end"] == report.created_at + HORIZON_SECONDS["当日"]

    # 无因果链的研报显示占位提示（不挂审计轮，避免 round_id 唯一约束冲突）
    other = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        direction="偏空",
        confidence="低",
        horizon="当日",
        narrative="结构转弱。",
    )
    text2 = await registry.execute(
        "get_research_review_case", {"report_id": other.id, "contract": "BTC_USDT"}
    )
    assert "（当时未提交因果链）" in text2


async def test_get_case_shows_report_level_and_prompt_version(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """R6：案例材料含报告级摘要/跨市场观察/全局风险与研报提示词版本归因。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言报告级三行与归因三形态（命中版本 v{id}/未归档标注/无记录占位）
    """
    md5 = "0123456789abcdef0123456789abcdef"
    version = await deps.repo.research_prompt.save_version(
        "研报提示词正文", md5, "human", "初始版本"
    )
    report = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        direction="偏多",
        confidence="中",
        horizon="当日",
        narrative="结构向上。",
        summary="宏观催化确认",
        cross_market_view="BTC 强于 ETH",
        global_risks_json=json.dumps(["流动性偏薄"], ensure_ascii=False),
        research_prompt_md5=md5,
    )
    text = await registry.execute(
        "get_research_review_case", {"report_id": report.id, "contract": "BTC_USDT"}
    )
    assert "报告摘要：宏观催化确认" in text
    assert "跨市场观察：BTC 强于 ETH" in text
    assert "全局风险：流动性偏薄" in text
    assert f"研报提示词版本：v{version.id}（md5 01234567…）" in text

    # md5 有记录但未归档为版本（如人工改文件未入库）→ 标注「未归档版本」
    other = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        direction="偏空",
        confidence="低",
        horizon="当日",
        narrative="结构转弱。",
        research_prompt_md5="ffffffffffffffffffffffffffffffff",
    )
    text2 = await registry.execute(
        "get_research_review_case", {"report_id": other.id, "contract": "BTC_USDT"}
    )
    assert "研报提示词版本：md5 ffffffff…（未归档版本）" in text2

    # 旧数据无 md5 → 占位「无记录」
    legacy = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        direction="中性",
        confidence="低",
        horizon="当日",
        narrative="无归因。",
    )
    text3 = await registry.execute(
        "get_research_review_case", {"report_id": legacy.id, "contract": "BTC_USDT"}
    )
    assert "研报提示词版本：（无记录）" in text3


async def test_get_case_prefers_version_id_then_falls_back_to_md5(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """R5-4：案例归因优先按 research_prompt_version_id；id 失效时回退 md5+时点反解。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言 id 优先（md5 指向别的版本也不篡改）与 id 失效回退两形态
    """
    md5_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    md5_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    version_a = await deps.repo.research_prompt.save_version("版本A正文", md5_a, "human", "A")
    version_b = await deps.repo.research_prompt.save_version("版本B正文", md5_b, "human", "B")
    # id 优先：md5 指向 B 但 version_id 指向 A（构建时点精确归因覆盖 md5 反解）
    by_id = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        narrative="id 优先归因。",
        research_prompt_md5=md5_b,
        research_prompt_version_id=version_a.id,
    )
    text = await registry.execute(
        "get_research_review_case", {"report_id": by_id.id, "contract": "BTC_USDT"}
    )
    assert f"研报提示词版本：v{version_a.id}（md5 bbbbbbbb…）" in text
    # id 失效（版本已被清理）→ 回退 md5 反解出 B
    stale = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        narrative="id 失效回退。",
        research_prompt_md5=md5_b,
        research_prompt_version_id=99999,
    )
    text2 = await registry.execute(
        "get_research_review_case", {"report_id": stale.id, "contract": "BTC_USDT"}
    )
    assert f"研报提示词版本：v{version_b.id}（md5 bbbbbbbb…）" in text2


async def test_get_case_without_candle_source_degrades(deps: ReviewToolDeps) -> None:
    """K 线来源未装配时案例客观结果降级为 unavailable 且明确标注原因。

    参数：
        deps: ReviewToolDeps，复盘工具依赖

    返回：
        None，断言降级文本与 outcome 状态
    """
    deps.candle_source = None
    registry = ReviewToolRegistry(deps)
    report_id = await _seed_reviewable_report(deps)
    text = await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    assert "K线来源未配置" in text
    assert deps.loaded_research_cases[(report_id, "BTC_USDT")]["outcome"]["data_status"] == (
        "unavailable"
    )


async def test_get_case_renders_missing_end_price(deps: ReviewToolDeps) -> None:
    """窗口内只有相交不完整的 K 线：案例文本渲染无完整落窗说明，价格字段全缺（R3）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖

    返回：
        None，断言文本含无完整落窗说明、outcome 为 partial 且价格字段为 None
    """
    # K 线起点在窗口起点前 300 秒：与窗口相交但不完整 → 不参与计算，价格全缺
    deps.candle_source = _StubCandles(
        [
            Candle(
                t=int(time.time() - 25 * 3600) - 300,
                o=Decimal("100"),
                h=Decimal("105"),
                l=Decimal("95"),
                c=Decimal("101"),
                v=Decimal(1),
            )
        ]
    )
    registry = ReviewToolRegistry(deps)
    report_id = await _seed_reviewable_report(deps)
    text = await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    assert "无完整落窗" in text
    outcome = deps.loaded_research_cases[(report_id, "BTC_USDT")]["outcome"]
    assert outcome["data_status"] == "partial"
    assert outcome["start_price"] is None
    assert outcome["end_price"] is None


async def test_submit_requires_loaded_case(registry: ReviewToolRegistry) -> None:
    """未读案例直接提交被拒并提示先读案例。

    参数：
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言拒绝文本与不落草稿
    """
    result = await registry.execute("submit_research_review", _valid_review_args(1))
    assert "请先用 get_research_review_case 读取" in result


async def test_submit_rejects_outcome_field(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """LLM 携带 outcome 字段一律拒绝（客观结果只允许代码附加）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言拒绝文本且草稿未落
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _WindowCandles()
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    args = _valid_review_args(report_id) | {"outcome": {"data_status": "complete"}}
    result = await registry.execute("submit_research_review", args)
    assert "不允许提交该字段" in result
    assert deps.pending_research_reviews == {}


async def test_submit_enforces_evidence_one_to_one(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """依据评价与原研报依据强制 1:1：漏评、越界、重复 evidence_index 均被拒。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言三类违规的拒绝文本
    """

    def _item(index: int, text: str) -> dict:
        """构造一条合法结构的依据评价（仅序号与说明可变）。

        参数：
            index: int，原研报依据序号
            text: str，评价说明

        返回：
            dict：合法结构的依据评价项
        """
        return {
            "evidence_index": index,
            "fact_status": "confirmed",
            "reasoning_status": "supported",
            "explanation": text,
        }

    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _WindowCandles()
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )

    missing = _valid_review_args(report_id)
    missing["evidence_reviews"] = [_item(0, "只评了第一条")]
    assert "一一对应" in await registry.execute("submit_research_review", missing)

    overflow = _valid_review_args(report_id)
    overflow["evidence_reviews"] = [_item(0, "a"), _item(5, "b")]
    assert "一一对应" in await registry.execute("submit_research_review", overflow)

    duplicated = _valid_review_args(report_id)
    duplicated["evidence_reviews"] = [_item(0, "a"), _item(0, "b")]
    assert "一一对应" in await registry.execute("submit_research_review", duplicated)
    assert deps.pending_research_reviews == {}


async def test_reasoning_quality_enum_finalized_set(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """总体推理质量枚举为 issue #113 定稿 sound/partial/flawed/unreviewable（V7 回归）。

    逐条依据层的 reasoning_status 枚举（含 unsupported/unverifiable）不受影响；
    总体层旧取值 unsupported/unverifiable 已非法，定稿取值可正常提交。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言枚举集合、旧取值拒绝与定稿取值 partial 提交成功
    """
    assert set(REASONING_QUALITIES) == {"sound", "partial", "flawed", "unreviewable"}
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _WindowCandles()
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    for stale in ("unsupported", "unverifiable"):
        args = _valid_review_args(report_id) | {"reasoning_quality": stale}
        result = await registry.execute("submit_research_review", args)
        assert "reasoning_quality 取值非法" in result
    ok = await registry.execute(
        "submit_research_review", _valid_review_args(report_id) | {"reasoning_quality": "partial"}
    )
    assert "已暂存" in ok
    assert deps.pending_research_reviews[(report_id, "BTC_USDT")]["reasoning_quality"] == "partial"


async def test_submit_rejects_invalid_enums_and_empty_explanation(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """枚举校验：三个评价维度的非法枚举值与依据评价的空 explanation 均被拒。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言非法取值的拒绝文本且不落草稿
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _WindowCandles()
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )

    bad_direction = _valid_review_args(report_id) | {"direction_relation": "方向一致"}
    result = await registry.execute("submit_research_review", bad_direction)
    assert "direction_relation 取值非法" in result

    bad_reasoning = _valid_review_args(report_id) | {"reasoning_quality": "推理成立"}
    result = await registry.execute("submit_research_review", bad_reasoning)
    assert "reasoning_quality 取值非法" in result

    bad_confidence = _valid_review_args(report_id) | {"confidence_assessment": "匹配"}
    result = await registry.execute("submit_research_review", bad_confidence)
    assert "confidence_assessment 取值非法" in result

    empty_explanation = _valid_review_args(report_id)
    empty_explanation["evidence_reviews"][0]["explanation"] = "  "
    result = await registry.execute("submit_research_review", empty_explanation)
    assert "explanation 必须是非空文本" in result
    assert deps.pending_research_reviews == {}


async def test_submit_stores_draft_and_repeat_updates(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """校验通过暂存草稿（含代码附加的 outcome）；同轮重复提交更新同目标草稿。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言草稿内容、outcome 附加与重复提交的更新语义
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _WindowCandles()
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )

    first = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "已暂存" in first
    draft = deps.pending_research_reviews[(report_id, "BTC_USDT")]
    assert draft["direction_relation"] == "realized"
    assert draft["direction_reason"] == "窗口内价格上行，与研报方向一致"
    assert json.loads(draft["outcome_json"])["data_status"] == "complete"
    evidence = json.loads(draft["evidence_reviews_json"])
    assert [item["evidence_index"] for item in evidence] == [0, 1]
    assert evidence[0]["fact_status"] == "confirmed"
    assert evidence[1]["reasoning_status"] == "unverifiable"

    updated_args = _valid_review_args(report_id) | {"direction_relation": "diverged"}
    second = await registry.execute("submit_research_review", updated_args)
    assert "已更新同目标草稿" in second
    assert len(deps.pending_research_reviews) == 1
    assert deps.pending_research_reviews[(report_id, "BTC_USDT")]["direction_relation"] == (
        "diverged"
    )


async def test_submit_rejects_unavailable_outcome(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """客观结果数据不可用（K 线缺失）时提交被拒（F2：数据不足以支撑批改）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（默认空 K 线桩 → outcome unavailable）
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言拒绝文本且不落草稿
    """
    report_id = await _seed_reviewable_report(deps)
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    result = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "data_status=unavailable" in result
    assert deps.pending_research_reviews == {}


async def test_submit_rejects_not_due(deps: ReviewToolDeps, registry: ReviewToolRegistry) -> None:
    """horizon 窗口未到期时提交被拒（F2 后端自查，不依赖已读缓存的陈旧状态）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言拒绝文本且不落草稿
    """
    report = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        direction="偏多",
        confidence="中",
        horizon="当日",
        narrative="结构向上。",
    )
    await registry.execute(
        "get_research_review_case", {"report_id": report.id, "contract": "BTC_USDT"}
    )
    result = await registry.execute("submit_research_review", _valid_review_args(report.id))
    assert "窗口未到期" in result
    assert deps.pending_research_reviews == {}


async def test_submit_rejects_already_reviewed(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """已被正式复盘批改过的结论重复提交被拒（F2 后端查重）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言拒绝文本且不落草稿
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _WindowCandles()
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    await deps.repo.research_review.save_review(
        review_report_id=999, report_id=report_id, contract="BTC_USDT"
    )
    result = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "已被正式复盘批改过" in result
    assert deps.pending_research_reviews == {}


async def test_submit_rejects_rereview_switch_params(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """已复盘目标重复提交一律拒绝；旧版 LLM 侧 manual_rereview 开关已移除不再生效（R5-1）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言重复提交（含携带旧开关参数）一律拒绝且不落草稿
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _WindowCandles()
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    await deps.repo.research_review.save_review(
        review_report_id=999, report_id=report_id, contract="BTC_USDT"
    )
    rejected = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "已被正式复盘批改过" in rejected
    assert deps.pending_research_reviews == {}
    # 旧版 LLM 侧重评开关不再生效：携带也不放行（V6 开关已移除，R5-1）
    with_switch = await registry.execute(
        "submit_research_review",
        {
            **_valid_review_args(report_id),
            "manual_rereview": True,
            "rereview_reason": "原复盘把震荡误判为背离，人工复核后重评",
        },
    )
    assert "已被正式复盘批改过" in with_switch
    assert deps.pending_research_reviews == {}


class _TruncatedWindowCandles:
    """截尾窗口 K 线桩：只返回窗口前 max_bars 根 15m K 线（模拟行情稀疏）。"""

    def __init__(self, max_bars: int) -> None:
        """保存截断根数。

        参数：
            max_bars: int，窗口内最多返回的 K 线根数

        返回：
            None，仅保存截断配置
        """
        self._max_bars = max_bars

    async def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """返回窗口前 max_bars 根连续 15m K 线。

        参数：
            contract: str，合约名
            interval: str，K 线周期
            limit: int | None，最近 N 根
            from_ts: int | None，窗口起始时间戳
            to_ts: int | None，窗口结束时间戳

        返回：
            list[Candle]：截断后的窗口 K 线
        """
        if from_ts is None or to_ts is None:
            return []
        full = [
            Candle(
                t=t,
                o=Decimal("100"),
                h=Decimal("110"),
                l=Decimal("90"),
                c=Decimal("105"),
                v=Decimal("1"),
            )
            for t in range(from_ts, to_ts, 900)
        ]
        return full[: self._max_bars]


class _GappedWindowCandles:
    """中段断档窗口 K 线桩：满窗口 K 线抽掉指定序号（端点保留，模拟中段稀疏）。"""

    def __init__(self, drop_indexes: set[int]) -> None:
        """保存待抽掉的 K 线序号（自窗口起点起每 900 秒一根的序号）。

        参数：
            drop_indexes: set[int]，要剔除的 K 线序号集合

        返回：
            None，仅保存断档配置
        """
        self._drop = drop_indexes

    async def get_candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[Candle]:
        """返回满窗口 K 线中未被抽掉的部分（序号按 from_ts 起每 900 秒递增）。

        参数：
            contract: str，合约名
            interval: str，K 线周期
            limit: int | None，最近 N 根
            from_ts: int | None，窗口起始时间戳
            to_ts: int | None，窗口结束时间戳

        返回：
            list[Candle]：剔除指定序号后的窗口 K 线
        """
        if from_ts is None or to_ts is None:
            return []
        return [
            Candle(
                t=t,
                o=Decimal("100"),
                h=Decimal("110"),
                l=Decimal("90"),
                c=Decimal("105"),
                v=Decimal("1"),
            )
            for i, t in enumerate(range(from_ts, to_ts, 900))
            if i not in self._drop
        ]


async def test_submit_allows_qualified_partial(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """partial 达标（覆盖 77/96 ≈ 80.2%、起止价齐全、端点贴窗）放行提交（R1）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言提交暂存成功且 outcome 以 partial 落草稿
    """
    report_id = await _seed_reviewable_report(deps)
    # 中段抽掉 18 根（i=40..57）：首根与 created_at 非整点对齐被剔，完整落窗
    # 77 根（77/96 ≥ 80%），两端 K 线保留故价格时点贴窗（V1 端点约束放行）
    deps.candle_source = _GappedWindowCandles(set(range(40, 58)))
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    outcome = deps.loaded_research_cases[(report_id, "BTC_USDT")]["outcome"]
    assert outcome["data_status"] == "partial" and outcome["candles_actual"] == 77
    result = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "已暂存" in result
    draft = deps.pending_research_reviews[(report_id, "BTC_USDT")]
    assert json.loads(draft["outcome_json"])["data_status"] == "partial"


async def test_submit_rejects_sparse_partial(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """partial 过稀（覆盖 9/96 ≈ 9.4% < 80%）拒绝提交并留待后续轮次（R1）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言拒绝文本含覆盖率门槛说明且不落草稿
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _TruncatedWindowCandles(10)  # 剔首根后完整落窗 9 根
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    outcome = deps.loaded_research_cases[(report_id, "BTC_USDT")]["outcome"]
    assert outcome["data_status"] == "partial" and outcome["candles_actual"] == 9
    result = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "data_status=partial" in result and "80%" in result
    assert deps.pending_research_reviews == {}


async def test_submit_rejects_tail_gapped_partial(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """缺尾段的 partial 不得闭合为正常复盘（V1：覆盖 82.3% 达标但尾部缺 16 根 > 端点容忍）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言拒绝文本含数据不足说明、无 unreviewable 结案引导（R5-1），且不落草稿
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _TruncatedWindowCandles(80)  # 剔首根后完整落窗 79 根，尾部缺 16 根
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    outcome = deps.loaded_research_cases[(report_id, "BTC_USDT")]["outcome"]
    assert outcome["data_status"] == "partial" and outcome["candles_actual"] == 79
    result = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "已暂存" not in result
    assert "数据不足" in result and "留待后续轮次" in result
    assert "unreviewable" not in result  # R5-1：自动复盘路径删除结案逃生口引导
    assert deps.pending_research_reviews == {}


async def test_submit_rejects_unreviewable_on_auto_path(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """R5-1：reasoning_quality=unreviewable 不属于自动复盘路径，数据不足也不许结案。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言 unreviewable 提交被拒（即使客观数据确实不足）且不落草稿
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _TruncatedWindowCandles(80)  # 同缺尾场景：数据确实不足
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    args = _valid_review_args(report_id) | {
        "reasoning_quality": "unreviewable",
        "reasoning_review": "窗口尾部行情数据确认不可恢复，无法评价推理兑现",
    }
    result = await registry.execute("submit_research_review", args)
    assert "已暂存" not in result
    assert "unreviewable 不属于自动复盘路径" in result
    assert deps.pending_research_reviews == {}


async def test_submit_recomputes_outcome_at_submit_time(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """R5-1：提交时点用 K 线来源重算客观结果，不信已读案例缓存的旧值（双向验证）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言读案例后 K 线数据变差时提交被拒（缓存 complete 不放行），
        读案例后数据变好时提交放行且草稿 outcome 用重算值
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _WindowCandles()  # 读案例时窗口完整
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    cached = deps.loaded_research_cases[(report_id, "BTC_USDT")]["outcome"]
    assert cached["data_status"] == "complete"

    # 方向一：提交前行情数据变差（尾部缺失）→ 重算不达门槛，拒绝
    deps.candle_source = _TruncatedWindowCandles(80)
    rejected = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "已暂存" not in rejected and "数据不足" in rejected
    assert deps.pending_research_reviews == {}

    # 方向二：提交前行情回补完整 → 重算达标，放行且草稿 outcome 为重算的 complete
    deps.candle_source = _WindowCandles()
    ok = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "已暂存" in ok
    draft = deps.pending_research_reviews[(report_id, "BTC_USDT")]
    assert json.loads(draft["outcome_json"])["data_status"] == "complete"


async def test_submit_rejects_when_candle_source_missing(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """R5-1：K 线来源未装配时提交一律拒绝（不再用已读缓存的 unavailable 走门禁）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言拒绝文本指出 K 线来源未装配且不落草稿
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _WindowCandles()
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    deps.candle_source = None  # 读案例后装配丢失（重启/配置变更场景）
    result = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "K 线来源未装配" in result
    assert deps.pending_research_reviews == {}


async def test_list_research_reviews_returns_full_records(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """历史复盘查询返回完整记录（枚举+理由+逐条依据双枚举+客观结果），支持合约过滤。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言空态提示、完整字段与过滤
    """
    assert "无符合条件" in await registry.execute("list_research_reviews", {})

    await deps.repo.research_review.save_review(
        review_report_id=7,
        report_id=42,
        contract="BTC_USDT",
        direction_relation="realized",
        direction_reason="方向一致，窗口内上行",
        reasoning_quality="sound",
        reasoning_review="推理成立",
        evidence_reviews_json=json.dumps(
            [
                {
                    "evidence_index": 0,
                    "fact_status": "confirmed",
                    "reasoning_status": "supported",
                    "explanation": "快照核对属实",
                }
            ]
        ),
        confidence_assessment="appropriate",
        confidence_reason="匹配合规",
        improvement_advice="继续",
        outcome_json=json.dumps(
            {
                "data_status": "complete",
                "start_price": "100",
                "end_price": "110",
                "high": "115",
                "low": "98",
                "return_pct": "10",
                "max_up_pct": "15",
                "max_down_pct": "-2",
                "candles_actual": 24,
                "candles_expected": 24,
            }
        ),
    )
    text = await registry.execute("list_research_reviews", {"contract": "BTC_USDT"})
    assert "研报#42/BTC_USDT" in text
    assert "realized" in text and "方向一致，窗口内上行" in text
    assert "sound" in text and "推理成立" in text
    assert "appropriate" in text and "匹配合规" in text
    assert "事实=confirmed" in text and "快照核对属实" in text and "涨跌 10%" in text

    none = await registry.execute("list_research_reviews", {"contract": "ETH_USDT"})
    assert "无符合条件" in none


async def test_list_research_reviews_shows_prompt_md5(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """R6：历史复盘行附被复盘研报的提示词 md5 前 8 位归因；无 md5 的研报不展示。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言有 md5 行带归因文本、无 md5 行不带
    """
    md5 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    report = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        direction="偏多",
        confidence="中",
        horizon="当日",
        narrative="结构向上。",
        research_prompt_md5=md5,
    )
    await deps.repo.research_review.save_review(
        review_report_id=9,
        report_id=report.id,
        contract="BTC_USDT",
        direction_relation="realized",
    )
    await deps.repo.research_review.save_review(
        review_report_id=9, report_id=424242, contract="ETH_USDT", direction_relation="diverged"
    )
    text = await registry.execute("list_research_reviews", {})
    assert "研报提示词 md5=aaaaaaaa…" in text
    eth_row = next(line for line in text.splitlines() if "ETH_USDT" in line)
    assert "研报提示词" not in eth_row


def test_review_registry_has_no_causal_link_write(registry) -> None:
    """复盘注册表不含因果链写工具：因果链生命周期归研报侧，复盘侧只读复盘记录。

    参数：
        registry: ReviewToolRegistry，复盘工具注册表夹具

    返回：
        None：断言注册表无 submit_causal_links 等因果链写工具
    """
    names = {spec.name for spec in registry.specs}
    assert "submit_causal_links" not in names
    assert not any("causal" in name for name in names)  # 复盘侧无任何因果链工具


# ---------- 研报提示词版本工具（issue #113 C6） ----------


@pytest.fixture
async def prompt_store(deps: ReviewToolDeps, tmp_path):
    """给 deps 装配研报提示词版本存储（临时文件 + 播种 v1）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（就地挂 research_prompt_store）
        tmp_path: Path，pytest 提供的临时目录

    返回：
        ResearchPromptStore：已播种 v1 的研报提示词存储（deps 已持有引用）
    """
    from src.research.prompt_store import ResearchPromptStore

    path = tmp_path / "research_prompt.md"
    path.write_text("初始研报提示词：" + "先事实后判断。" * 20, encoding="utf-8")
    store = ResearchPromptStore(path, deps.repo)
    await store.seed_if_empty()
    deps.research_prompt_store = store
    return store


async def test_research_prompt_tools_degrade_when_unassembled(
    registry: ReviewToolRegistry,
) -> None:
    """未装配 store 时两个工具返回中文降级提示，不中断本轮复盘。

    参数：
        registry: ReviewToolRegistry，工具注册表（deps 默认无 research_prompt_store）

    返回：
        None，断言降级提示文本
    """
    text = await registry.execute("get_research_prompt_versions", {})
    assert "未装配" in text
    text = await registry.execute(
        "submit_research_prompt_revision",
        {"new_prompt_md": "x" * 200, "reason": "测试"},
    )
    assert "未装配" in text


async def test_submit_research_prompt_revision_draft(
    deps: ReviewToolDeps, registry: ReviewToolRegistry, prompt_store
) -> None:
    """提交修订：校验通过落 draft、deps 记录版本 id 与草稿 id，文件不动。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表
        prompt_store: ResearchPromptStore，已装配的研报提示词存储

    返回：
        None，断言草稿状态、deps 回写与文件未变
    """
    new_prompt = "修订后研报提示词：" + "逐条核对证据。" * 20
    text = await registry.execute(
        "submit_research_prompt_revision",
        {"new_prompt_md": new_prompt, "reason": "研报复盘发现证据门槛过低"},
    )
    assert "草稿 v2" in text and "统一生效" in text
    assert deps.research_prompt_version_id == 2
    assert deps.research_prompt_draft_ids == [2]
    version = await deps.repo.research_prompt.get_version(2)
    assert version is not None and version.status == "draft"
    assert version.created_by == "review_agent"
    assert prompt_store.current().startswith("初始研报提示词")  # 草稿期文件不动


async def test_submit_research_prompt_revision_validation_rejects(
    deps: ReviewToolDeps, registry: ReviewToolRegistry, prompt_store
) -> None:
    """校验拒绝（过短/无差异）返回原因文本，不落版本、不回写 deps。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表
        prompt_store: ResearchPromptStore，已装配的研报提示词存储

    返回：
        None，断言拒绝文案与无副作用
    """
    text = await registry.execute(
        "submit_research_prompt_revision", {"new_prompt_md": "太短", "reason": "x"}
    )
    assert "校验拒绝" in text and "过短" in text
    current = prompt_store.current()
    text = await registry.execute(
        "submit_research_prompt_revision", {"new_prompt_md": current, "reason": "x"}
    )
    assert "校验拒绝" in text and "无差异" in text
    assert deps.research_prompt_version_id is None
    assert deps.research_prompt_draft_ids == []
    assert len(await deps.repo.research_prompt.list_versions()) == 1  # 只有种子 v1


async def test_get_research_prompt_versions(
    deps: ReviewToolDeps, registry: ReviewToolRegistry, prompt_store
) -> None:
    """版本查询：列表含状态/来源/理由并附当前全文；version_id 取详情；不存在给提示。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表
        prompt_store: ResearchPromptStore，已装配的研报提示词存储

    返回：
        None，断言列表、详情与不存在提示三种形态
    """
    text = await registry.execute("get_research_prompt_versions", {})
    assert "研报提示词版本共 1 个" in text and "状态=applied" in text
    assert "当前研报提示词全文" in text and "初始研报提示词" in text
    detail = await registry.execute("get_research_prompt_versions", {"version_id": 1})
    assert "v1" in detail and "全文" in detail
    missing = await registry.execute("get_research_prompt_versions", {"version_id": 99})
    assert "不存在" in missing


# ---------- R5-2：人工授权重评分派（注册表入口在 tool_research_rereview） ----------


async def _seed_reviewed_target(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> tuple[int, int]:
    """造一份已到期、已读案例且已被正式复盘的研报目标，返回 (report_id, 首条复盘记录 id)。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（candle_source 被切换为完整窗口桩）
        registry: ReviewToolRegistry，工具注册表（读案例登记已读缓存）

    返回：
        tuple[int, int]：研报编号与种子复盘记录编号（重评的 rereview_of_id 应指向它）
    """
    report_id = await _seed_reviewable_report(deps)
    deps.candle_source = _WindowCandles()
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )
    seeded = await deps.repo.research_review.save_review(
        review_report_id=999, report_id=report_id, contract="BTC_USDT"
    )
    return report_id, seeded.id


async def test_rereview_rejected_without_authorization(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """已复盘目标无人工授权时提交被拒，文案指引研报详情页授权入口（R5-2）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言拒绝文本与不落草稿
    """
    report_id, _ = await _seed_reviewed_target(deps, registry)
    result = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "已被正式复盘批改过" in result
    assert "人工在研报详情页发起重评授权" in result
    assert deps.pending_research_reviews == {}


async def test_rereview_authorized_stages_manual_draft(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """命中人工授权的重评放行：草稿带 manual 身份、授权理由、替代指向与授权编号（R5-2）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言暂存文案与草稿五要素（review_kind/rereview_reason/rereview_of_id/
        rereview_request_id/重算 outcome）
    """
    report_id, first_review_id = await _seed_reviewed_target(deps, registry)
    req, reused = await deps.repo.research_review.create_rereview_request(
        report_id, "BTC_USDT", "原复盘把震荡误判为背离"
    )
    assert reused is False

    result = await registry.execute("submit_research_review", _valid_review_args(report_id))

    assert "人工授权重评已暂存" in result and f"授权#{req.id}" in result
    draft = deps.pending_research_reviews[(report_id, "BTC_USDT")]
    assert draft["review_kind"] == "manual"
    assert draft["rereview_reason"] == "原复盘把震荡误判为背离"
    assert draft["rereview_of_id"] == first_review_id  # 替代指向被重评的首条记录
    assert draft["rereview_request_id"] == req.id
    assert json.loads(draft["outcome_json"])["data_status"] == "complete"  # 提交时点重算


async def test_rereview_unreviewable_closure_requires_consistent_enums(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """授权重评以 unreviewable 结案时三枚举须一致降级，否则拒绝（R5-2 结案约束）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言结案约束拒绝文本且不落草稿
    """
    report_id, _ = await _seed_reviewed_target(deps, registry)
    await deps.repo.research_review.create_rereview_request(report_id, "BTC_USDT", "结案复核")
    args = _valid_review_args(report_id) | {"reasoning_quality": "unreviewable"}

    result = await registry.execute("submit_research_review", args)

    assert "direction_relation 必须取" in result
    assert "confidence_assessment 必须取 unreviewable" in result
    assert deps.pending_research_reviews == {}


async def test_rereview_unreviewable_closure_bypasses_data_gate(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """unreviewable 结案不套数据门槛：提交时点行情不可用也放行，outcome 落 unavailable（R5-2）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（提交前切回空 K 线桩模拟数据缺口）
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言结案暂存成功且重算 outcome 为 unavailable
    """
    report_id, _ = await _seed_reviewed_target(deps, registry)
    await deps.repo.research_review.create_rereview_request(report_id, "BTC_USDT", "数据缺失结案")
    deps.candle_source = _StubCandles([])  # 结案提交时点行情不可用
    args = _valid_review_args(report_id) | {
        "direction_relation": "unverifiable",
        "reasoning_quality": "unreviewable",
        "confidence_assessment": "unreviewable",
    }

    result = await registry.execute("submit_research_review", args)

    assert "人工授权重评已暂存" in result
    draft = deps.pending_research_reviews[(report_id, "BTC_USDT")]
    assert json.loads(draft["outcome_json"])["data_status"] == "unavailable"


async def test_rereview_non_closure_still_gated_by_data(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """非结案的授权重评仍受数据门槛约束：提交时点数据不足拒绝（R5-2）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（提交前切回空 K 线桩）
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言数据不足拒绝文本且不落草稿
    """
    report_id, _ = await _seed_reviewed_target(deps, registry)
    await deps.repo.research_review.create_rereview_request(report_id, "BTC_USDT", "补评")
    deps.candle_source = _StubCandles([])

    result = await registry.execute("submit_research_review", _valid_review_args(report_id))

    assert "data_status=unavailable" in result
    assert "unreviewable 结案" in result  # 指引改用结案口径
    assert deps.pending_research_reviews == {}


async def test_rereview_still_rejects_not_due(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """授权重评保留窗口到期检查：horizon 未到期即使有授权也拒绝（R5-2）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言窗口未到期拒绝文本且不落草稿
    """
    report = await save_report_fixture(
        deps.repo,
        report_type="us_open",
        direction="偏多",
        confidence="中",
        horizon="当日",
        narrative="结构向上。",
        evidence_json=json.dumps(
            [
                {"point": "EMA向上", "source": "市场快照"},
                {"point": "加息预期降温", "source": "金十快讯"},
            ],
            ensure_ascii=False,
        ),
    )  # 不回拨 created_at：窗口未到期
    await registry.execute(
        "get_research_review_case", {"report_id": report.id, "contract": "BTC_USDT"}
    )
    await deps.repo.research_review.save_review(
        review_report_id=999, report_id=report.id, contract="BTC_USDT"
    )
    await deps.repo.research_review.create_rereview_request(report.id, "BTC_USDT", "过早重评")

    result = await registry.execute("submit_research_review", _valid_review_args(report.id))

    assert "窗口未到期" in result
    assert deps.pending_research_reviews == {}


async def test_candidates_list_shows_pending_rereview_section(
    deps: ReviewToolDeps, registry: ReviewToolRegistry
) -> None:
    """候选清单尾部列出待处理人工授权重评；无授权时无此段（R5-2：授权须对复盘方可见）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表

    返回：
        None，断言授权登记前后清单尾部待办段的出现与内容（已复盘目标不进主候选，
        仅以授权待办形式可见）
    """
    report_id, _ = await _seed_reviewed_target(deps, registry)
    empty = await registry.execute("list_research_review_candidates", {})
    assert "人工授权重评待处理" not in empty

    await deps.repo.research_review.create_rereview_request(report_id, "BTC_USDT", "复核原结论")
    text = await registry.execute("list_research_review_candidates", {})

    assert "人工授权重评待处理 1 条" in text
    assert f"- 研报#{report_id}/BTC_USDT | 授权理由=复核原结论" in text
    assert "发起人=human" in text


async def test_submit_research_prompt_revision_stamps_base_md5(
    deps: ReviewToolDeps, registry: ReviewToolRegistry, prompt_store
) -> None:
    """轮初采样基线写入 deps 后，研报提示词修订草稿盖 base_md5 章且端到端正常生效。

    参数：
        deps: ReviewToolDeps，复盘工具依赖
        registry: ReviewToolRegistry，工具注册表
        prompt_store: ResearchPromptStore，已装配并播种 v1 的研报提示词存储

    返回：
        None，断言草稿版本行 base_md5 等于轮初基线、apply 按 CAS 正路径生效
    """
    v1 = await deps.repo.research_prompt.get_version(1)
    deps.base_md5_by_channel["research_prompt"] = v1.md5  # 模拟轮初采样
    new_prompt = "修订后研报提示词：" + "逐条核对证据。" * 20
    text = await registry.execute(
        "submit_research_prompt_revision",
        {"new_prompt_md": new_prompt, "reason": "研报复盘发现证据门槛过低"},
    )
    assert "草稿 v2" in text
    version = await deps.repo.research_prompt.get_version(2)
    assert version is not None and version.base_md5 == v1.md5
    applied = await prompt_store.apply_version(2)
    assert applied is not None and applied.status == "applied"
    assert prompt_store.current() == new_prompt
