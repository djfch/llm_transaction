"""src/research/scheduler.py 测试：时间逻辑走可注入 now 的 _tick 与纯函数，不 sleep 60s。

覆盖：到点触发、enabled=False 跳过、未到点跳过、已跑幂等跳过（落库为准）、锁占用跳过、
补跑只补最新一篇（更早盘口不回看）、run_now busy/成功透传/llm 未配置、
美国冬夏令时美盘顺延（纯函数 + tick 两级）、enabled 热开关。

DST 用例注入固定的本地日期（1 月冬令时 / 7 月夏令时）：纽约 DST 判定走绝对时刻换算，
与测试机本地时区无关；预存研报的 created_at 为真实当前时刻，必然落在注入的过往
日期锚点之后，has_report_since 判定为「当日已跑」。
"""

import time

import pytest

from src.config import ResearchConfig, Settings
from src.memory import Database, Repo
from tests.research_helpers import save_report_fixture
from src.research.scheduler import ResearchScheduler, _slot_fire_ts


class StubAgent:
    """记录调用并返回固定结果的 stub（鸭子类型替代 ResearchAgent）。"""

    def __init__(self) -> None:
        """初始化空调用记录，供调度触发断言使用。

        参数：
            无

        返回：
            None：执行测试辅助操作，无返回值
        """
        self.calls: list[dict] = []

    async def run(self, report_type: str = "manual", hours: int = 24) -> dict:
        """记录研报类型与回看时长并返回固定成功结果。

        参数：
            report_type: str，研报或复盘类型
            hours: int，回溯小时数

        返回：
            dict：固定包含成功状态、研报编号、轮次编号与中性方向的结果
        """
        self.calls.append({"report_type": report_type, "hours": hours})
        return {"ok": True, "report_id": 1, "round_id": "r1", "direction": "中性"}


@pytest.fixture
async def repo(tmp_path):
    """创建隔离数据库的仓储夹具。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        AsyncIterator[Repo]：yield 已打开临时数据库的仓储，并在夹具收尾关闭数据库
    """
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


def _settings(enabled: bool = True, us_dst_adjust: bool = True) -> Settings:
    """构造定时器测试所需的设置对象。

    参数：
        enabled: bool，是否启用定时任务
        us_dst_adjust: bool，是否启用美国冬夏令时调整

    返回：
        Settings：三盘口时间固定、开关与冬夏令时参数可调的研报配置
    """
    return Settings(
        research=ResearchConfig(
            enabled=enabled,
            time_asia="08:30",
            time_europe="14:30",
            time_us="21:00",
            us_dst_adjust=us_dst_adjust,
        )
    )


def _local(y: int, m: int, d: int, hh: int, mm: int) -> float:
    """本地某时刻的时间戳（与实现同走 mktime/localtime 口径）。

    参数：
        y: int，本地年份
        m: int，本地月份
        d: int，本地日期
        hh: int，本地小时
        mm: int，本地分钟

    返回：
        float：指定本地日期时间对应的 Unix 时间戳
    """
    return time.mktime((y, m, d, hh, mm, 0, 0, 0, -1))


def _today(hh: int, mm: int) -> float:
    """今日本地 hh:mm 的时间戳。

    参数：
        hh: int，今天的目标小时
        mm: int，今天的目标分钟

    返回：
        float：今天指定时分对应的本地 Unix 时间戳
    """
    lt = time.localtime()
    return _local(lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm)


async def _save_report(repo: Repo, report_type: str) -> None:
    """落库一份成功研报（created_at 为真实当前时刻，供幂等判定）。

    参数：
        repo: Repo，临时数据库仓储夹具
        report_type: str，研报或复盘类型

    返回：
        None：执行测试辅助操作，无返回值
    """
    await save_report_fixture(repo, report_type=report_type, direction="中性", confidence="低")


# ---------- 定时触发 ----------


async def test_tick_fires_asia_after_time(repo):
    """到点（08:30 后）且当日未跑 → 触发亚盘研报（hours=24）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    await scheduler._tick(now=_today(8, 31))
    assert agent.calls == [{"report_type": "asia_open", "hours": 24}]


async def test_tick_skips_before_time(repo):
    """08:29 未到亚盘触发时刻 → 跳过。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    await scheduler._tick(now=_today(8, 29))
    assert agent.calls == []


async def test_tick_skips_when_disabled(repo):
    """研报功能关闭时即使亚盘到点也不调用 Agent。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(enabled=False), agent, repo)
    await scheduler._tick(now=_today(8, 31))
    assert agent.calls == []


async def test_tick_skips_when_already_ran(repo):
    """当日已跑过该盘口（先落库成功研报）→ 不重复触发（重启幂等以落库为准）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    await _save_report(repo, "asia_open")
    await scheduler._tick(now=_today(8, 31))
    assert agent.calls == []


async def test_tick_skips_after_failed_report(repo):
    """当日失败研报也计入幂等：error 非空落库后不自动重跑（防 LLM 故障每分钟重发）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    await save_report_fixture(
        repo, report_type="asia_open", direction="中性", confidence="低", error="LLM 故障"
    )
    await scheduler._tick(now=_today(8, 31))
    assert agent.calls == []


async def test_tick_skips_when_locked(repo):
    """锁被占用（手动触发进行中）→ 巡检跳过，下一分钟再试。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    await scheduler._lock.acquire()
    try:
        await scheduler._tick(now=_today(8, 31))
    finally:
        scheduler._lock.release()
    assert agent.calls == []


