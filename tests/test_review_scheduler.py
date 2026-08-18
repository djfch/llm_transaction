"""src/review/scheduler.py 测试：时间逻辑走可注入 now 的 _tick 与纯函数，不 sleep 60s。

覆盖：到点触发（enabled 且 latest None）、间隔内不重复（latest 距今不足 interval_days）、
disabled 跳过、未到点跳过、锁占用时巡检跳过、巡检持锁时 start_now 同步 busy 不排队、
start_now 点火即返回（后台任务执行）、锁占用/未配置 LLM/区间非法的同步失败路径、
断连取消不变量、shutdown 取消（桩级取消语义 + 真实 ReviewAgent 取消收尾副作用）、
点火后首次执行前 shutdown 的取消窗口（锁/预留无泄漏）、任务跑完释放锁与预留。
"""

import asyncio
import time

import pytest

from src.audit.trail import AuditTrail
from src.config import AuditConfig, ReviewConfig, Settings
from src.memory import Database, Repo
from src.review.agent import ReviewAgent
from src.review.prompts import ReviewPromptLoader
from src.review.scheduler import ReviewScheduler, daily_fire_ts, local_day_start
from src.review.strategy import StrategyStore


class StubAgent:
    """记录调用区间并返回固定结果的 stub（鸭子类型替代 ReviewAgent）。"""

    def __init__(self) -> None:
        """初始化空调用记录列表与已配置 LLM 标记。

        参数：无

        返回：
            None，就地初始化 calls 为空列表，供后续断言调用区间
        """
        self.calls: list[tuple[float, float]] = []
        self.round_ids: list[str | None] = []  # 每次 run 收到的预分配 round_id（自动巡检为 None）
        self.llm_configured = True

    async def run(
        self, period_start: float, period_end: float, *, round_id: str | None = None
    ) -> dict:
        """记录本次调用区间与预分配轮次编号并返回固定的成功结果。

        参数：
            period_start: float，复盘区间起点（Unix 时间戳）
            period_end: float，复盘区间终点（Unix 时间戳）
            round_id: str | None，调度器手动点火时预分配的审计轮次编号

        返回：
            dict：固定为 {"ok": True, "report_id": 1} 的成功结果
        """
        self.calls.append((period_start, period_end))
        self.round_ids.append(round_id)
        return {"ok": True, "report_id": 1}


@pytest.fixture
async def repo(tmp_path):
    """构造指向临时数据库的 Repo 实例，测试结束后关闭数据库。

    参数：
        tmp_path: Path，pytest 临时目录夹具，测试数据库文件落在其中

    返回：
        AsyncIterator[Repo]，yield 已打开临时数据库的仓储对象，测试结束后关闭连接
    """
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


def _settings(enabled: bool = True, interval_days: int = 1) -> Settings:
    """构造复盘调度测试用的 Settings（daily_time 固定为 03:00）。

    参数：
        enabled: bool，复盘功能开关，默认开启
        interval_days: int，复盘间隔天数，默认 1 天

    返回：
        Settings：仅定制复盘配置、其余走默认的应用配置对象
    """
    return Settings(
        review=ReviewConfig(enabled=enabled, daily_time="03:00", interval_days=interval_days)
    )


def _anchors() -> tuple[float, float]:
    """当日00:00 与 03:00（触发时刻）。与实现走同一对纯函数，期望区间手工推导。

    参数：
        无

    返回：
        tuple[float, float]：当日本地零点与 03:00 触发时刻的时间戳
    """
    now = time.time()
    return local_day_start(now), daily_fire_ts("03:00", now)


async def test_tick_fires_after_daily_time(repo):
    """到点且从未复盘（latest None）→ 触发「昨日00:00 ~ 当日00:00」区间。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 86400, day_start)]
    assert agent.round_ids == [None]  # 自动巡检不预分配轮次编号（仅手动点火预分配）


async def test_tick_skips_before_daily_time(repo):
    """未到当日触发时刻（02:59:59）→ 巡检不触发复盘，agent 零调用。

    参数：
        repo: Repo，临时数据库仓储夹具，提供空的复盘报告存储

    返回：
        None，断言 agent.calls 为空，且触发时刻确为当日 03:00
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await scheduler._tick(now=fire - 1)  # 02:59:59 未到点
    assert agent.calls == []
    assert fire - day_start == 3 * 3600  # 触发时刻确为当日 03:00


