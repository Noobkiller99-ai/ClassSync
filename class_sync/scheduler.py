from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Callable


class WeeklyScheduler:
    def __init__(self, job: Callable[[], None], weekday: int = 0, hour: int = 2, minute: int = 0):
        self.job = job
        self.weekday = weekday
        self.hour = hour
        self.minute = minute
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            now = datetime.now()
            run_at = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
            days_until = (self.weekday - run_at.weekday()) % 7
            run_at += timedelta(days=days_until)
            if run_at <= now:
                run_at += timedelta(days=7)
            wait_seconds = max(1, int((run_at - now).total_seconds()))
            if self._stop.wait(wait_seconds):
                return
            self.job()
