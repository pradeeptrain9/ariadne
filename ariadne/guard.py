"""Limits for the public demo: a daily spend cap, per-client rate limits, and a concurrency cap.

Locally none of this applies — the person running the server is paying for their own key. On a
public URL every Investigate click is a paid model call on the project's key, made by whoever
found the link, so the hosted server refuses work once the day's budget is spoken for.

The cap is enforced by *reservation*: a run's cost is only known when it ends, so starting one
reserves the most a run has been measured to cost, and the reservation is replaced by the real
figure when the run finishes. Without that, a burst of simultaneous clicks would each see the
same unspent budget and together overshoot it.

The counter lives in memory. A free host that sleeps when idle restarts with a fresh one, so this
is a guard against a busy day, not a billing control — the hard ceiling belongs on the API key
itself (a workspace spend limit in the Anthropic Console), and the README says so.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

#: Reserved per run before it starts. The 20-alert measurement's most expensive run was $0.0605
#: (cold); this leaves headroom for a run that wanders.
RESERVE_PER_RUN_USD = 0.08


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


class Budget:
    """Daily spend cap in USD, reset at 00:00 UTC."""

    def __init__(self, cap_usd: float, *, reserve_usd: float = RESERVE_PER_RUN_USD,
                 clock: Callable[[], float] = time.time) -> None:
        self.cap = float(cap_usd)
        self.reserve = float(reserve_usd)
        self._clock = clock
        self._lock = threading.Lock()
        self._day = utc_day(clock())
        self._spent = 0.0
        self._reserved = 0.0

    def _roll(self) -> None:
        today = utc_day(self._clock())
        if today != self._day:
            # Reservations still open belong to runs in flight; they carry over and settle into
            # the new day, which is the day the money is spent.
            self._day, self._spent = today, 0.0

    def try_reserve(self) -> bool:
        with self._lock:
            self._roll()
            if self._spent + self._reserved + self.reserve > self.cap + 1e-9:
                return False
            self._reserved += self.reserve
            return True

    def settle(self, actual_usd: float | None) -> None:
        """Replace one reservation with what the run cost. `None` — the run died before it could
        report — charges the reservation, since the money may well have been spent."""
        with self._lock:
            self._roll()
            self._reserved = max(0.0, self._reserved - self.reserve)
            self._spent += self.reserve if actual_usd is None else max(0.0, float(actual_usd))

    def status(self) -> dict[str, float | str]:
        with self._lock:
            self._roll()
            remaining = max(0.0, self.cap - self._spent - self._reserved)
            return {"cap_usd": round(self.cap, 4), "spent_usd": round(self._spent, 4),
                    "reserved_usd": round(self._reserved, 4),
                    "remaining_usd": round(remaining, 4), "day_utc": self._day,
                    "runs_left_estimate": int((remaining + 1e-9) // self.reserve) if self.reserve else 0}


class RateLimit:
    """At most `limit` events per `window_s` seconds per key (a sliding window)."""

    def __init__(self, limit: int, window_s: float, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.limit, self.window = limit, window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # forget idle clients rather than grow without bound
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True


class PublicLimits:
    """Everything the hosted server checks, in one place, so the handler stays readable."""

    def __init__(self, *, daily_cap_usd: float, runs_per_hour: int = 6,
                 webhooks_per_hour: int = 20, max_concurrent_runs: int = 2,
                 max_pushed_alerts: int = 300,
                 clock: Callable[[], float] = time.time) -> None:
        self.budget = Budget(daily_cap_usd, clock=clock)
        self.runs = RateLimit(runs_per_hour, 3600, clock=clock)
        self.webhooks = RateLimit(webhooks_per_hour, 3600, clock=clock)
        self.concurrent = threading.BoundedSemaphore(max_concurrent_runs)
        self.max_concurrent_runs = max_concurrent_runs
        self.max_pushed_alerts = max_pushed_alerts

    def admit_run(self, client: str) -> str | None:
        """`None` when the run may start (a slot and a reservation are then held — release
        both with `finish_run`), else the reason it may not, written for the page to show."""
        if not self.runs.allow(client):
            return ("Rate limit: at most "
                    f"{self.runs.limit} live investigations per hour from one address. "
                    "Clone the repo to run it without limits.")
        if not self.concurrent.acquire(blocking=False):
            return (f"{self.max_concurrent_runs} investigations are already running on this "
                    "shared demo. Try again in half a minute.")
        if not self.budget.try_reserve():
            self.concurrent.release()
            return (f"Today's live-run budget (${self.budget.cap:.2f}, resets 00:00 UTC) is "
                    "spent. Everything else on the page still works; clone the repo to run "
                    "investigations on your own key.")
        return None

    def finish_run(self, actual_usd: float | None) -> None:
        self.budget.settle(actual_usd)
        self.concurrent.release()