async def test_tick_skips_when_already_reviewed(repo):
    """当日已跑（latest=当日00:00）→ 不重复触发（重启幂等以落库为准）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(day_start - 86400, day_start, "{}", "", "none")
    await scheduler._tick(now=fire + 1)
    assert agent.calls == []


async def test_tick_skips_when_disabled(repo):
    """复盘功能关闭（enabled=False）→ 即使已过触发时刻也不触发，agent 零调用。

    参数：
        repo: Repo，临时数据库仓储夹具，提供空的复盘报告存储

    返回：
        None，断言 agent.calls 为空（disabled 时 _tick 直接跳过）
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(enabled=False), agent, repo)
    _, fire = _anchors()
    await scheduler._tick(now=fire + 1)
    assert agent.calls == []


async def test_tick_skips_when_locked(repo):
    """锁被占用（手动触发进行中）→ 巡检跳过，下一分钟再试。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    _, fire = _anchors()
    await scheduler._lock.acquire()
    try:
        await scheduler._tick(now=fire + 1)
    finally:
        scheduler._lock.release()
    assert agent.calls == []


async def test_start_now_busy_when_locked(repo):
    """调度锁被占用时 start_now 不等待、同步返回 busy，不点火后台任务。

    参数：
        repo: Repo，临时数据库仓储夹具，提供复盘报告存储

    返回：
        None，断言 started=False、error_code=busy、错误文案非空且 agent 零调用
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    await scheduler._lock.acquire()
    try:
        result = await scheduler.start_now()
    finally:
        scheduler._lock.release()
    assert result["started"] is False
    assert result["error_code"] == "busy"  # server 层据此映 409（不判中文文案）
    assert result["error"]  # 文案非空即可
    assert scheduler._manual_task is None
    assert agent.calls == []


async def test_start_now_llm_not_configured_started_false(repo):
    """agent 未配置 LLM → start_now 同步返回 llm_not_configured（server 层映 503），不点火。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：断言 started=False、error_code=llm_not_configured 且 agent 零调用
    """
    agent = StubAgent()
    agent.llm_configured = False
    scheduler = ReviewScheduler(_settings(), agent, repo)
    result = await scheduler.start_now()
    assert result["started"] is False
    assert result["error_code"] == "llm_not_configured"
    assert scheduler._manual_task is None
    assert agent.calls == []


async def test_start_now_runs_yesterday_period(repo):
    """start_now 无参点火：立即返回 started=True，后台任务跑「昨日00:00 ~ 当日00:00」区间。

    参数：
        repo: Repo，临时数据库仓储夹具，提供复盘报告存储

    返回：
        None，断言点火契约与后台任务完成后的 agent 调用区间
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start = local_day_start(time.time())
    result = await scheduler.start_now()
    round_id = result.pop("round_id")  # 预分配轮次编号：单独断言，余键严格等值
    assert isinstance(round_id, str) and len(round_id) == 32
    assert result == {
        "started": True,
        "period_start": day_start - 86400,
        "period_end": day_start,
    }
    assert agent.calls == []  # 点火返回时后台任务尚未执行
    assert scheduler._manual_task is not None
    await asyncio.wait_for(scheduler._manual_task, timeout=1)
    assert agent.calls == [(day_start - 86400, day_start)]
    assert agent.round_ids == [round_id]  # 后台 agent.run 收到同一轮次身份
    assert not scheduler._lock.locked()  # 后台任务收尾释放锁


# ---------- 人工补跑指定区间 ----------


async def test_start_now_with_explicit_period_passthrough(repo):
    """人工补跑：指定区间原样透传到后台 agent.run（不走昨日区间）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：断言点火回显区间与后台任务完成后的 agent 调用区间
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    result = await scheduler.start_now(period_start=1000.0, period_end=2000.0)
    round_id = result.pop("round_id")  # 预分配轮次编号：单独断言，余键严格等值
    assert isinstance(round_id, str) and len(round_id) == 32
    assert result == {"started": True, "period_start": 1000.0, "period_end": 2000.0}
    assert scheduler._manual_task is not None
    await asyncio.wait_for(scheduler._manual_task, timeout=1)
    assert agent.calls == [(1000.0, 2000.0)]
    assert agent.round_ids == [round_id]  # 后台 agent.run 收到同一轮次身份


