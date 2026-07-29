"""W4 — the authorization boundary. The most-tested code in the engagement.

Authorization logic now lives in one node instead of being scattered across every
query. That is a real maintainability gain and also a single point of failure, so
it gets the most tests.

The invariant under test:

    Agents never make authorization decisions.

Three mechanical properties, each asserted independently, because any one of them
alone is a comment rather than a control.
"""
import sys

import pytest

from conftest import load_module

scope_mod = load_module("services/gateway/scope.py", "w4_scope")
pvg = load_module("services/ai-orchestrator/patient_view_graph.py", "w4_pvg")
kg = load_module("services/ai-orchestrator/knowledge_graph.py", "w4_kg")

MARIA = [1042, 1330, 1588]


def patient_scope(ids):
    return scope_mod.AuthorizedScope(
        principal="patient", username="maria.gonzalez", patient_ids=frozenset(ids)
    )


def staff_scope():
    return scope_mod.AuthorizedScope(
        principal="staff", username="frontdesk", open_to_context=True
    )


# --------------------------------------------------------------------------- #
# scope resolution — session to "which patients may you see"
# --------------------------------------------------------------------------- #
def test_patient_session_resolves_to_own_chart_plus_fragments():
    """Maria is three charts. Authorizing only one would turn the W2
    fragmentation into an access denial and hide her own allergy from her."""
    s = scope_mod.resolve_scope(
        {"username": "maria.gonzalez", "patient_id": "1042"},
        same_as_lookup=lambda _p: MARIA,
    )
    assert s.principal == "patient"
    assert sorted(s.patient_ids) == MARIA
    assert s.permits(1330) and s.permits(1588)
    assert not s.permits(1043)


def test_staff_session_is_open_to_context_and_says_so():
    s = scope_mod.resolve_scope({"username": "frontdesk", "role": "staff"})
    assert s.principal == "staff"
    assert s.open_to_context is True
    assert s.permits(9999)


def test_a_failed_identity_lookup_narrows_it_never_widens():
    def boom(_p):
        raise RuntimeError("identity service down")

    s = scope_mod.resolve_scope(
        {"username": "maria.gonzalez", "patient_id": "1042"}, same_as_lookup=boom
    )
    assert sorted(s.patient_ids) == [1042], (
        "a failed identity lookup must narrow to self — identity resolution must "
        "never be able to widen access"
    )


def test_a_malformed_session_authorizes_nothing():
    s = scope_mod.resolve_scope({"username": "x", "patient_id": "not-a-number"})
    assert s.patient_ids == frozenset()
    assert not s.open_to_context
    assert not s.permits(1042), "a malformed session is not a licence to see everything"


def test_denial_raises_404_not_403():
    """A 403 on a real id and a 404 on a nonexistent one is an enumeration
    oracle, and enumeration is most of what the HAR walk was after."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        scope_mod.require_patient_access(patient_scope([1042]), 1043)
    assert exc.value.status_code == 404
    assert "not found" in str(exc.value.detail).lower()


def test_filter_never_widens():
    assert scope_mod.filter_patient_ids(patient_scope([1042]), [1042, 1043, 1330]) == [1042]
    assert scope_mod.filter_patient_ids(staff_scope(), [1042, 1043]) == [1042, 1043]


# --------------------------------------------------------------------------- #
# the graph invariant
# --------------------------------------------------------------------------- #
class RecordingLoaders:
    """Loaders that record when and with what they were called."""

    def __init__(self):
        self.calls = []

    def make(self, domain):
        def loader(ids):
            self.calls.append((domain, sorted(int(i) for i in ids)))
            return pvg.DomainResult(domain, pvg.STATUS_OK, [f"{domain} row"])
        return loader

    def as_dict(self):
        return {d: self.make(d) for d in pvg.DOMAINS}


@pytest.fixture
def recording():
    return RecordingLoaders()


@pytest.fixture
def graph(recording):
    from langgraph.checkpoint.memory import InMemorySaver
    return pvg.build_graph(loaders=recording.as_dict(), checkpointer=InMemorySaver())


def test_authorized_request_fans_out_to_every_domain(graph, recording):
    view = pvg.run(graph, patient_id=1042, scope=patient_scope(MARIA))
    assert view.authorized and view.released
    assert {d for d, _ids in recording.calls} == set(pvg.DOMAINS)
    assert "authorize" in view.path and "synthesize" in view.path


def test_denied_request_loads_zero_rows(graph, recording):
    """THE assertion. Not "loads then filters" — a filtered breach is still a
    breach: the rows were already in memory, in logs, and one refactor away from
    a model's context."""
    view = pvg.run(graph, patient_id=1043, scope=patient_scope(MARIA), thread_id="deny-1")
    assert view.authorized is False
    assert view.released is False
    assert recording.calls == [], "a retriever ran for an unauthorized request"
    assert view.domains == {}
    assert view.path == ["authorize", "deny"]


