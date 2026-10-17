"""The public demo's limits, on a fake clock.

These guard the project's API key on a public URL, so the properties are about money: a burst
of clicks cannot jointly overshoot the cap, a run that dies is still charged, and the day rolls
over at UTC midnight without forgetting runs still in flight.
"""

from __future__ import annotations

import pytest

from ariadne.guard import Budget, PublicLimits, RateLimit


class Clock:
    def __init__(self, t: float = 1_760_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_reservations_stop_a_burst_from_overshooting_the_cap():
    b = Budget(0.20, reserve_usd=0.08, clock=Clock())
    assert [b.try_reserve() for _ in range(4)] == [True, True, False, False]
    assert b.status()["reserved_usd"] == pytest.approx(0.16)


def test_settling_replaces_the_reservation_with_the_real_cost():
    b = Budget(0.20, reserve_usd=0.08, clock=Clock())
    assert b.try_reserve() and b.try_reserve()
    b.settle(0.03)
    b.settle(0.03)
    s = b.status()
    assert s["spent_usd"] == pytest.approx(0.06) and s["reserved_usd"] == 0
    assert b.try_reserve(), "real costs below the reservation free the difference"


def test_a_run_that_died_is_charged_its_reservation():
    b = Budget(1.0, reserve_usd=0.08, clock=Clock())
    assert b.try_reserve()
    b.settle(None)
    assert b.status()["spent_usd"] == pytest.approx(0.08)


def test_the_day_rolls_over_at_utc_midnight_and_keeps_runs_in_flight():
    clock = Clock(1_760_054_399.0)  # 2025-10-09 23:59:59 UTC
    b = Budget(0.10, reserve_usd=0.08, clock=clock)
    assert b.try_reserve()
    b.settle(0.08)
    assert b.try_reserve() is False
    clock.t += 1
    assert b.status()["day_utc"] == "2025-10-10" and b.status()["spent_usd"] == 0
    assert b.try_reserve()
    clock.t += 86_400
    b.settle(0.05)  # settles into the day the money was spent
    assert b.status()["spent_usd"] == pytest.approx(0.05) and b.status()["reserved_usd"] == 0


def test_a_zero_cap_admits_nothing():
    assert Budget(0.0, clock=Clock()).try_reserve() is False


def test_rate_limit_is_per_client_and_slides():
    clock = Clock()
    r = RateLimit(2, 3600, clock=clock)
    assert [r.allow("a"), r.allow("a"), r.allow("a")] == [True, True, False]
    assert r.allow("b")
    clock.t += 3600
    assert r.allow("a")


def test_admission_checks_rate_then_concurrency_then_budget():
    clock = Clock()
    limits = PublicLimits(daily_cap_usd=1.0, runs_per_hour=10, max_concurrent_runs=1,
                          clock=clock)
    assert limits.admit_run("a") is None
    reason = limits.admit_run("b")
    assert reason is not None and "already running" in reason
    limits.finish_run(0.02)
    assert limits.admit_run("b") is None
    limits.finish_run(0.02)


def test_a_refused_budget_releases_the_concurrency_slot():
    limits = PublicLimits(daily_cap_usd=0.0, max_concurrent_runs=1, clock=Clock())
    assert "budget" in limits.admit_run("a")
    assert "budget" in limits.admit_run("a"), "the slot must not leak on refusal"


def test_rate_limited_client_is_told_why():
    limits = PublicLimits(daily_cap_usd=5.0, runs_per_hour=1, clock=Clock())
    assert limits.admit_run("a") is None
    limits.finish_run(0.01)
    assert "Rate limit" in limits.admit_run("a")
