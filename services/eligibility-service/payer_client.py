"""The resilient payer eligibility client — the actual Week-3 deliverable.

Replaces `check.py`, which had "no timeout, no retry, no circuit breaker, no
cache" (its own docstring said so) and sat inline on the intake request path.

The contract that matters
-------------------------
**This client never raises.** Every path returns an ``EligibilityResult``, and
``unknown`` is a valid status. That is not defensive coding for its own sake — it
is the thing that lets registration proceed when the payer is down, which is the
week's deliverable. An exception here would propagate into `/intake` and we would
have rebuilt the outage with better logging.

Staleness is surfaced, never hidden
-----------------------------------
A degraded result carries the ``checked_at`` of the value we cached, and a
``stale`` flag. The front desk sees

    Active — as of 9:03am (payer unreachable)

not

    Active

Presenting a six-hour-old coverage status as current is how a patient gets billed
for an uncovered visit. The staleness is the clinically useful part.

Timeout budget
--------------
Total 5 seconds (connect 2, read 4, but bounded overall). Derived from the
front desk's tolerance, not the payer's SLA: the portal's healthy `/intake` p95 is
~600ms, and a receptionist is standing in front of a patient. A check slower than
a few seconds has already failed them, so we stop waiting and degrade. See
ADR 0008 for the full derivation of every number here.
"""
import asyncio
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from breaker import CircuitBreaker
from config import settings

STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_UNKNOWN = "unknown"


@dataclass
class EligibilityResult:
    insurance_id: str
    status: str                    # active | inactive | unknown
    payer: str = ""
    checked_at: float = 0.0        # epoch seconds of the ORIGINAL check
    stale: bool = False
    degraded_reason: str = ""      # timeout | breaker_open | payer_error | no_cache
    raw_status: Optional[int] = None
    latency_ms: int = 0

    @property
    def active(self) -> Optional[bool]:
        if self.status == STATUS_ACTIVE:
            return True
        if self.status == STATUS_INACTIVE:
            return False
        return None                # unknown is not False — that distinction matters

    def as_dict(self) -> dict:
        out = asdict(self)
        out["active"] = self.active
        return out


@dataclass
class _CacheEntry:
    status: str
    checked_at: float
    raw_status: Optional[int]


class LastKnownCache:
    """Last successful eligibility per member id.

    In-process by design for this increment. A shared cache (Redis is already in
    the stack) is the obvious next step and is noted in ADR 0008 — but it changes
    the failure domain from 'this process' to 'this cluster', which deserves its
    own decision rather than being smuggled in here.
    """

    def __init__(self, ttl_s: float = 86400.0, clock: Callable[[], float] = time.time):
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, _CacheEntry] = {}

    def put(self, insurance_id: str, status: str, raw_status: Optional[int]) -> None:
        with self._lock:
            self._entries[insurance_id] = _CacheEntry(
                status=status, checked_at=self._clock(), raw_status=raw_status
            )

    def get(self, insurance_id: str) -> Optional[_CacheEntry]:
        with self._lock:
            entry = self._entries.get(insurance_id)
            if entry is None:
                return None
            if self._clock() - entry.checked_at > self.ttl_s:
                # Expired. Deliberately dropped rather than served: a coverage
                # status older than a day is not evidence of anything.
                del self._entries[insurance_id]
                return None
            return entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class PayerClient:
    def __init__(
        self,
        *,
        transport: Optional[Callable[[str], Any]] = None,
        breaker: Optional[CircuitBreaker] = None,
        cache: Optional[LastKnownCache] = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._transport = transport            # injected in tests; real one below
        self.breaker = breaker or CircuitBreaker(
            failure_threshold=settings.breaker_failure_threshold,
            cooldown_s=settings.breaker_cooldown_s,
        )
        self.cache = cache or LastKnownCache(ttl_s=settings.eligibility_cache_ttl_s,
                                             clock=clock)
        self._clock = clock
        self._monotonic = monotonic

    # -- transport ---------------------------------------------------------- #
    async def _call_payer(self, insurance_id: str) -> tuple[str, Optional[int]]:
        """One bounded, async round trip. Raises on failure; the caller degrades."""
        if self._transport is not None:
            return await _maybe_await(self._transport(insurance_id))

        import httpx

        timeout = httpx.Timeout(
            connect=settings.payer_connect_timeout_s,
            read=settings.payer_read_timeout_s,
            write=settings.payer_connect_timeout_s,
            pool=settings.payer_connect_timeout_s,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                settings.payer_api_url,
                params={"member_id": insurance_id, "service_type": "30"},
                headers={"Authorization": f"Bearer {settings.payer_api_key}"},
            )
        return (STATUS_ACTIVE if resp.is_success else STATUS_INACTIVE), resp.status_code

    # -- the public call ---------------------------------------------------- #
    async def check(self, insurance_id: str) -> EligibilityResult:
        """Never raises. `unknown` is a valid answer."""
        insurance_id = (insurance_id or "").strip()
        started = self._monotonic()

        if not insurance_id:
            return EligibilityResult(
                insurance_id="", status=STATUS_UNKNOWN, payer=settings.payer_name,
                degraded_reason="no_member_id", checked_at=self._clock(),
            )

        if not self.breaker.allow():
            return self._degrade(insurance_id, "breaker_open", started)

        try:
            status, raw = await asyncio.wait_for(
                self._call_payer(insurance_id), timeout=settings.payer_total_timeout_s
            )
        except asyncio.TimeoutError:
            self.breaker.record_failure()
            return self._degrade(insurance_id, "timeout", started)
        except Exception:  # noqa: BLE001 — every payer failure degrades identically
            self.breaker.record_failure()
            return self._degrade(insurance_id, "payer_error", started)

        self.breaker.record_success()
        now = self._clock()
        self.cache.put(insurance_id, status, raw)
        return EligibilityResult(
            insurance_id=insurance_id,
            status=status,
            payer=settings.payer_name,
            checked_at=now,
            stale=False,
            raw_status=raw,
            latency_ms=int((self._monotonic() - started) * 1000),
        )

    def _degrade(self, insurance_id: str, reason: str, started: float) -> EligibilityResult:
        """Serve last-known if we have it, `unknown` if we do not. Never fail."""
        entry = self.cache.get(insurance_id)
        latency = int((self._monotonic() - started) * 1000)
        if entry is not None:
            return EligibilityResult(
                insurance_id=insurance_id,
                status=entry.status,
                payer=settings.payer_name,
                checked_at=entry.checked_at,   # the ORIGINAL check, not now
                stale=True,
                degraded_reason=reason,
                raw_status=entry.raw_status,
                latency_ms=latency,
            )
        return EligibilityResult(
            insurance_id=insurance_id,
            status=STATUS_UNKNOWN,
            payer=settings.payer_name,
            checked_at=self._clock(),
            stale=False,
            degraded_reason=f"{reason}:no_cache",
            latency_ms=latency,
        )


async def _maybe_await(value):
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def describe(result: EligibilityResult) -> str:
    """One line a receptionist can act on. Staleness is never hidden."""
    import datetime as _dt

    if result.status == STATUS_UNKNOWN:
        return (
            "Coverage could not be verified right now — the payer is unreachable. "
            "Proceed with registration and mark coverage unverified."
        )
    label = "Active" if result.status == STATUS_ACTIVE else "Not active"
    if not result.stale:
        return f"{label} (verified just now)."
    when = _dt.datetime.fromtimestamp(result.checked_at).strftime("%-I:%M%p on %-d %b")
    return f"{label} — as of {when} (payer unreachable, showing last known)."
