"""价格触发器：LLM 预警线的内存索引、越线检查与 alerts 表生命周期接线。

不变量：同一触发器最多触发一次（触发即从索引移除，防重复触发）。
本模块不 import 存储层实现（Repo 仅 TYPE_CHECKING 类型依赖），持久化由接线辅助完成：
- agent 工具层 set_price_alert：落 alerts 行 + add 内存触发器（双写入口）
- make_fire_callback：触发即把对应告警行置 active=0（不永久悬挂），并抢醒调度器
- rebuild_from_repo：启动时把 active=1 的告警行重建为内存触发器（重启预警不丢）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..memory.repo import Repo  # 仅类型注解：保持 market 运行期不依赖存储层

logger = logging.getLogger(__name__)

Direction = Literal[">=", "<="]


@dataclass(frozen=True)
class PriceTrigger:
    """预警线。direction ">="：价格上穿 price 触发；"<="：下穿触发。"""

    id: str
    contract: str
    direction: Direction
    price: Decimal


FireCallback = Callable[[PriceTrigger, Decimal], None]
WakeNowFn = Callable[[str], bool]


class TriggerManager:
    """触发器的内存索引（按合约分桶）与检查入口。"""

    def __init__(self, on_fire: FireCallback) -> None:
        self._on_fire = on_fire
        self._triggers: dict[str, PriceTrigger] = {}
        self._by_contract: dict[str, set[str]] = {}

    def add(self, contract: str, direction: Direction, price: Decimal) -> PriceTrigger:
        if direction not in (">=", "<="):
            raise ValueError(f"非法 direction: {direction}（可选 >= / <=）")
        trigger = PriceTrigger(
            id=f"trig-{uuid.uuid4().hex[:12]}",
            contract=contract,
            direction=direction,
            price=price,
        )
        self._triggers[trigger.id] = trigger
        self._by_contract.setdefault(contract, set()).add(trigger.id)
        return trigger

    def remove(self, trigger_id: str) -> bool:
        """取消预警线。存在并移除返回 True，否则 False。"""
        trigger = self._triggers.pop(trigger_id, None)
        if trigger is None:
            return False
        bucket = self._by_contract.get(trigger.contract)
        if bucket is not None:
            bucket.discard(trigger_id)
            if not bucket:
                del self._by_contract[trigger.contract]
        return True

    def list(self, contract: str | None = None) -> list[PriceTrigger]:
        """列出未触发的预警线，可按合约过滤。"""
        if contract is None:
            return list(self._triggers.values())
        ids = self._by_contract.get(contract, set())
        return [self._triggers[i] for i in ids]

    def check(self, contract: str, price: Decimal) -> list[PriceTrigger]:
        """ticker 更新时检查越线：触发的预警线回调后失效，返回触发列表。

        先收集本轮全部命中再逐个回调；单个回调异常记日志后继续派发，
        已移除的触发器保持一次性语义（回调失败也不重发）。
        """
        fired = [
            self._triggers[i]
            for i in self._by_contract.get(contract, ())
            if self._crossed(self._triggers[i], price)
        ]
        for trigger in fired:
            self.remove(trigger.id)  # 先移除再回调：回调抛异常也不破坏一次性语义
            try:
                self._on_fire(trigger, price)
            except Exception:
                logger.exception(
                    "触发器回调异常（%s %s %s）",
                    trigger.contract,
                    trigger.direction,
                    trigger.price,
                )
        return fired

    @staticmethod
    def _crossed(trigger: PriceTrigger, price: Decimal) -> bool:
        if trigger.direction == ">=":
            return price >= trigger.price
        return price <= trigger.price


# ---------- alerts 表生命周期接线（由主程序组装时注入） ----------


def make_fire_callback(repo: Repo, wake_now: WakeNowFn) -> FireCallback:
    """生成触发回调：先把对应 active 告警行持久化关闭（best-effort），再抢醒调度器。

    alerts 行由工具层按 (contract, above/below, price) 写入，触发器方向与之映射
    （">=" ↔ above，"<=" ↔ below）；持久化失败只记日志，不影响内存触发语义。
    """

    async def _deactivate(trigger: PriceTrigger) -> None:
        try:
            direction = "above" if trigger.direction == ">=" else "below"
            for alert in await repo.list_alerts(active_only=True):
                if (alert.contract, alert.direction, alert.price) == (
                    trigger.contract,
                    direction,
                    trigger.price,
                ):
                    await repo.deactivate_alert(alert.id)
        except Exception:
            logger.exception("告警持久化关闭失败（%s）", trigger.contract)

    def on_fire(trigger: PriceTrigger, price: Decimal) -> None:
        try:
            asyncio.get_running_loop().create_task(_deactivate(trigger))
        except RuntimeError:
            pass  # 无运行中事件循环（纯同步用法）：跳过持久化，内存触发语义不受影响
        wake_now(f"price_trigger:{trigger.contract}@{price}")

    return on_fire


async def rebuild_from_repo(repo: Repo, manager: TriggerManager) -> int:
    """启动重建：把 DB 中 active=1 的告警行恢复为内存触发器，返回重建数量。"""
    count = 0
    for alert in await repo.list_alerts(active_only=True):
        if alert.direction not in ("above", "below"):
            logger.warning("跳过未知方向的告警行 id=%s direction=%s", alert.id, alert.direction)
            continue
        manager.add(alert.contract, ">=" if alert.direction == "above" else "<=", alert.price)
        count += 1
    return count
