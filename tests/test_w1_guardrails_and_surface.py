"""W1 — output validation, the retention preflight, and the client-visible surface.

The guardrail tests exist because of one specific artifact: the contractor's
transcript in which the summary invented a medication the patient was not taking.
A mostly-faithful summary that adds "continue taking metformin 500mg" is MORE
dangerous than an obviously off-topic one, because it reads as competent and
barely moves an overlap score. Test 15 is that transcript.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

guardrails = load_module("services/ai-orchestrator/guardrails.py", "w1_guardrails")
retention = load_module("services/ai-orchestrator/retention.py", "w1_retention")
mc = load_module("services/ai-orchestrator/model_client.py", "w1_mc_surface")
app_mod = load_module("services/ai-orchestrator/app.py", "w1_app_surface")

client = TestClient(app_mod.app)
settings = app_mod.settings

SOURCE = (
    "Please arrive fifteen minutes before your appointment. Bring your insurance "
    "card and a photo ID. Do not eat or drink anything except water for eight "
    "hours before your blood draw. Parking is free in the north lot."
)


# --------------------------------------------------------------------------- #
# 14/15 — Tier-0 validation
# --------------------------------------------------------------------------- #
def test_faithful_summary_is_grounded():
    summary = ("Arrive fifteen minutes early, bring your insurance card and photo "
               "ID, and do not eat for eight hours before your blood draw.")
    verdict = guardrails.check(summary, SOURCE, 0.55)
    assert verdict.grounded, f"faithful summary scored {verdict.score}: {verdict.reasons}"


def test_ungrounded_summary_is_withheld():
    summary = ("Your mortgage application has been approved and the interest rate "
               "is locked for thirty years pending underwriter signature.")
    verdict = guardrails.check(summary, SOURCE, 0.55)
    assert not verdict.grounded
    assert verdict.needs_review


def test_invented_medication_is_caught_despite_high_overlap():
    """THE regression test for the contractor's hallucination.

    Note the summary is otherwise faithful — it repeats the source almost
    verbatim. Overlap scoring alone would pass it. Adding one clinical fact is
    exactly the failure that matters and exactly the one a score cannot see.
    """
    summary = (
        "Arrive fifteen minutes before your appointment, bring your insurance "
        "card and a photo ID, do not eat or drink anything except water for "
        "eight hours before your blood draw, and continue taking your metformin "
        "500 mg as prescribed."
    )
    verdict = guardrails.check(summary, SOURCE, 0.55)
    assert not verdict.grounded, "invented medication passed the guardrail"
    assert any(r.startswith("invented_medication") for r in verdict.reasons)
    assert any(r.startswith("invented_dosage") for r in verdict.reasons)


def test_invented_claim_fails_regardless_of_score():
    summary = SOURCE + " You should stop taking your warfarin."
    verdict = guardrails.check(summary, SOURCE, 0.0)  # threshold cannot save it
    assert not verdict.grounded
    assert verdict.reasons


def test_dosage_present_in_source_is_not_flagged():
    source = "Take 500 mg of the prep solution at 8pm the night before."
    summary = "Take 500 mg of the prep solution the night before your procedure."
    verdict = guardrails.check(summary, source, 0.4)
    assert verdict.grounded, f"a dosage quoted FROM the source is not invented: {verdict.reasons}"


# --------------------------------------------------------------------------- #
# 17 — refuse before spending
# --------------------------------------------------------------------------- #
def test_short_source_refuses_without_a_model_call():
    r = client.post("/summary", json={"instructions": "hi"})
    body = r.json()
    assert r.status_code == 200
    assert body["grounded"] is False
    assert body["needs_review"] is True
    assert body["usage"]["refused"] == "source_too_short"


# --------------------------------------------------------------------------- #
# 20 — retention preflight fails closed (RVB-X-09)
# --------------------------------------------------------------------------- #
def test_retention_accepts_zero_retention_model(monkeypatch):
    monkeypatch.setattr(retention.settings, "use_stub", False)
    status = retention.check(
        "m", probe=lambda _m: {"effective_mode": "none", "allowed_modes": ["none", "default"]}
    )
    assert status.ok and status.checked


@pytest.mark.parametrize("mode", ["default", "provider_data_share", "inherit", None])
def test_retention_refuses_non_zero_effective_mode(mode, monkeypatch):
    monkeypatch.setattr(retention.settings, "use_stub", False)
    status = retention.check(
        "m", probe=lambda _m: {"effective_mode": mode, "allowed_modes": ["none"]}
    )
    assert not status.ok, f"effective mode {mode!r} must be refused for a PHI workload"


def test_retention_refuses_model_that_forces_provider_sharing(monkeypatch):
    """The Claude Fable 5 / Mythos 5 shape: allowed_modes = [provider_data_share].

    Selecting such a model would share prompts and completions with the provider
    for up to 30 days — debt D13 through the front door.
    """
    monkeypatch.setattr(retention.settings, "use_stub", False)
    status = retention.check(
        "m", probe=lambda _m: {"effective_mode": "none",
                               "allowed_modes": ["provider_data_share"]}
    )
    assert not status.ok
    assert "zero data retention" in status.reason


def test_retention_fails_closed_when_the_probe_errors(monkeypatch):
    """An unanswerable question about where PHI goes is a 'no'."""
    monkeypatch.setattr(retention.settings, "use_stub", False)

    def boom(_m):
        raise RuntimeError("network unreachable")

    status = retention.check("m", probe=boom)
    assert not status.ok
    assert "failing closed" in status.reason


def test_retention_enforce_raises(monkeypatch):
    monkeypatch.setattr(retention.settings, "use_stub", False)
    with pytest.raises(retention.RetentionPolicyError):
        retention.enforce("m", probe=lambda _m: {"effective_mode": "default",
                                                 "allowed_modes": ["default"]})


def test_summary_refuses_when_retention_is_unsafe(monkeypatch):
    bad = retention.RetentionStatus(ok=False, checked=True, reason="effective mode 'default'")
    monkeypatch.setattr(app_mod, "RETENTION", bad)
    r = client.post("/summary", json={"instructions": SOURCE})
    body = r.json()
    assert body["usage"]["refused"] == "retention_policy"
    assert body["needs_review"] is True


# --------------------------------------------------------------------------- #
# 18/19 — Tier-1 guardrail wiring (mocked; enabling is config, not code)
# --------------------------------------------------------------------------- #
class FakeGuardrail:
    def __init__(self, action="NONE"):
        self.action = action
        self.calls = []

    def apply_guardrail(self, **kwargs):
        self.calls.append(kwargs)
        return {"action": self.action}


def test_apply_guardrail_input_side_carries_no_grounding(monkeypatch):
    """INPUT side is content/PII/topic policies.

    Contextual grounding needs a model response to evaluate, so it cannot run at
    input time. An earlier draft of the spec claimed it did; this test pins the
    correction.
    """
    monkeypatch.setattr(mc.settings, "guardrail_enabled", True)
    monkeypatch.setattr(mc.settings, "guardrail_id", "gr-1")
    fake = FakeGuardrail()
    client_ = mc.ModelClient("m", guardrail_client=fake)

    action = client_.apply_guardrail_input("some user text")

    assert action == "NONE"
    call = fake.calls[0]
    assert call["source"] == "INPUT"
    assert len(call["content"]) == 1
    blob = str(call["content"])
    assert "grounding_source" not in blob and "query" not in blob


def test_apply_guardrail_output_side_carries_all_three_components(monkeypatch):
    monkeypatch.setattr(mc.settings, "guardrail_enabled", True)
    monkeypatch.setattr(mc.settings, "guardrail_id", "gr-1")
    fake = FakeGuardrail()
    client_ = mc.ModelClient("m", guardrail_client=fake)

    client_.apply_guardrail_output("the answer", "the source", "the question")

    call = fake.calls[0]
    assert call["source"] == "OUTPUT"
    qualifiers = [q for c in call["content"] for q in c["text"].get("qualifiers", [])]
    assert "grounding_source" in qualifiers and "query" in qualifiers
    assert len(call["content"]) == 3


def test_blocked_guardrail_suppresses_the_response(monkeypatch):
    monkeypatch.setattr(mc.settings, "guardrail_enabled", True)
    monkeypatch.setattr(mc.settings, "guardrail_id", "gr-1")
    monkeypatch.setattr(mc.settings, "use_stub", False)
    fake = FakeGuardrail(action="GUARDRAIL_INTERVENED")
    chat = type("C", (), {"invoke": lambda self, m: type(
        "M", (), {"content": '{"summary":"x"}', "usage_metadata": {}})()})()
    client_ = mc.ModelClient("m", chat=chat, guardrail_client=fake)

    with pytest.raises(mc.GuardrailBlocked):
        client_.invoke("sys", "user text long enough to pass")


def test_guardrail_is_inert_when_disabled(monkeypatch):
    monkeypatch.setattr(mc.settings, "guardrail_enabled", False)
    fake = FakeGuardrail()
    client_ = mc.ModelClient("m", guardrail_client=fake)
    assert client_.apply_guardrail_input("text") == "NONE"
    assert fake.calls == [], "no Tier-1 call should be made when disabled"


# --------------------------------------------------------------------------- #
# 21 — the client-visible surface actually works (RVB-W1-15)
# --------------------------------------------------------------------------- #
def test_e2e_summary_returns_a_grounded_result():
    """An endpoint that rejected everything would pass a 401-only test.

    This one proves the feature Dr. Okonkwo would click.
    """
    r = client.post("/summary", json={"instructions": SOURCE})
    assert r.status_code == 200
    body = r.json()
    assert body["grounded"] is True, f"stub summary was not grounded: {body}"
    assert body["needs_review"] is False
    assert body["stubbed"] is True
    assert len(body["summary"]) > 20
    assert "insurance card" in body["summary"]
    assert body["usage"]["est_cost_usd"] >= 0
    assert body["request_id"]


def test_healthz_reports_retention_posture():
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert "retention" in body and "ok" in body["retention"]


# --------------------------------------------------------------------------- #
# 20b — the two id namespaces that meet at the retention probe
#
# The first live run failed here. Invocation uses a region-scoped INFERENCE
# PROFILE id; the retention API knows only foundation-model ids. Passing the
# profile id got "The provided model identifier is invalid", reported by the old
# code as a bare `ValidationException` -- which read like a policy refusal rather
# than a wrong-API bug, and cost a diagnosis.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("given,expected", [
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-4-5"),
    ("eu.anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-sonnet-4-5"),
    ("apac.anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-4-5"),
    # Already a retention-API id — unchanged.
    ("anthropic.claude-fable-5", "anthropic.claude-fable-5"),
    # Unprefixed foundation-model id — only the version suffix goes.
    ("anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-4-5"),
])
def test_retention_model_id_maps_a_profile_onto_a_model(given, expected):
    assert retention.retention_model_id(given) == expected


def test_retention_probe_failure_reports_why_not_just_the_type(monkeypatch):
    """`ValidationException` on its own is true and useless.

    The message never carries prompt text -- this call sends a model id and
    nothing else -- so including it costs no PHI exposure and saves the next
    person the hour this one cost.
    """
    monkeypatch.setattr(retention.settings, "use_stub", False)
    monkeypatch.setattr(retention.settings, "require_zero_retention", True)

    def boom(_model_id):
        raise ValueError("The provided model identifier is invalid.")

    status = retention.check("us.anthropic.claude-haiku-4-5-20251001-v1:0", probe=boom)
    assert status.ok is False
    assert "model identifier is invalid" in status.reason
    assert "ValueError" in status.reason


def test_retention_surfaces_an_unavailable_model(monkeypatch):
    """Bedrock's own fail-closed, surfaced at startup rather than at the first
    PHI request: a model whose allowed_modes excludes the account's effective
    mode reports `status: unavailable`."""
    monkeypatch.setattr(retention.settings, "use_stub", False)
    monkeypatch.setattr(retention.settings, "require_zero_retention", True)

    status = retention.check("anthropic.claude-fable-5", probe=lambda _m: {
        "effective_mode": "default",
        "allowed_modes": ["provider_data_share"],
        "status": "unavailable",
        "status_reason": "This model is not available under data retention mode 'default'.",
    })
    assert status.ok is False