async def test_tick_backfills_only_latest_slot(repo):
    """三盘口全过时且都未跑 → 每 tick 最多补一篇，且只补最新的美盘。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    await scheduler._tick(now=_today(23, 30))  # 美盘冬令时 22:00 亦已过
    assert agent.calls == [{"report_type": "us_open", "hours": 24}]


async def test_tick_no_backfill_when_latest_ran(repo):
    """最新到点盘口（美盘）已跑 → 不触发；更早的亚盘/欧盘未跑也不回看不连补。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    await _save_report(repo, "us_open")
    await scheduler._tick(now=_today(23, 30))
    assert agent.calls == []


async def test_tick_enabled_hot_toggle(repo):
    """enabled 每 tick 现读 settings：巡检中途改为 True 立即生效。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    settings = _settings(enabled=False)
    agent = StubAgent()
    scheduler = ResearchScheduler(settings, agent, repo)
    await scheduler._tick(now=_today(8, 31))
    assert agent.calls == []
    settings.research.enabled = True
    await scheduler._tick(now=_today(8, 31))
    assert agent.calls == [{"report_type": "asia_open", "hours": 24}]


# ---------- 手动触发 ----------


async def test_run_now_busy_when_locked(repo):
    """调度锁已占用时 run_now 立即返回 busy 且不调用 Agent。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    await scheduler._lock.acquire()
    try:
        result = await scheduler.run_now()
    finally:
        scheduler._lock.release()
    assert result["started"] is False
    assert result["error_code"] == "busy"  # server 层据此映 409（不判中文文案）
    assert result["error"]  # 文案非空即可
    assert agent.calls == []


async def test_run_now_success_passthrough(repo):
    """正常触发：started=True 且 agent.run 结构化结果原样并入返回，参数透传。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    result = await scheduler.run_now(report_type="us_open", hours=12)
    assert result["started"] is True and result["ok"] is True
    assert result["report_id"] == 1 and result["round_id"] == "r1"
    assert agent.calls == [{"report_type": "us_open", "hours": 12}]


async def test_run_now_default_args(repo):
    """无参 run_now：report_type='manual'、hours=24。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    result = await scheduler.run_now()
    assert result["started"] is True
    assert agent.calls == [{"report_type": "manual", "hours": 24}]


async def test_run_now_llm_not_configured_started_false(repo):
    """agent 回报 llm_not_configured（研报未实际开始）→ started=False（语义诚实）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """

    class _NoLlmAgent:
        async def run(self, report_type: str = "manual", hours: int = 24) -> dict:
            """返回 LLM 未配置结果，模拟研报无法启动。

            参数：
                report_type: str，研报或复盘类型
                hours: int，回溯小时数

            返回：
                dict：携带 llm_not_configured 错误码的失败结果
            """
            return {"ok": False, "error": "任意错误文案", "error_code": "llm_not_configured"}

    scheduler = ResearchScheduler(_settings(), _NoLlmAgent(), repo)
    result = await scheduler.run_now()
    assert result["started"] is False
    assert result["error_code"] == "llm_not_configured"


# ---------- 美国冬夏令时：美盘顺延（纯函数级） ----------


async def test_slot_fire_ts_winter_us_delayed():
    """冬令时（1 月）美盘触发时刻 +1h：本地 21:00 → 22:00。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    ts = _local(2026, 1, 15, 12, 0)
    assert _slot_fire_ts("21:00", ts, us_dst_adjust=True) == _local(2026, 1, 15, 22, 0)


async def test_slot_fire_ts_summer_us_not_delayed():
    """夏令时（7 月）美盘触发时刻不顺延：本地 21:00 不变。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    ts = _local(2026, 7, 15, 12, 0)
    assert _slot_fire_ts("21:00", ts, us_dst_adjust=True) == _local(2026, 7, 15, 21, 0)


async def test_slot_fire_ts_adjust_disabled():
    """us_dst_adjust=False：冬令时也不顺延。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    ts = _local(2026, 1, 15, 12, 0)
    assert _slot_fire_ts("21:00", ts, us_dst_adjust=False) == _local(2026, 1, 15, 21, 0)


# ---------- 美国冬夏令时：美盘顺延（tick 级，注入北京时间戳） ----------


async def test_tick_us_winter_delayed(repo):
    """冬令时（1 月 15 日）：21:30 美盘未触发（顺延至 22:00），22:30 触发。

    亚盘/欧盘预存成功研报（更早盘口本就不回看，预存只为聚焦美盘判定）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    await _save_report(repo, "asia_open")
    await _save_report(repo, "europe_open")
    await scheduler._tick(now=_local(2026, 1, 15, 21, 30))
    assert agent.calls == []
    await scheduler._tick(now=_local(2026, 1, 15, 22, 30))
    assert agent.calls == [{"report_type": "us_open", "hours": 24}]


async def test_tick_us_summer_fires_at_21(repo):
    """夏令时（7 月 15 日）：21:30 美盘已触发（不顺延）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo)
    await _save_report(repo, "asia_open")
    await _save_report(repo, "europe_open")
    await scheduler._tick(now=_local(2026, 7, 15, 21, 30))
    assert agent.calls == [{"report_type": "us_open", "hours": 24}]
