"""唤醒调度：定时唤醒（LLM 自设间隔）+ 外部抢醒，单轮防重入。"""

from .wakeup import WakeupScheduler

__all__ = ["WakeupScheduler"]
