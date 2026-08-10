"""价格触发器：LLM 预警线的内存唯一存储、越线检查与触发派发。

不变量：
- 内存即唯一权威——不落库、启动不重建，进程重启后预警线即失效
  （LLM 上下文与 /api/alerts 如实暴露空列表，由 LLM 决定是否重设）。
- 同一触发器最多触发一次（触发即从索引移除，防重复触发）。
- 同一 (contract, direction, price) 的唯一性由工具层经 find() 查重保证，
  索引本身不做唯一性约束（单一事件循环内 find→add 无并发窗口）。
- 未触发预警线全局总数不超 MAX_ALERTS（add() 硬校验，任何调用方不可绕过）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from itertools import count
from typing import Literal

logger = logging.getLogger(__name__)

Direction = Literal[">=", "<="]

# 未触发预警线全局上限（跨合约合并计数）：防预警线无界累积膨胀上下文
MAX_ALERTS = 10


@dataclass(frozen=True)
class PriceTrigger:
    """预警线。direction ">="：价格上穿 price 触发；"<="：下穿触发。"""

    id: int  # 自增数字 id（/api/alerts 响应契约要求数字 id）
    contract: str
    direction: Direction
    price: Decimal
    created_at: float  # 设置时间（Unix 秒）


FireCallback = Callable[[PriceTrigger, Decimal], None]


class TriggerManager:
    """触发器的内存索引（按合约分桶）与检查入口。"""

    def __init__(self, on_fire: FireCallback) -> None:
        """创建触发器管理器：保存触发回调，初始化空的内存索引与自增 id 计数器。

        参数：
            on_fire: FireCallback，预警线触发时的回调，入参为（触发的预警线, 触发时最新价），
                由装配层接线（如触发后抢醒调度器）

        返回：
            None，仅就地初始化内部字段（触发回调、空索引与 id 计数器）
        """
        self._on_fire = on_fire
        self._triggers: dict[int, PriceTrigger] = {}
        self._by_contract: dict[str, set[int]] = {}
        self._next_id: Iterator[int] = count(1)

    def add(self, contract: str, direction: Direction, price: Decimal) -> PriceTrigger:
        """登记一条价格预警线：分配自增 id，写入主索引与合约分桶后返回。

        参数：
            contract: str，合约名（如 BTC_USDT）
            direction: Direction，触发方向，">=" 表示价格上穿触发，"<=" 表示下穿触发
            price: Decimal，触发价（预警线价位）

        返回：
            PriceTrigger：新登记的预警线（含自增 id 与设置时间）

        异常：
            ValueError：direction 不是 ">=" / "<="，或未触发预警线总数已达 MAX_ALERTS 上限
        """
        if direction not in (">=", "<="):
            raise ValueError(f"非法 direction: {direction}（可选 >= / <=）")
        if len(self._triggers) >= MAX_ALERTS:
            raise ValueError(f"价格预警数量已达上限（{MAX_ALERTS} 条），请先取消不需要的预警线")
        trigger = PriceTrigger(
            id=next(self._next_id),
            contract=contract,
            direction=direction,
            price=price,
            created_at=time.time(),
        )
        self._triggers[trigger.id] = trigger
        self._by_contract.setdefault(contract, set()).add(trigger.id)
        return trigger

    def remove(self, trigger_id: int) -> bool:
        """取消预警线。存在并移除返回 True，否则 False。

        参数：
            trigger_id: int，预警线标识
        返回：
            bool，取消预警线。存在并移除返回 True，否则 False
        """
        trigger = self._triggers.pop(trigger_id, None)
        if trigger is None:
            return False
        bucket = self._by_contract.get(trigger.contract)
        if bucket is not None:
            bucket.discard(trigger_id)
            if not bucket:
                del self._by_contract[trigger.contract]
        return True

    def find(self, contract: str, direction: Direction, price: Decimal) -> PriceTrigger | None:
        """按 (contract, direction, price) 精确查找未触发预警线。

        Decimal 数值相等即命中（70000 与 70000.0 视为相同）；只搜同合约分桶。

        参数：
            contract: str，合约标识
            direction: Direction，价格穿越方向
            price: Decimal，委托价格；None 表示市价
        返回：
            PriceTrigger | None，按 (contract, direction, price) 精确查找未触发预警线
        """
        for trigger_id in self._by_contract.get(contract, ()):
            trigger = self._triggers[trigger_id]
            if trigger.direction == direction and trigger.price == price:
                return trigger
        return None

    def list(self, contract: str | None = None) -> list[PriceTrigger]:
        """列出未触发的预警线，可按合约过滤。

        参数：
            contract: str | None，合约标识
        返回：
            list[PriceTrigger]，列出未触发的预警线，可按合约过滤
        """
        if contract is None:
            return list(self._triggers.values())
        ids = self._by_contract.get(contract, set())
        return [self._triggers[i] for i in ids]

    def check(self, contract: str, price: Decimal) -> list[PriceTrigger]:
        """ticker 更新时检查越线：触发的预警线回调后失效，返回触发列表。

        先收集本轮全部命中再逐个回调；单个回调异常记日志后继续派发，
        已移除的触发器保持一次性语义（回调失败也不重发）。

        参数：
            contract: str，合约标识
            price: Decimal，委托价格；None 表示市价
        返回：
            list[PriceTrigger]，ticker 更新时检查越线：触发的预警线回调后失效，返回触发列表
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
        """判断最新价是否越过预警线（触及也算越过）。

        参数：
            trigger: PriceTrigger，待检查的预警线
            price: Decimal，最新价格（ticker 推送价）

        返回：
            bool：direction 为 ">=" 时 price 达到或高于预警价返回 True；
                "<=" 时 price 达到或低于预警价返回 True
        """
        if trigger.direction == ">=":
            return price >= trigger.price
        return price <= trigger.price