async def test_start_now_invalid_period(repo):
    """非法区间（start>=end、非数字、只给一端、bool）→ 同步 started=False +
    error_code=invalid_period（server 层映 422），不点火后台任务。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    bad_periods = [
        (2000.0, 1000.0),
        (1000.0, 1000.0),
        ("x", 2000.0),
        (1000.0, None),
        (True, 2000.0),
    ]
    for start, end in bad_periods:
        result = await scheduler.start_now(period_start=start, period_end=end)
        assert result["started"] is False, (start, end)
        assert result["error_code"] == "invalid_period", (start, end)
    assert scheduler._manual_task is None
    assert agent.calls == []


class BlockingAgent:
    """run 挂起直至测试释放的复盘 Agent 边界桩（模拟生成进行中）。"""

    def __init__(self) -> None:
        """初始化调用记录、开始与释放事件。

        参数：无

        返回：
            None：就地初始化事件与已配置 LLM 标记
        """
        self.calls: list[tuple[float, float]] = []
        self.llm_configured = True
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(
        self, period_start: float, period_end: float, *, round_id: str | None = None
    ) -> dict:
        """记录调用区间、标记开始并挂起，直到测试释放后返回成功结果。

        参数：
            period_start: float，复盘区间起点（Unix 时间戳）
            period_end: float，复盘区间终点（Unix 时间戳）
            round_id: str | None，调度器手动点火时预分配的审计轮次编号（本桩不读取）

        返回：
            dict：固定成功结果
        """
        self.calls.append((period_start, period_end))
        self.started.set()
        await self.release.wait()
        return {"ok": True, "report_id": 1}


class HangingProvider:
    """chat 挂起的 provider 桩（模拟真实 ReviewAgent 生成进行中，供 shutdown 取消回归）。"""

    def __init__(self) -> None:
        """初始化进入事件。

        参数：无

        返回：
            None：就地初始化 entered 事件，供测试等待 chat 真正开始
        """
        self.entered = asyncio.Event()

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        """标记进入后挂起，直至任务被取消（永不正常返回）。

        参数：
            system: str，传给 LLM 的系统提示词
            messages: list[dict]，传给 LLM 的消息历史
            tools: list[dict]，传给 LLM 的工具定义

        返回：
            dict，永不返回；挂起只能被外部取消打断
        """
        self.entered.set()
        await asyncio.sleep(60)  # 远超测试等待，仅取消可打断

    def tool_result_message(self, call: object, result: str) -> dict:
        """把工具调用结果封装为模拟提供商消息（本桩不会走到）。

        参数：
            call: object，待封装的工具调用
            result: str，工具执行结果文本

        返回：
            dict，包含 role=tool 与结果内容的工具消息
        """
        return {"role": "tool", "content": result}


async def test_tick_holding_lock_makes_start_now_busy(repo):
    """巡检持锁进行中时 start_now 同步返回 busy，不点火也不排队等锁（竞态回归）。

    与 research 侧 test_auto_tick_claims_execution_before_first_await 对称：
    _tick 在锁内执行 agent.run 期间，手动入口必须在首个 await 前看到锁已占用。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None，断言 busy 同步返回、无后台任务且巡检调用仅发生一次
    """
    agent = BlockingAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    tick_task = asyncio.create_task(scheduler._tick(now=fire + 1))
    await agent.started.wait()  # 巡检已持锁进入 agent.run

    result = await scheduler.start_now()
    agent.release.set()
    await tick_task

    assert result["started"] is False
    assert result["error_code"] == "busy"
    assert scheduler._manual_task is None
    assert agent.calls == [(day_start - 86400, day_start)]  # 仅巡检那一次，未排队补跑


async def test_start_now_background_survives_caller_cancellation(repo):
    """取消不变量（断连回归）：点火后取消调用方任务，后台复盘仍跑完。

    模拟 HTTP 断连：调用方协程点火后挂起（如保持连接），被取消时后台任务不受影响。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：断言调用方被取消而后台复盘执行到底
    """
    agent = BlockingAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    outcome: dict = {}

    async def http_like_call() -> None:
        """模拟请求处理：点火后挂起等待连接生命周期结束。

        参数：无

        返回：
            None：点火结果写入 outcome 后永久挂起，直至被取消
        """
        outcome["result"] = await scheduler.start_now(period_start=1000.0, period_end=2000.0)
        await asyncio.Event().wait()  # 模拟响应发送/连接保持：挂起直到断连取消

    request = asyncio.create_task(http_like_call())
    await agent.started.wait()
    fired = dict(outcome["result"])
    round_id = fired.pop("round_id")  # 预分配轮次编号：单独断言，余键严格等值
    assert isinstance(round_id, str) and len(round_id) == 32
    assert fired == {
        "started": True,
        "period_start": 1000.0,
        "period_end": 2000.0,
    }
    request.cancel()  # 浏览器断连：请求任务被取消
    with pytest.raises(asyncio.CancelledError):
        await request

    assert scheduler._manual_task is not None and not scheduler._manual_task.done()
    agent.release.set()
    await asyncio.wait_for(scheduler._manual_task, timeout=1)
    assert agent.calls == [(1000.0, 2000.0)]
    assert not scheduler._lock.locked()


async def test_shutdown_cancels_running_manual_task(repo, caplog: pytest.LogCaptureFixture):
    """shutdown 取消进行中的后台任务：任务以取消态收尾、释放锁且无未捕获噪音。

    参数：
        repo: Repo，临时数据库仓储夹具
        caplog: pytest.LogCaptureFixture，日志捕获夹具

    返回：
        None：断言任务呈取消态、锁释放且取消日志留痕
    """
    agent = BlockingAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    fired = await scheduler.start_now()
    await agent.started.wait()
    task = scheduler._manual_task
    assert task is not None

    with caplog.at_level("INFO", logger="src.review.scheduler"):
        await scheduler.shutdown()

    assert task.done()
    assert task.cancelled()  # 取消语义保留：_run_manual 原样传播，shutdown gather 取回
    assert not scheduler._lock.locked()
    assert "手动复盘后台任务被取消" in caplog.text
    # 补记钉住：BlockingAgent 桩不触库（无审计轮、无报告），shutdown 补记唯一的取消终态；
    # 生产真实 agent 首个 DB 写即 begin_round，补记的审计轮判重闸会拦截、不会走到这
    reports, total = await repo.review.list_review_reports_page(10, 0)
    assert total == 1
    assert reports[0].error == "手动复盘在开始执行前被关机取消"
    assert reports[0].round_id == fired["round_id"]
    found = await repo.review.find_report_by_round_id(fired["round_id"])
    assert found is not None and found.id == reports[0].id
    await scheduler.shutdown()  # 幂等：无进行中任务时立即返回


async def test_shutdown_before_manual_task_first_execution(repo):
    """点火后不等待任何执行立即 shutdown：锁从未持有、预留由 done 回调清理，且补记取消终态。

    后台任务在首次执行前被取消时协程体不进入；旧实现锁由调用方任务持有后跨任务转移，
    该窗口下 finally 不执行 → 锁永久 locked、之后点火永远 busy。此外 begin_round 从未
    运行、agent 未留痕，shutdown 须为预分配轮次补写一条「关机取消」失败报告；
    正常执行完的轮 shutdown 早退，不产生额外记录。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：断言任务取消态、锁未持有、预留清除、补记取消终态、再次点火正常
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    result = await scheduler.start_now()
    assert result["started"] is True
    assert scheduler._manual_reserved is True  # 点火即置预留
    second = await scheduler.start_now()  # 预留期间再点火：同步 busy，不排队
    assert second["started"] is False and second["error_code"] == "busy"

    await scheduler.shutdown()

    task = scheduler._manual_task
    assert task is not None and task.cancelled()  # 首次执行前被取消，协程体未进入
    assert agent.calls == []
    assert not scheduler._lock.locked()  # 锁从未持有，无泄漏
    assert scheduler._manual_reserved is False  # done 回调清理预留
    # 关机补记取消终态：预分配轮次留一条失败报告（begin_round 从未运行，agent 未留痕）
    reports, total = await repo.review.list_review_reports_page(10, 0)
    assert total == 1
    assert reports[0].error == "手动复盘在开始执行前被关机取消"
    assert reports[0].round_id == result["round_id"]
    assert (reports[0].period_start, reports[0].period_end) == (
        result["period_start"],
        result["period_end"],
    )
    again = await scheduler.start_now()  # 预留已清：可再次正常点火
    assert again["started"] is True
    await asyncio.wait_for(scheduler._manual_task, timeout=1)
    assert len(agent.calls) == 1
    assert not scheduler._lock.locked()
    # 正常执行完的轮（StubAgent 不落库）：任务已 done，shutdown 早退不产生额外记录
    await scheduler.shutdown()
    reports, total = await repo.review.list_review_reports_page(10, 0)
    assert total == 1


