"""W3 — the payer call: breaker, timeout, cache, degradation.

The state machine is driven with an injected clock. A test that sleeps 30 seconds
to watch a cooldown elapse is a test nobody runs.
"""
import asyncio

import pytest

from conftest import load_module

breaker_mod = load_module("services/eligibility-service/breaker.py", "w3_breaker")
payer = load_module("services/eligibility-service/payer_client.py", "w3_payer")

State = breaker_mod.State
settings = payer.settings


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def monotonic(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_breaker(clock, threshold=5, cooldown=30.0):
    return breaker_mod.CircuitBreaker(
        failure_threshold=threshold, cooldown_s=cooldown, monotonic=clock.monotonic
    )


# --------------------------------------------------------------------------- #
# 7-11 — the breaker state machine
# --------------------------------------------------------------------------- #
def test_breaker_starts_closed_and_passes_calls():
    b = make_breaker(FakeClock())
    assert b.state is State.CLOSED
    assert all(b.allow() for _ in range(10))


def test_breaker_opens_after_threshold_consecutive_failures():
    clock = FakeClock()
    b = make_breaker(clock, threshold=5)
    for _ in range(4):
        b.record_failure()
    assert b.state is State.CLOSED, "must not trip early on a transient blip"
    b.record_failure()
    assert b.state is State.OPEN
    assert b.trips == 1


def test_a_success_resets_the_consecutive_counter():
    """Consecutive, not windowed — total unavailability is an unbroken run."""
    b = make_breaker(FakeClock(), threshold=3)
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert b.state is State.CLOSED


def test_breaker_short_circuits_while_open():
    clock = FakeClock()
    b = make_breaker(clock, threshold=1)
    b.record_failure()
    assert b.state is State.OPEN
    assert not any(b.allow() for _ in range(20)), (
        "while open, ZERO calls should reach the payer"
    )
    assert b.short_circuited == 20


def test_breaker_half_opens_after_cooldown_and_admits_one_probe():
    clock = FakeClock()
    b = make_breaker(clock, threshold=1, cooldown=30.0)
    b.record_failure()

    clock.advance(29.9)
    assert b.state is State.OPEN
    assert not b.allow()

    clock.advance(0.2)
    assert b.state is State.HALF_OPEN
    assert b.allow(), "exactly one probe should be admitted"
    assert not b.allow(), "a second concurrent probe must be short-circuited"


def test_successful_probe_closes_the_breaker():
    clock = FakeClock()
    b = make_breaker(clock, threshold=1, cooldown=30.0)
    b.record_failure()
    clock.advance(31)
    assert b.allow()
    b.record_success()
    assert b.state is State.CLOSED
    assert b.allow()


def test_failed_probe_reopens_for_a_full_cooldown():
    """The payer just told us it is still unwell. Back off again."""
    clock = FakeClock()
    b = make_breaker(clock, threshold=1, cooldown=30.0)
    b.record_failure()
    clock.advance(31)
    assert b.allow()
    b.record_failure()
    assert b.state is State.OPEN
    clock.advance(29)
    assert b.state is State.OPEN
    clock.advance(2)
    assert b.state is State.HALF_OPEN


# --------------------------------------------------------------------------- #
# 5 — the call is bounded
# --------------------------------------------------------------------------- #
def test_hung_payer_returns_within_the_budget(monkeypatch):
    monkeypatch.setattr(settings, "payer_total_timeout_s", 0.05)

    async def hang(_insurance_id):
        await asyncio.sleep(60)

    client = payer.PayerClient(transport=hang)
    result = asyncio.run(client.check("BCBS4471"))

    assert result.status == payer.STATUS_UNKNOWN
    assert result.degraded_reason.startswith("timeout")
    assert result.active is None, "unknown must not be reported as False"


def test_client_never_raises_on_any_failure():
    def boom(_insurance_id):
        raise RuntimeError("connection reset")

    client = payer.PayerClient(transport=boom)
    result = asyncio.run(client.check("BCBS4471"))
    assert result.status == payer.STATUS_UNKNOWN
    assert "payer_error" in result.degraded_reason


def test_blank_member_id_is_unknown_not_an_error():
    client = payer.PayerClient(transport=lambda _i: ("active", 200))
    result = asyncio.run(client.check("   "))
    assert result.status == payer.STATUS_UNKNOWN


# --------------------------------------------------------------------------- #
# 12/13 — graceful degradation, with staleness surfaced
# --------------------------------------------------------------------------- #
def test_degrades_to_cached_value_with_staleness_and_original_timestamp():
    clock = FakeClock()
    calls = {"n": 0}

    def transport(_insurance_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("active", 200)
        raise RuntimeError("payer down")

    client = payer.PayerClient(transport=transport, clock=clock.wall,
                               monotonic=clock.monotonic)

    first = asyncio.run(client.check("BCBS4471"))
    assert first.status == "active" and not first.stale
    original_time = first.checked_at

    clock.advance(3600)
    second = asyncio.run(client.check("BCBS4471"))

    assert second.status == "active"
    assert second.stale is True
    assert second.checked_at == original_time, (
        "a stale result must carry the ORIGINAL check time — presenting a "
        "six-hour-old status as current is how a patient gets billed for an "
        "uncovered visit"
    )


def test_degrades_to_unknown_without_a_cache():
    def boom(_i):
        raise RuntimeError("payer down")

    client = payer.PayerClient(transport=boom)
    result = asyncio.run(client.check("NEVER-SEEN"))
    assert result.status == payer.STATUS_UNKNOWN
    assert result.degraded_reason.endswith("no_cache")
    assert result.stale is False, "we are not serving a stale value; we have none"


def test_expired_cache_entry_is_dropped_not_served(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(settings, "eligibility_cache_ttl_s", 60.0)
    calls = {"n": 0}

    def transport(_i):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("active", 200)
        raise RuntimeError("down")

    client = payer.PayerClient(transport=transport, clock=clock.wall,
                               monotonic=clock.monotonic)
    asyncio.run(client.check("M1"))
    clock.advance(120)
    result = asyncio.run(client.check("M1"))
    assert result.status == payer.STATUS_UNKNOWN, (
        "a coverage status older than the TTL is not evidence of anything"
    )


def test_open_breaker_serves_cache_without_calling_the_payer():
    clock = FakeClock()
    calls = {"n": 0}

    def transport(_i):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("active", 200)
        raise RuntimeError("down")

    b = make_breaker(clock, threshold=2, cooldown=30.0)
    client = payer.PayerClient(transport=transport, breaker=b,
                               clock=clock.wall, monotonic=clock.monotonic)

    asyncio.run(client.check("M1"))            # warms the cache
    asyncio.run(client.check("M1"))            # failure 1
    asyncio.run(client.check("M1"))            # failure 2 -> open
    assert b.state is State.OPEN

    before = calls["n"]
    result = asyncio.run(client.check("M1"))
    assert calls["n"] == before, "an open breaker must not call the payer at all"
    assert result.stale is True
    assert result.degraded_reason == "breaker_open"


# --------------------------------------------------------------------------- #
# the message a receptionist actually reads
# --------------------------------------------------------------------------- #
def test_describe_never_hides_staleness():
    fresh = payer.EligibilityResult(insurance_id="M1", status="active",
                                    checked_at=1_700_000_000.0, stale=False)
    stale = payer.EligibilityResult(insurance_id="M1", status="active",
                                    checked_at=1_700_000_000.0, stale=True,
                                    degraded_reason="breaker_open")
    unknown = payer.EligibilityResult(insurance_id="M1", status="unknown")

    assert "just now" in payer.describe(fresh)
    assert "last known" in payer.describe(stale) and "as of" in payer.describe(stale)
    assert "proceed with registration" in payer.describe(unknown).lower(), (
        "an unverifiable coverage status must tell the desk what to DO"
    )


# --------------------------------------------------------------------------- #
# 14 — THE Tuesday regression test
# --------------------------------------------------------------------------- #
def test_a_nineteen_minute_payer_outage_costs_almost_nothing():
    """Replay Tuesday 09:02-09:21 against the new client.

    Before: every registration inherited the payer's hang, for nineteen minutes.
    After: the breaker opens within the first few requests and the remaining
    ~19 minutes are served from cache with no payer contact at all.
    """
    clock = FakeClock()
    payer_calls = {"n": 0}
    outage = {"active": False}

    def transport(_i):
        payer_calls["n"] += 1
        if outage["active"]:
            raise RuntimeError("clearinghouse degraded")
        return ("active", 200)

    b = make_breaker(clock, threshold=5, cooldown=30.0)
    client = payer.PayerClient(transport=transport, breaker=b,
                               clock=clock.wall, monotonic=clock.monotonic)

    asyncio.run(client.check("BCBS4471"))          # 08:55, healthy: warms cache
    calls_before_outage = payer_calls["n"]

    outage["active"] = True
    results = []
    # 19 minutes of front-desk traffic, one registration every 15 seconds.
    for _ in range(19 * 4):
        results.append(asyncio.run(client.check("BCBS4471")))
        clock.advance(15)

    outage_calls = payer_calls["n"] - calls_before_outage

    assert all(r.status == "active" for r in results), (
        "the front desk should have kept getting the last-known status"
    )
    assert all(r.stale for r in results), "and it should have been marked stale"
    # 76 requests. Without a breaker every one hits a dead payer. With a 30s
    # cooldown across 19 minutes we expect roughly the 5 that trip it plus ~38
    # probes — a fraction, and none of them on the registration path.
    assert outage_calls < 50, f"expected far fewer payer calls, got {outage_calls}"
    assert b.trips >= 1
    assert b.short_circuited > 0

    # Recovery: within one cooldown of the payer returning.
    outage["active"] = False
    clock.advance(31)
    recovered = asyncio.run(client.check("BCBS4471"))
    assert recovered.status == "active"
    assert recovered.stale is False, "should be a fresh value once the payer is back"
    assert b.state is State.CLOSED
