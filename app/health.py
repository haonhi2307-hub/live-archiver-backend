from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from threading import Lock
from .models import Platform

@dataclass
class PlatformHealth:
    successes: int = 0
    failures: int = 0
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_error: str | None = None
    last_strategy: str | None = None

class HealthRegistry:
    def __init__(self):
        self._lock = Lock()
        self._data = {p.value: PlatformHealth() for p in Platform}

    def success(self, platform: Platform, strategy: str):
        with self._lock:
            row = self._data[platform.value]
            row.successes += 1
            row.last_success_at = self._now()
            row.last_strategy = strategy
            row.last_error = None

    def failure(self, platform: Platform | None, message: str):
        if platform is None:
            return
        with self._lock:
            row = self._data[platform.value]
            row.failures += 1
            row.last_failure_at = self._now()
            row.last_error = message[:500]

    def snapshot(self):
        with self._lock:
            return {name: asdict(value) for name, value in self._data.items()}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

health_registry = HealthRegistry()