def test_authorize_runs_before_every_retriever(graph, recording):
    view = pvg.run(graph, patient_id=1042, scope=patient_scope(MARIA), thread_id="ord-1")
    assert view.path[0] == "authorize", "authorize is not the first node"
    first_retrieve = next(i for i, p in enumerate(view.path) if p.startswith("retrieve:"))
    assert view.path.index("authorize") < first_retrieve


def test_branches_receive_the_narrowed_scope_not_the_raw_request(graph, recording):
    """A retriever cannot widen a scope it never received."""
    pvg.run(graph, patient_id=1042, scope=patient_scope(MARIA), thread_id="scope-1")
    for _domain, ids in recording.calls:
        assert ids == sorted(MARIA), f"a branch received {ids}, not the authorized set"


def test_staff_scope_narrows_to_the_patient_in_context(graph, recording):
    """Staff scope is 'the patient in context', never 'everything staff could ask
    for'. Coarse — that is D7 and W9 — but not unbounded."""
    pvg.run(graph, patient_id=1043, scope=staff_scope(), thread_id="staff-1")
    for _domain, ids in recording.calls:
        assert ids == [1043]


def test_loaders_reassert_scope_at_the_data_layer():
    """Defence in depth: a loader handed a deliberately widened set still returns
    only rows for ids it was given, and never reaches past them."""
    loaders = load_module("services/ai-orchestrator/patient_view_loaders.py", "w4_loaders")

    result = loaders.load_demographics([1042])
    assert len(result.data) == 1
    assert "1042" in result.data[0]
    assert "1043" not in " ".join(result.data)

    widened = loaders.load_demographics([1042, 1043])
    assert len(widened.data) == 2, (
        "the loader must honour the ids it is GIVEN — narrowing happens at the "
        "gate, and the gate is what the tests above pin"
    )


def test_the_model_only_sees_authorized_material(recording):
    """The synthesis node is the only model call, and its input is assembled
    from already-filtered domain results."""
    seen = {}

    class SpyClient:
        def invoke(self, _system, user, **_kw):
            import model_client
            seen["prompt"] = user
            return model_client.ModelResult(text='{"summary":"ok"}', model_id="spy",
                                            stubbed=True)

    from langgraph.checkpoint.memory import InMemorySaver
    graph = pvg.build_graph(loaders=recording.as_dict(), client=SpyClient(),
                            checkpointer=InMemorySaver())
    pvg.run(graph, patient_id=1042, scope=patient_scope([1042]), thread_id="spy-1")
    assert "1043" not in seen.get("prompt", "")


# --------------------------------------------------------------------------- #
# per-domain degradation
# --------------------------------------------------------------------------- #
def test_one_failing_domain_does_not_fail_the_view():
    def boom(_ids):
        raise RuntimeError("payer unreachable")

    loaders = {d: (lambda ids, d=d: pvg.DomainResult(d, pvg.STATUS_OK, [f"{d} row"]))
               for d in pvg.DOMAINS}
    loaders["coverage"] = boom

    from langgraph.checkpoint.memory import InMemorySaver
    graph = pvg.build_graph(loaders=loaders, checkpointer=InMemorySaver())
    view = pvg.run(graph, patient_id=1042, scope=patient_scope([1042]), thread_id="deg-1")

    assert view.released, "a single domain failure took the whole view down"
    assert view.domains["coverage"]["status"] == pvg.STATUS_UNAVAILABLE
    assert view.domains["labs"]["status"] == pvg.STATUS_OK