async def test_manual_task_completion_releases_lock_and_reservation(repo):
    """点火后等任务真正跑完：锁释放、预留清除。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：断言后台任务完成后 agent 调用发生且锁与预留均已释放
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start = local_day_start(time.time())
    await scheduler.start_now()
    assert scheduler._manual_reserved is True
    await asyncio.wait_for(scheduler._manual_task, timeout=1)
    assert agent.calls == [(day_start - 86400, day_start)]
    assert not scheduler._lock.locked()
    assert scheduler._manual_reserved is False


async def test_shutdown_cancel_finishes_real_agent_cleanup(repo, tmp_path):
    """shutdown 取消进行中手动复盘：真实 ReviewAgent 完成取消收尾（B1 回归）。

    全链路：start_now 点火 → provider 挂起 → shutdown 取消 → 断言 ①error 报告落库、
    ②审计轮 ended_at 非空且 error 含 CancelledError、③事件序列以 review_round ok=False 收尾。

    参数：
        repo: Repo，临时数据库仓储夹具
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言取消态与失败收尾副作用同时成立
    """
    prompt_file = tmp_path / "system_prompt.md"
    prompt_file.write_text("初始策略书", encoding="utf-8")
    store = StrategyStore(prompt_file, repo)
    await store.seed_if_empty()
    review_prompt = tmp_path / "review_prompt.md"
    review_prompt.write_text("# 复盘纪律", encoding="utf-8")
    events: list[dict] = []
    settings = _settings()
    provider = HangingProvider()
    agent = ReviewAgent(
        settings=settings,
        provider=provider,
        repo=repo,
        audit=AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit"))),
        store=store,
        prompt_loader=ReviewPromptLoader(review_prompt),
        notify_event=lambda payload: events.append(payload),
    )
    scheduler = ReviewScheduler(settings, agent, repo)
    fired = await scheduler.start_now(period_start=1000.0, period_end=2000.0)
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    task = scheduler._manual_task
    assert task is not None

    await scheduler.shutdown()

    assert task.cancelled()
    assert not scheduler._lock.locked()
    reports, total = await repo.review.list_review_reports_page(10, 0)
    assert total == 1
    assert reports[0].error == "CancelledError: 复盘被取消"
    round_row = await repo.latest_audit_round("paper")
    assert round_row is not None and round_row.ended_at is not None
    assert round_row.error == "CancelledError: 复盘被取消"
    assert round_row.round_id == fired["round_id"]  # 预分配身份落到真实审计轮
    assert [e["type"] for e in events] == ["review_round_start", "review_round"]
    assert events[-1]["data"] == {"round_id": round_row.round_id, "ok": False}


# ---------- interval_days：指定间隔天数复盘 ----------


async def test_tick_interval_not_reached_skips(repo):
    """interval_days=3：距上次复盘仅 2 天 → 未满间隔，不触发。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(
        day_start - 5 * 86400, day_start - 2 * 86400, "{}", "", "none"
    )
    await scheduler._tick(now=fire + 1)
    assert agent.calls == []


