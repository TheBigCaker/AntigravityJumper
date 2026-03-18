"""
CODAI SCHEDULER
Fires tasks at configured times or intervals.

Schedule format (config.json -> "schedule"):
  [
    {
      "id": "morning_check",
      "time": "08:00",           -- daily HH:MM local time
      "days": ["mon","tue","wed","thu","fri"],  -- optional
      "prompt": "Check training status and report any issues.",
      "to": "*",                 -- relay target
      "enabled": true
    },
    {
      "id": "hourly_pulse",
      "interval_minutes": 60,    -- repeat every N minutes
      "prompt": "Quick status ping.",
      "enabled": true
    }
  ]
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

log = logging.getLogger("scheduler")

DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


class ScheduledTask:
    def __init__(self, config: dict):
        self.id = config["id"]
        self.prompt = config.get("prompt", "")
        self.to = config.get("to", "*")
        self.enabled = config.get("enabled", True)

        self.time_str = config.get("time")
        self.days = (
            [DAY_MAP[d.lower()] for d in config.get("days", [])]
            if config.get("days") else None
        )
        self.interval_minutes = config.get("interval_minutes")

        self._last_run: Optional[datetime] = None
        self._next_run: Optional[datetime] = self._calc_next()

    def _calc_next(self) -> Optional[datetime]:
        now = datetime.now()
        if self.interval_minutes:
            return now + timedelta(minutes=self.interval_minutes)
        if self.time_str:
            h, m = map(int, self.time_str.split(":"))
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            if self.days:
                for _ in range(7):
                    if candidate.weekday() in self.days:
                        break
                    candidate += timedelta(days=1)
            return candidate
        return None

    def is_due(self) -> bool:
        if not self.enabled or not self._next_run:
            return False
        return datetime.now() >= self._next_run

    def mark_ran(self):
        self._last_run = datetime.now()
        self._next_run = self._calc_next()
        log.info(f"Task '{self.id}' ran. Next: {self._next_run}")

    def status(self) -> dict:
        return {
            "id": self.id,
            "enabled": self.enabled,
            "time": self.time_str,
            "interval_minutes": self.interval_minutes,
            "days": self.days,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "next_run": self._next_run.isoformat() if self._next_run else None,
            "prompt_preview": self.prompt[:80],
            "to": self.to,
        }


class Scheduler:
    def __init__(self, on_task: Callable, check_interval: float = 10.0):
        self.on_task = on_task
        self.check_interval = check_interval
        self.tasks: Dict[str, ScheduledTask] = {}
        self._running = False

    def load(self, schedule_config: List[dict]):
        self.tasks.clear()
        for cfg in schedule_config:
            if not cfg.get("id"):
                continue
            task = ScheduledTask(cfg)
            self.tasks[task.id] = task
            log.info(f"Scheduled '{task.id}': next run {task._next_run}")

    def add(self, config: dict):
        task = ScheduledTask(config)
        self.tasks[task.id] = task
        return task

    def remove(self, task_id: str):
        self.tasks.pop(task_id, None)

    def enable(self, task_id: str, enabled: bool = True):
        if task_id in self.tasks:
            self.tasks[task_id].enabled = enabled

    def list_status(self) -> list:
        return [t.status() for t in self.tasks.values()]

    async def run(self):
        self._running = True
        log.info(f"Scheduler started. {len(self.tasks)} tasks loaded.")
        while self._running:
            for task in list(self.tasks.values()):
                if task.is_due():
                    log.info(f"Task due: {task.id}")
                    try:
                        if asyncio.iscoroutinefunction(self.on_task):
                            await self.on_task(task)
                        else:
                            self.on_task(task)
                        task.mark_ran()
                    except Exception as e:
                        log.error(f"Task '{task.id}' failed: {e}")
            await asyncio.sleep(self.check_interval)

    def stop(self):
        self._running = False
