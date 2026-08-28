"""复盘侧研报复盘工具测试（issue #113）：候选/案例/历史/提交四工具契约。

覆盖：未读案例提交被拒、LLM 提交 outcome 被拒、依据 1:1 校验（漏/重/越界）、
同轮重复提交更新草稿、K 线来源未配置降级、案例材料完整性与已读登记。
"""

from __future__ import annotations

import json
import time

import pytest

from src.gateway.base import Candle
from src.memory import Database, Repo
from src.review.strategy import StrategyStore
from src.review.tool_handlers import ReviewToolDeps
from src.review.tools import ReviewToolRegistry
from tests.research_helpers import save_report_fixture

_INIT = "初始策略书：" + "稳健交易，控制回撤。" * 10


class _StubCandles:
    """内存 K 线桩：满足 CandleSource 结构协议。"""

    def __init__(self, candles: list[Candle]) -> None:
        """保存固定返回的 K 线列表。

        参数：
            candles: list[Candle]，被调用时返回的 K 线
        """
        self._candles = candles

    def get_candlesticks(
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
    listed = await registry.execute("list_research_review_candidates", {})
    assert f"研报#{report_id}/BTC_USDT" in listed
    assert "方向=偏多" in listed and "horizon=当日" in listed


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
    await registry.execute(
        "get_research_review_case", {"report_id": report_id, "contract": "BTC_USDT"}
    )

    first = await registry.execute("submit_research_review", _valid_review_args(report_id))
    assert "已暂存" in first
    draft = deps.pending_research_reviews[(report_id, "BTC_USDT")]
    assert draft["direction_relation"] == "realized"
    assert draft["direction_reason"] == "窗口内价格上行，与研报方向一致"
    assert json.loads(draft["outcome_json"])["data_status"] == "unavailable"
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