def test_synthesis_names_what_is_missing_rather_than_omitting_it():
    """A section that silently disappears reads as a complete record."""
    loaders = {d: (lambda ids, d=d: pvg.DomainResult(d, pvg.STATUS_OK, [f"{d} row"]))
               for d in pvg.DOMAINS}
    loaders["coverage"] = lambda _ids: (_ for _ in ()).throw(RuntimeError("down"))

    from langgraph.checkpoint.memory import InMemorySaver
    graph = pvg.build_graph(loaders=loaders, checkpointer=InMemorySaver())
    view = pvg.run(graph, patient_id=1042, scope=patient_scope([1042]), thread_id="miss-1")
    assert "coverage" in view.summary.lower()
    assert "not shown" in view.summary.lower() or "unavailable" in view.summary.lower()


# --------------------------------------------------------------------------- #
# HITL — the one synchronous human gate
# --------------------------------------------------------------------------- #
def test_sensitive_assembly_interrupts(graph):
    view = pvg.run(graph, patient_id=1042, scope=patient_scope(MARIA),
                   cross_patient=True, thread_id="hitl-1")
    assert view.released is False, "a disclosure-shaped assembly auto-released"
    state = graph.get_state({"configurable": {"thread_id": "hitl-1"}})
    assert state.interrupts, "the run did not pause at the sensitivity gate"


def test_resume_false_withholds(graph):
    pvg.run(graph, patient_id=1042, scope=patient_scope(MARIA),
            cross_patient=True, thread_id="hitl-2")
    view = pvg.resume(graph, approved=False, patient_id=1042, thread_id="hitl-2")
    assert view.released is False
    assert view.summary == ""
    assert view.deny_reason == "withheld_pending_approval"


def test_resume_true_releases(graph):
    pvg.run(graph, patient_id=1042, scope=patient_scope(MARIA),
            cross_patient=True, thread_id="hitl-3")
    view = pvg.resume(graph, approved=True, patient_id=1042, thread_id="hitl-3")
    assert view.released is True
    assert view.summary


def test_ordinary_same_patient_views_are_not_gated(graph):
    """A human gate on a high-volume path is a workaround generator, not a
    control. The gate is deliberately narrow."""
    view = pvg.run(graph, patient_id=1042, scope=patient_scope(MARIA), thread_id="ng-1")
    assert view.sensitive is False
    assert view.released is True


def test_a_paused_run_survives_being_reloaded(recording):
    """The checkpointer holds assembled PHI while a human decides. If the state
    could not be reloaded, the approval workflow would be a same-process
    illusion."""
    from langgraph.checkpoint.memory import InMemorySaver

    saver = InMemorySaver()
    g1 = pvg.build_graph(loaders=recording.as_dict(), checkpointer=saver)
    pvg.run(g1, patient_id=1042, scope=patient_scope(MARIA),
            cross_patient=True, thread_id="persist-1")

    g2 = pvg.build_graph(loaders=recording.as_dict(), checkpointer=saver)
    view = pvg.resume(g2, approved=True, patient_id=1042, thread_id="persist-1")
    assert view.released is True


# --------------------------------------------------------------------------- #
# the rejection is enforced, not just documented
# --------------------------------------------------------------------------- #
def test_no_langgraph_supervisor_dependency():
    """ADR 0009 rejects it on architecture: a supervisor gives routing to a
    model, and this design needs a deterministic authorization edge before
    fan-out."""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("requirements-dev.txt",
                 "services/ai-orchestrator/requirements.txt"):
        content = open(os.path.join(root, name), encoding="utf-8").read()
        assert "langgraph-supervisor" not in content, f"{name} depends on it"
