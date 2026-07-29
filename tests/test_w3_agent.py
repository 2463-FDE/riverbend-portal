"""W3 — the agent: one tool, visit-scoped memory, and honest degradation.

The model is a scripted fake. We are not testing whether Claude can converse; we
are testing that the agent cannot state a coverage status the tool did not
return, that memory is scoped to a visit, and that checkpointed PHI is encrypted
in production.
"""
import pytest

from conftest import load_module

agent_mod = load_module("services/ai-orchestrator/eligibility_agent.py", "w3_agent")
settings = agent_mod.settings


# --------------------------------------------------------------------------- #
# the consistency post-check — the control that replaces contextual grounding
#
# AWS documents contextual grounding as NOT supporting conversational/chatbot
# use cases (adr/0004 §4), so this path gets a deterministic check instead: does
# the reply's coverage claim match what the tool actually returned?
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reply,expected", [
    ("The patient's coverage is active.", "active"),
    ("Coverage is currently active and in effect.", "active"),
    ("Their plan is not active.", "inactive"),
    ("There is no coverage on file.", "inactive"),
    ("I could not verify coverage right now.", "unknown"),
    ("The payer is unreachable — please proceed with registration.", "unknown"),
    ("What member id would you like me to check?", "none"),
])
def test_claimed_status_classification(reply, expected):
    assert agent_mod.claimed_status(reply) == expected


def test_asserting_active_when_the_tool_said_unknown_is_inconsistent():
    """The failure this control exists for.

    A model that says "you're covered" because that is the statistically common
    answer has invented a clinical-billing fact.
    """
    verdict = agent_mod.check_consistency("Yes, the patient is active.", "unknown")
    assert not verdict.consistent
    assert "could not verify" in verdict.reason


def test_asserting_inactive_when_the_tool_said_active_is_inconsistent():
    verdict = agent_mod.check_consistency("Their coverage is not active.", "active")
    assert not verdict.consistent


def test_a_matching_claim_is_consistent():
    assert agent_mod.check_consistency("Coverage is active.", "active").consistent
    assert agent_mod.check_consistency(
        "I could not verify coverage.", "unknown").consistent


def test_stating_no_status_is_allowed():
    """'What would you like me to check?' is a legitimate turn."""
    assert agent_mod.check_consistency("Which member id?", "active").consistent


def test_templated_reply_tells_the_desk_what_to_do():
    unknown = agent_mod.templated_reply({"status": "unknown"})
    assert "proceed with registration" in unknown.lower()
    assert "unverified" in unknown.lower()

    stale = agent_mod.templated_reply(
        {"status": "active", "stale": True, "checked_at_display": "9:03AM on 4 Mar"}
    )
    assert "last known" in stale.lower()
    assert "9:03AM" in stale
    assert "provisional" in stale.lower()


# --------------------------------------------------------------------------- #
# the agent itself, against a scripted model
# --------------------------------------------------------------------------- #
class ScriptedModel:
    """A minimal chat model that calls the tool once, then replies with a script."""

    def __init__(self, reply: str, call_tool_with: str | None = "BCBS4471"):
        self.reply = reply
        self.call_tool_with = call_tool_with
        self._tools = []

    def bind_tools(self, tools, **_kw):
        self._tools = tools
        return self

    def invoke(self, messages, **_kw):
        from langchain_core.messages import AIMessage

        already_called = any(
            getattr(m, "type", "") == "tool" or getattr(m, "tool_call_id", None)
            for m in messages
        )
        if self.call_tool_with and not already_called:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "check_eligibility",
                    "args": {"insurance_id": self.call_tool_with},
                    "id": "call-1",
                }],
            )
        return AIMessage(content=self.reply)


def build_agent(reply, status="active", stale=False, call_tool=True):
    from langgraph.checkpoint.memory import InMemorySaver

    lookups = {"n": 0}

    def lookup(_insurance_id):
        lookups["n"] += 1
        return {"status": status, "stale": stale,
                "checked_at_display": "9:03AM on 4 Mar"}

    agent = agent_mod.EligibilityAgent(
        eligibility_lookup=lookup,
        model=ScriptedModel(reply, "BCBS4471" if call_tool else None),
        checkpointer=InMemorySaver(),
    )
    agent._lookups = lookups
    return agent


def test_agent_has_exactly_one_tool():
    """Every tool added dilutes tool-choice accuracy. The client asked for one."""
    agent = build_agent("Coverage is active.")
    tools = agent.tools
    assert len(tools) == 1
    assert tools[0].name == "check_eligibility"


def test_agent_calls_the_tool_and_reports_the_status():
    agent = build_agent("Coverage is active.", status="active")
    turn = agent.turn("visit-1", "check BCBS4471 please")
    assert turn.tool_called
    assert turn.tool_status == "active"
    assert not turn.overridden
    assert "active" in turn.reply.lower()


