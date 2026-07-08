import threading
import time
from typing import Any, Callable

UNMATCHED_ROUTE = "(unmatched)"


class MetricsRegistry:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._started_at = clock()
        self._lock = threading.Lock()
        self._routes: dict[tuple[str, str], dict[str, Any]] = {}

    def record(
        self, method: str, route: str, status_code: int, duration_seconds: float
    ) -> None:
        key = (method, route)
        with self._lock:
            entry = self._routes.setdefault(
                key,
                {"count": 0, "total_seconds": 0.0, "max_seconds": 0.0, "statuses": {}},
            )
            entry["count"] += 1
            entry["total_seconds"] += duration_seconds
            entry["max_seconds"] = max(entry["max_seconds"], duration_seconds)
            status_key = str(status_code)
            entry["statuses"][status_key] = entry["statuses"].get(status_key, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            routes = {}
            total_requests = 0
            for (method, route), entry in sorted(self._routes.items()):
                routes[f"{method} {route}"] = {
                    "count": entry["count"],
                    "avg_ms": round(entry["total_seconds"] / entry["count"] * 1000, 2),
                    "max_ms": round(entry["max_seconds"] * 1000, 2),
                    "statuses": dict(entry["statuses"]),
                }
                total_requests += entry["count"]
            return {
                "uptime_seconds": round(self._clock() - self._started_at, 1),
                "total_requests": total_requests,
                "routes": routes,
            }

    def reset(self) -> None:
        with self._lock:
            self._routes.clear()
            self._started_at = self._clock()