async def test_tick_latest_not_day_aligned(repo):
    """人工补跑的 period_end 非日对齐（昨日 12:00）：幂等判定按自然日计，今日照常触发。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(
        day_start - 86400, day_start - 43200, "{}", "", "none"
    )  # 昨日 12:00 结束的补跑区间
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 86400, day_start)]


async def test_tick_interval_reached_fires_span(repo):
    """interval_days=3：距上次复盘恰满 3 天 → 触发，且区间为最近 3 天。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(
        day_start - 6 * 86400, day_start - 3 * 86400, "{}", "", "none"
    )
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 3 * 86400, day_start)]


async def test_tick_interval_first_run_uses_span(repo):
    """interval_days=3 且从未复盘（latest None）→ 首次即按 3 天区间触发。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start, fire = _anchors()
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 3 * 86400, day_start)]


async def test_start_now_uses_interval_span(repo):
    """start_now 无参：默认区间同步为最近 interval_days 天，后台任务按该区间执行。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：断言点火回显区间与后台任务完成后的 agent 调用区间
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start = local_day_start(time.time())
    result = await scheduler.start_now()
    round_id = result.pop("round_id")  # 预分配轮次编号：单独断言，余键严格等值
    assert isinstance(round_id, str) and len(round_id) == 32
    assert result == {
        "started": True,
        "period_start": day_start - 3 * 86400,
        "period_end": day_start,
    }
    assert scheduler._manual_task is not None
    await asyncio.wait_for(scheduler._manual_task, timeout=1)
    assert agent.calls == [(day_start - 3 * 86400, day_start)]