def test_agent_reply_is_overridden_when_it_contradicts_the_tool():
    """The headline control. The model claims active; the tool said unknown."""
    agent = build_agent("Good news — the patient is active and covered.",
                        status="unknown")
    turn = agent.turn("visit-2", "is BCBS4471 covered?")

    assert turn.tool_called
    assert turn.tool_status == "unknown"
    assert turn.overridden is True
    assert "could not be verified" in turn.reply.lower()
    assert "proceed with registration" in turn.reply.lower()
    assert "good news" not in turn.reply.lower(), (
        "the model's unsupported claim must be REPLACED, not annotated"
    )


def test_stale_status_is_surfaced_in_the_reply():
    agent = build_agent("Coverage is active.", status="active", stale=True)
    turn = agent.turn("visit-3", "check BCBS4471")
    assert turn.stale is True


def test_agent_reports_unknown_honestly_when_it_behaves():
    agent = build_agent(
        "I could not verify coverage — the payer is unreachable. Proceed with "
        "registration and mark coverage unverified.",
        status="unknown",
    )
    turn = agent.turn("visit-4", "check BCBS4471")
    assert not turn.overridden, "a correct reply must not be overridden"
    assert "unreachable" in turn.reply.lower()


# --------------------------------------------------------------------------- #
# visit-scoped memory
# --------------------------------------------------------------------------- #
def test_memory_persists_within_a_visit():
    agent = build_agent("Coverage is active.")
    agent.turn("visit-A", "check BCBS4471")
    agent.turn("visit-A", "and what did I just ask?")

    graph = agent._build()
    state = graph.get_state({"configurable": {"thread_id": "visit-A"}})
    contents = [getattr(m, "content", "") for m in state.values.get("messages", [])]
    assert any("check BCBS4471" in str(c) for c in contents)
    assert any("what did I just ask" in str(c) for c in contents)


def test_a_new_visit_starts_clean():
    """A visit has a natural end. That is what bounds how long PHI-bearing
    conversation state lives — patient-scoped memory would accumulate forever."""
    agent = build_agent("Coverage is active.")
    agent.turn("visit-A", "check BCBS4471 for Maria")

    graph = agent._build()
    fresh = graph.get_state({"configurable": {"thread_id": "visit-B"}})
    contents = " ".join(
        str(getattr(m, "content", "")) for m in fresh.values.get("messages", [])
    )
    assert "Maria" not in contents, "visit B can see visit A's conversation"


# --------------------------------------------------------------------------- #
# checkpointed PHI is encrypted in production
# --------------------------------------------------------------------------- #
def test_dev_uses_in_memory_checkpointer(monkeypatch):
    monkeypatch.setattr(settings, "agent_durable_memory", False)
    saver, kind, encrypted = agent_mod.build_checkpointer()
    assert kind == "memory"
    assert encrypted is False


def test_production_selects_an_encrypted_durable_checkpointer(monkeypatch, tmp_path):
    """The failure mode here is silent: an unencrypted checkpoint store looks
    identical to an encrypted one until someone reads the disk."""
    pytest.importorskip("Crypto", reason="pycryptodome needed for EncryptedSerializer")
    monkeypatch.setattr(settings, "agent_durable_memory", True)
    monkeypatch.setattr(settings, "agent_checkpoint_path",
                        str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)

    saver, kind, encrypted = agent_mod.build_checkpointer()
    assert kind == "sqlite"
    assert encrypted is True
    assert type(saver.serde).__name__ == "EncryptedSerializer"


# --------------------------------------------------------------------------- #
# third-party tracing is off unless BOTH a flag and a key are present
# --------------------------------------------------------------------------- #
def test_tracing_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "trace_enabled", False)
    monkeypatch.setattr(settings, "trace_api_key", "")
    assert agent_mod.configure_tracing() is False


def test_a_stale_ambient_flag_alone_cannot_enable_tracing(monkeypatch):
    """Staff type patient names, and regex scrubbing does not catch names.

    A trace sink that uploads prompt bodies is an un-BAA'd disclosure path for
    this endpoint specifically — so a leftover env flag must not be enough.
    """
    monkeypatch.setattr(settings, "trace_enabled", True)
    monkeypatch.setattr(settings, "trace_api_key", "")
    assert agent_mod.configure_tracing() is False

    import os
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_tracing_requires_both_flag_and_key(monkeypatch):
    monkeypatch.setattr(settings, "trace_enabled", True)
    monkeypatch.setattr(settings, "trace_api_key", "ls-key")
    assert agent_mod.configure_tracing() is True
    monkeypatch.setattr(settings, "trace_enabled", False)
    assert agent_mod.configure_tracing() is False


def test_no_patient_name_in_agent_logs(caplog):
    import logging

    agent = build_agent("Coverage is active.")
    svc = logging.getLogger("ai-orchestrator")
    svc.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG):
            agent.turn("visit-Maria-Gonzalez-1971", "check coverage for Maria Gonzalez")
    finally:
        svc.removeHandler(caplog.handler)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "Maria Gonzalez" not in blob, "a patient name reached a log record"
    assert "check coverage for" not in blob, "the message body was logged"
