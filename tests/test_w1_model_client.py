"""W1 — the resilience envelope around the model call.

These tests do not test the model. They test everything that can actually take a
system down: an unbounded deadline, a retry that hammers a throttled endpoint, a
parse that explodes on prose, a budget guard that does not fire.

Every test runs offline with no AWS credentials and spends nothing. The clock,
the sleep function and the RNG are injected so retry and backoff behaviour is
asserted exactly rather than statistically — a flaky test of a resilience control
is worse than no test, because it teaches people to re-run CI.
"""
import random

import pytest

from conftest import load_module

mc = load_module("services/ai-orchestrator/model_client.py", "w1_model_client")
settings = mc.settings


class FakeClock:
    """Monotonic clock that only advances when we say so."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BoomChat:
    """A chat client that raises a chosen error every time, counting calls."""

    def __init__(self, error, clock=None, per_call_seconds=0.0):
        self.error = error
        self.calls = 0
        self.clock = clock
        self.per_call_seconds = per_call_seconds

    def invoke(self, _messages):
        self.calls += 1
        if self.clock and self.per_call_seconds:
            self.clock.advance(self.per_call_seconds)
        raise self.error


class ReplyChat:
    """A chat client that returns fixed text, optionally after N failures."""

    def __init__(self, text, fail_times=0, error=None):
        self.text = text
        self.fail_times = fail_times
        self.error = error or _named_error("ThrottlingException")
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return type("Msg", (), {"content": self.text, "usage_metadata": {
            "input_tokens": 100, "output_tokens": 20}})()


def _named_error(name: str) -> Exception:
    """Build an exception whose class name matches a boto error class."""
    return type(name, (Exception,), {})(name)


@pytest.fixture(autouse=True)
def _real_mode(monkeypatch):
    """Exercise the real transport path (with an injected chat), not the stub."""
    monkeypatch.setattr(settings, "use_stub", False)
    monkeypatch.setattr(settings, "guardrail_enabled", False)
    yield


# --------------------------------------------------------------------------- #
# 1 — the deadline bounds the whole request, not just one hop
# --------------------------------------------------------------------------- #
def test_deadline_bounds_total_wall_clock(monkeypatch):
    monkeypatch.setattr(settings, "deadline_s", 10.0)
    monkeypatch.setattr(settings, "max_retries", 10)
    monkeypatch.setattr(settings, "read_timeout_s", 20.0)

    clock = FakeClock()
    # Each attempt burns 4s of wall clock, so an unbounded client would run
    # 11 attempts = 44s+. The deadline must stop it at 10s.
    chat = BoomChat(_named_error("ThrottlingException"), clock=clock, per_call_seconds=4.0)
    client = mc.ModelClient("m", sleep=clock.sleep, monotonic=clock.monotonic,
                            rng=random.Random(1), chat=chat)

    with pytest.raises(mc.ModelUnavailable):
        client.invoke("sys", "user text long enough to pass the guard")

    assert clock.now <= settings.deadline_s + 4.0, "ran past the deadline"
    assert chat.calls < 11, "deadline did not curtail the retry budget"


def test_backoff_never_sleeps_past_the_deadline(monkeypatch):
    """A backoff that overruns the budget is just a slower timeout."""
    monkeypatch.setattr(settings, "deadline_s", 1.0)
    monkeypatch.setattr(settings, "max_retries", 5)
    monkeypatch.setattr(settings, "backoff_base_s", 10.0)
    monkeypatch.setattr(settings, "backoff_cap_s", 10.0)

    clock = FakeClock()
    chat = BoomChat(_named_error("ThrottlingException"), clock=clock)
    client = mc.ModelClient("m", sleep=clock.sleep, monotonic=clock.monotonic,
                            rng=random.Random(1), chat=chat)

    with pytest.raises(mc.ModelUnavailable):
        client.invoke("sys", "user text long enough to pass the guard")

    assert clock.slept == [], "slept a 10s backoff inside a 1s deadline"


# --------------------------------------------------------------------------- #
# 2 — retry classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(mc.RETRYABLE))
def test_retryable_errors_are_retried(name, monkeypatch):
    monkeypatch.setattr(settings, "max_retries", 2)
    monkeypatch.setattr(settings, "deadline_s", 1000.0)
    clock = FakeClock()
    chat = BoomChat(_named_error(name))
    client = mc.ModelClient("m", sleep=clock.sleep, monotonic=clock.monotonic,
                            rng=random.Random(1), chat=chat)

    with pytest.raises(mc.ModelUnavailable):
        client.invoke("sys", "user text long enough to pass the guard")

    assert chat.calls == 3, f"{name} should be retried to max_retries+1"


@pytest.mark.parametrize("name", sorted(mc.NON_RETRYABLE))
def test_non_retryable_errors_fail_fast(name, monkeypatch):
    monkeypatch.setattr(settings, "max_retries", 5)
    monkeypatch.setattr(settings, "deadline_s", 1000.0)
    clock = FakeClock()
    chat = BoomChat(_named_error(name))
    client = mc.ModelClient("m", sleep=clock.sleep, monotonic=clock.monotonic,
                            rng=random.Random(1), chat=chat)

    with pytest.raises(mc.ModelUnavailable):
        client.invoke("sys", "user text long enough to pass the guard")

    assert chat.calls == 1, (
        f"{name} must not be retried — resending a wrong request turns a fast, "
        f"clear error into a slow, ambiguous outage"
    )
    assert clock.slept == [], "must not back off before failing fast"


def test_client_error_code_is_classified(monkeypatch):
    """botocore ClientError carries the real code in the response envelope."""
    monkeypatch.setattr(settings, "max_retries", 2)
    monkeypatch.setattr(settings, "deadline_s", 1000.0)

    err = Exception("boom")
    err.response = {"Error": {"Code": "ThrottlingException"}}
    clock = FakeClock()
    chat = BoomChat(err)
    client = mc.ModelClient("m", sleep=clock.sleep, monotonic=clock.monotonic,
                            rng=random.Random(1), chat=chat)

    with pytest.raises(mc.ModelUnavailable):
        client.invoke("sys", "user text long enough to pass the guard")
    assert chat.calls == 3


def test_unknown_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(settings, "max_retries", 3)
    clock = FakeClock()
    chat = BoomChat(RuntimeError("something new"))
    client = mc.ModelClient("m", sleep=clock.sleep, monotonic=clock.monotonic,
                            rng=random.Random(1), chat=chat)
    with pytest.raises(mc.ModelUnavailable):
        client.invoke("sys", "user text long enough to pass the guard")
    assert chat.calls == 1, "unknown failures should surface loudly, not slowly"


def test_recovers_after_transient_failures(monkeypatch):
    monkeypatch.setattr(settings, "max_retries", 3)
    monkeypatch.setattr(settings, "deadline_s", 1000.0)
    clock = FakeClock()
    chat = ReplyChat('{"summary": "ok"}', fail_times=2)
    client = mc.ModelClient("m", sleep=clock.sleep, monotonic=clock.monotonic,
                            rng=random.Random(1), chat=chat)

    result = client.invoke("sys", "user text long enough", structured_key="summary")
    assert result.text == "ok"
    assert result.attempts == 3
    assert len(clock.slept) == 2


# --------------------------------------------------------------------------- #
# 3 — backoff is deterministic under a fixed seed
# --------------------------------------------------------------------------- #
def test_backoff_is_bounded_and_deterministic(monkeypatch):
    monkeypatch.setattr(settings, "backoff_base_s", 0.25)
    monkeypatch.setattr(settings, "backoff_cap_s", 4.0)

    client_a = mc.ModelClient("m", rng=random.Random(42))
    client_b = mc.ModelClient("m", rng=random.Random(42))
    client_c = mc.ModelClient("m", rng=random.Random(7))

    seq_a = [client_a.backoff_delay(i) for i in range(6)]
    seq_b = [client_b.backoff_delay(i) for i in range(6)]
    seq_c = [client_c.backoff_delay(i) for i in range(6)]

    assert seq_a == seq_b, "same seed must give the same sequence"
    assert seq_a != seq_c, "different seeds must give different sequences (jitter)"

    for i, delay in enumerate(seq_a):
        raw = min(4.0, 0.25 * (2 ** i))
        assert raw * 0.5 <= delay <= raw, f"attempt {i}: {delay} outside [{raw*0.5}, {raw}]"
        assert delay <= settings.backoff_cap_s


# --------------------------------------------------------------------------- #
# 4 — structured output parses, or degrades; never raises
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload,expected", [
    ('{"summary": "clean json"}', "clean json"),
    ('Here you go:\n{"summary": "wrapped in prose"}\nHope that helps!', "wrapped in prose"),
    ('```json\n{"summary": "fenced"}\n```', "fenced"),
    ('{"summary": "trailing junk"} <<<', "trailing junk"),
])
def test_structured_output_parse_variants(payload, expected):
    value, _obj = mc.parse_structured(payload, "summary")
    assert value == expected


@pytest.mark.parametrize("payload", [
    "just prose, no json at all",
    '{"summary": ',            # truncated
    '{"other_key": "x"}',      # right shape, wrong key
    "",
])
def test_structured_output_degrades_without_raising(payload, monkeypatch):
    monkeypatch.setattr(settings, "deadline_s", 1000.0)
    clock = FakeClock()
    chat = ReplyChat(payload)
    client = mc.ModelClient("m", sleep=clock.sleep, monotonic=clock.monotonic,
                            rng=random.Random(1), chat=chat)
    result = client.invoke("sys", "user text long enough", structured_key="summary")
    assert isinstance(result.text, str), "a bad response is not a server error"


# --------------------------------------------------------------------------- #
# 5/6 — budget guard refuses BEFORE any call
# --------------------------------------------------------------------------- #
def test_budget_refuses_before_any_call(monkeypatch):
    monkeypatch.setattr(settings, "max_input_tokens", 10)
    clock = FakeClock()
    chat = ReplyChat('{"summary": "never reached"}')
    client = mc.ModelClient("m", sleep=clock.sleep, monotonic=clock.monotonic,
                            rng=random.Random(1), chat=chat)

    with pytest.raises(mc.BudgetError):
        client.invoke("sys", "x" * 5000)

    assert chat.calls == 0, "budget guard must refuse before spending"


def test_cost_ceiling_refuses_before_any_call(monkeypatch):
    monkeypatch.setattr(settings, "max_input_tokens", 1_000_000)
    monkeypatch.setattr(settings, "max_cost_per_request_usd", 0.0000001)
    clock = FakeClock()
    chat = ReplyChat('{"summary": "never reached"}')
    client = mc.ModelClient("m", sleep=clock.sleep, monotonic=clock.monotonic,
                            rng=random.Random(1), chat=chat)

    with pytest.raises(mc.BudgetError):
        client.invoke("sys", "some instructions here")
    assert chat.calls == 0


def test_converse_content_block_list_is_flattened():
    """Bedrock Converse returns a content-block list, not a bare string."""
    msg = type("Msg", (), {
        "content": [{"type": "text", "text": '{"summary": "from blocks"}'}],
        "usage_metadata": {"input_tokens": 5, "output_tokens": 3},
    })()
    assert mc._message_text(msg) == '{"summary": "from blocks"}'
