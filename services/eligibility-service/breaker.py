"""Circuit breaker for the payer call — ADR 0008.

What happened on Tuesday, reconstructed from three artifacts the client gave us:

  jira RIV-141        "Tue 9:00-9:20am the ENTIRE intake screen froze - front
                       desk couldn't register any patient, not just eligibility."
  payer status page   ACME Clearinghouse eligibility endpoint degraded
                       Tue 09:02-09:21 (19 minutes).
  latency series      /intake p95 flat ~600ms all week, one 20-minute spike past
                       30s Tuesday morning, overlapping that window.

A third party's outage became Riverbend's outage, in a subsystem — patient
registration — that has nothing to do with insurance. A breaker is what stops the
next one, and there will be a next one.

The numbers, derived rather than guessed
----------------------------------------
``failure_threshold = 5`` consecutive failures. Fewer, and a single transient
blip degrades us unnecessarily. More, and we keep feeding requests into a dead
endpoint. At five, the breaker opens within seconds of an incident starting, so
the remaining ~19 minutes cost the clinic nothing.

``cooldown = 30s``. The observed outage was 19 minutes. Much shorter re-probes
uselessly; much longer keeps serving stale data after the payer recovers. Thirty
seconds is ~38 probes across a 19-minute outage — cheap — and recovery within
30 seconds of the payer coming back.

**Consecutive, not windowed.** A rolling error-rate window is more sophisticated
and, here, worse: the failure mode we are defending against is total unavailability,
which produces an unbroken run of failures. A consecutive counter detects that
in the minimum possible number of requests and is trivial to reason about at 3am.

The clock is injected so the state machine is tested exactly rather than with
sleeps. A test that sleeps 30 seconds does not get run.
"""
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class State(str, Enum):
    CLOSED = "closed"        # normal: calls pass through
    OPEN = "open"            # failing: calls short-circuit, payer is not called
    HALF_OPEN = "half_open"  # probing: exactly one call is admitted


class BreakerOpen(RuntimeError):
    """Raised when a call is short-circuited because the breaker is open."""


@dataclass
class BreakerStats:
    state: str
    consecutive_failures: int
    opened_at: Optional[float]
    trips: int
    short_circuited: int


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_s: float = 30.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        name: str = "payer",
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.name = name
        self._monotonic = monotonic
        self._lock = threading.RLock()

        self._state = State.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._probe_in_flight = False
        self.trips = 0
        self.short_circuited = 0

    # -- introspection ------------------------------------------------------ #
    @property
    def state(self) -> State:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def stats(self) -> BreakerStats:
        with self._lock:
            self._maybe_half_open()
            return BreakerStats(
                state=self._state.value,
                consecutive_failures=self._consecutive_failures,
                opened_at=self._opened_at,
                trips=self.trips,
                short_circuited=self.short_circuited,
            )

    # -- state transitions -------------------------------------------------- #
    def _maybe_half_open(self) -> None:
        """Move OPEN -> HALF_OPEN once the cooldown has elapsed. Caller holds lock."""
        if (
            self._state is State.OPEN
            and self._opened_at is not None
            and self._monotonic() - self._opened_at >= self.cooldown_s
        ):
            self._state = State.HALF_OPEN
            self._probe_in_flight = False

    def allow(self) -> bool:
        """May a call proceed? Admits exactly ONE probe in HALF_OPEN."""
        with self._lock:
            self._maybe_half_open()
            if self._state is State.CLOSED:
                return True
            if self._state is State.HALF_OPEN and not self._probe_in_flight:
                self._probe_in_flight = True
                return True
            self.short_circuited += 1
            return False

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._probe_in_flight = False
            if self._state is not State.CLOSED:
                self._state = State.CLOSED
                self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._probe_in_flight = False
            if self._state is State.HALF_OPEN:
                # A failed probe re-opens for a FULL cooldown. Backing off again
                # is the point: the payer told us it is still unwell.
                self._state = State.OPEN
                self._opened_at = self._monotonic()
                self.trips += 1
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = State.OPEN
                self._opened_at = self._monotonic()
                self.trips += 1

    def reset(self) -> None:
        with self._lock:
            self._state = State.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._probe_in_flight = False
