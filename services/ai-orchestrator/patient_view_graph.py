"""The patient-view assembler — Router + Send fan-out on LangGraph v1.

    "Let patients see their own labs and visit summaries in one place. Build
     something that assembles a patient's full picture across our services."

```
  request(session_scope, patient_id)
            │
            ▼
   ┌─────────────────────┐
   │  authorize          │  deterministic. no model. runs FIRST.
   └─────────┬───────────┘
             │ denied ─────────────────────────────────► deny (END)
             │ authorized → AuthorizedScope
             ▼
   ┌─────────────────────┐
   │  plan               │  which domains? rule-based, not model-decided.
   └─────────┬───────────┘
             │  Send() — each branch receives ONLY the scope
   ┌─────────┼──────────┬────────────┬───────────┐
   ▼         ▼          ▼            ▼           │  parallel, isolated
demographics encounters labs      coverage       │
   └─────────┴──────────┴────────────┴───────────┘
             │  reducer merges partials; per-domain failures degrade
             ▼
   ┌─────────────────────┐
   │  sensitivity_gate   │  disclosure-shaped / cross-patient?
   └─────────┬───────────┘
             │  yes → interrupt() ── HITL ──► Command(resume=True/False)
             ▼
   ┌─────────────────────┐
   │  synthesize         │  ← the ONLY model call, over authorized material
   └─────────────────────┘
```

The invariant
-------------
> **Agents never make authorization decisions.**

Three mechanical properties, each independently tested:

1. `authorize` is the first node, and no edge reaches a retriever without
   passing through it. A denied request loads **zero** rows — not "loads then
   filters", which would already have put unauthorized PHI in memory, in logs,
   and potentially in a model's context.
2. `Send` payloads carry the narrowed `AuthorizedScope`, never the caller's raw
   request. A retriever cannot widen what it never saw.
3. Every retriever re-asserts scope at the data layer. Defence in depth: a branch
   handed a deliberately widened scope still returns only authorized rows.

That inverts the obvious objection. A system whose job is to assemble *more* data
about a patient from *more* sources is an IDOR amplifier — unless the multi-agent
part is strictly downstream of a gate the model cannot influence. It is.

Why this is multi-agent at all
------------------------------
Recorded honestly in ADR 0009, including the argument against. "The full picture"
spans four systems with four authorization rules, four failure modes and four
retrieval strategies: demographics (SQL), encounters (graph traversal), labs
(vector + graph, and the HL7 mapper silently drops segments — D6), and coverage
(a live payer call that goes down — D4). Serial assembly makes the payer's
latency the labs' latency. One agent with eight tools puts four domains' rules in
one context. Parallel domain retrievers with per-branch failure isolation let one
domain fail while the patient still sees the other three, correctly scoped.

`langgraph-supervisor` is not used — see ADR 0009 for why the version argument
was wrong and the architectural one is not.
"""
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Optional, TypedDict

import guardrails
import model_client
from config import settings

DOMAINS = ("demographics", "encounters", "labs", "coverage")

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"


@dataclass
class DomainResult:
    domain: str
    status: str
    data: list = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {"domain": self.domain, "status": self.status,
                "data": self.data, "note": self.note}


def _merge_domains(left: dict, right: dict) -> dict:
    return {**(left or {}), **(right or {})}


class ViewState(TypedDict, total=False):
    # inputs
    patient_id: int
    # The scope travels as PLAIN DATA, not as an object. Graph state is
    # checkpointed, and a checkpointer has to serialize it — state that cannot be
    # checkpointed cannot support the HITL interrupt/resume this graph depends
    # on. It also keeps the authorization input inspectable in a saved
    # checkpoint, which is what makes "who was allowed to see what" auditable.
    scope: dict                      # {principal, patient_ids, open_to_context}
    requested_domains: list
    # Must be DECLARED here. LangGraph drops input keys that are not part of the
    # state schema, so an undeclared flag reaches `invoke` and then silently
    # vanishes — which made the sensitivity gate never fire while every other
    # test still passed. A control that fails open without any error is the worst
    # kind, so this is pinned by test_sensitive_assembly_interrupts.
    cross_patient: bool
    # authorization
    authorized: bool
    authorized_ids: list
    deny_reason: str
    # fan-out results
    domains: Annotated[dict, _merge_domains]
    # HITL
    sensitive: bool
    approved: Optional[bool]
    # output
    summary: str
    grounded: bool
    released: bool
    path: Annotated[list, lambda a, b: (a or []) + (b or [])]


@dataclass
class PatientView:
    patient_id: int
    authorized: bool
    released: bool
    # True when the run stopped at `interrupt()` and is waiting on a human.
    #
    # Needed because an interrupted `invoke` returns the state as of BEFORE the
    # interrupting node, so `sensitive` is still its default False. Inferring
    # "paused" from the payload therefore reported every paused run as a normal
    # unreleased one, and nothing was ever queued for a decision -- the gate fired
    # and then vanished.
    paused: bool = False
    summary: str = ""
    grounded: bool = False
    domains: dict = field(default_factory=dict)
    deny_reason: str = ""
    sensitive: bool = False
    approved: Optional[bool] = None
    path: list = field(default_factory=list)


_SYNTHESIS_SYSTEM = (
    "You write a short, plain-language summary of a patient's own record for the "
    "patient to read. Use ONLY the information provided. Do not add medications, "
    "dosages, diagnoses, or any fact not present. If a section is marked "
    "unavailable, say so plainly rather than omitting it silently. Respond ONLY "
    'with a JSON object of the form {"summary": "..."} and nothing else.'
)


def build_graph(
    *,
    loaders: dict[str, Callable],
    client: Optional[model_client.ModelClient] = None,
    checkpointer=None,
    sensitivity_check: Optional[Callable[[ViewState], bool]] = None,
):
    """Compile the assembler.

    `loaders` maps a domain name to `loader(authorized_ids) -> DomainResult`.
    Injected so every retriever is trivially replaceable in tests and so this
    module has no opinion about where the data lives.
    """
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, Send, interrupt

    client = client or model_client.ModelClient(settings.synthesis_model_id)

    # -- 1. authorize: deterministic, first, no model ---------------------- #
    def authorize(state: ViewState) -> dict:
        scope = state.get("scope") or {}
        patient_id = int(state["patient_id"])
        allowed_ids = {int(p) for p in (scope.get("patient_ids") or [])}
        open_to_context = bool(scope.get("open_to_context"))
        permitted = open_to_context or patient_id in allowed_ids

        if not scope or not permitted:
            return {
                "authorized": False,
                "authorized_ids": [],
                "deny_reason": "not found",     # 404-shaped: no enumeration oracle
                "path": ["authorize"],
            }

        # The narrowed set every branch will receive. For a patient principal it
        # is their own id plus SAME_AS fragments. For staff it is the patient in
        # context plus that patient's fragments -- never "everything staff could
        # ask for", which is why the gateway supplies the cluster explicitly
        # rather than this node widening on its own.
        #
        # It used to be exactly `[patient_id]` for staff, and that was wrong in
        # the worst available direction: a clinician assembling Maria Gonzalez's
        # record received chart 1042 only -- the fragment WITHOUT her penicillin
        # allergy -- while Maria's own portal view, scoped to all three charts,
        # showed it. The Week-2 finding, pointed at the clinician instead of the
        # patient. See docs/findings/w4-staff-view-single-chart.md.
        if open_to_context:
            ids = sorted(allowed_ids | {patient_id}) if allowed_ids else [patient_id]
        else:
            ids = sorted(allowed_ids)

        return {"authorized": True, "authorized_ids": ids, "path": ["authorize"]}

    def route_authorize(state: ViewState) -> str:
        return "plan" if state.get("authorized") else "deny"

    # -- 2. plan: rule-based, not model-decided ---------------------------- #
    def plan(state: ViewState) -> dict:
        requested = state.get("requested_domains") or list(DOMAINS)
        return {"requested_domains": [d for d in requested if d in loaders],
                "path": ["plan"]}

    def fan_out(state: ViewState):
        """Send one branch per domain, carrying ONLY the authorized scope.

        The caller's raw request is deliberately not forwarded. A retriever
        cannot widen a scope it never received.
        """
        ids = state["authorized_ids"]
        return [
            Send("retrieve", {"domain": domain, "authorized_ids": ids})
            for domain in state["requested_domains"]
        ]

    # -- 3. retrieve: one node, many parallel invocations ------------------ #
    def retrieve(payload: dict) -> dict:
        domain = payload["domain"]
        ids = payload["authorized_ids"]
        loader = loaders[domain]
        try:
            result = loader(ids)
        except Exception as e:  # noqa: BLE001 — one domain failing is not the view failing
            result = DomainResult(
                domain=domain, status=STATUS_UNAVAILABLE,
                note=f"{domain} is temporarily unavailable ({type(e).__name__})",
            )
        return {"domains": {domain: result.as_dict()}, "path": [f"retrieve:{domain}"]}

    # -- 4. sensitivity gate: the one synchronous HITL --------------------- #
    def default_sensitive(state: ViewState) -> bool:
        # Disclosure-shaped: an assembly spanning more than one distinct human,
        # or a staff principal pulling a full view. Deliberately narrow — a human
        # gate on a high-volume path is a workaround generator, not a control.
        return bool(state.get("cross_patient"))

    check_sensitive = sensitivity_check or default_sensitive

    def sensitivity_gate(state: ViewState) -> dict:
        if not check_sensitive(state):
            return {"sensitive": False, "approved": True, "path": ["sensitivity_gate"]}

        decision = interrupt({
            "reason": "disclosure_shaped_assembly",
            "patient_id": state["patient_id"],
            "authorized_ids": state["authorized_ids"],
            "question": "Release this assembled record view?",
        })
        return {"sensitive": True, "approved": bool(decision), "path": ["sensitivity_gate"]}

    def route_sensitivity(state: ViewState) -> str:
        return "synthesize" if state.get("approved") else "withhold"

    # -- 5. synthesize: the ONLY model call ------------------------------- #
    def synthesize(state: ViewState) -> dict:
        domains = state.get("domains") or {}
        context = _render_context(domains)
        missing = [d for d, r in domains.items() if r["status"] != STATUS_OK]

        try:
            result = client.invoke(
                _SYNTHESIS_SYSTEM,
                f"Record sections:\n{context}\n\nReturn JSON only.",
                structured_key="summary",
                stub_text=_stub_summary(domains),
                grounding_source=context,
            )
            summary = result.text
        except Exception as e:  # noqa: BLE001
            return {"summary": "", "grounded": False, "released": False,
                    "path": ["synthesize"], "deny_reason": f"synthesis_failed:{type(e).__name__}"}

        verdict = guardrails.check(summary, context, settings.grounding_threshold)
        if not verdict.grounded:
            summary = guardrails.safe_fallback()

        if missing and verdict.grounded:
            # The synthesis must NAME what is missing rather than silently
            # omitting it — a patient view with a section quietly absent reads
            # as a complete record.
            summary += (
                "\n\nNot shown: " + ", ".join(sorted(missing))
                + " could not be loaded right now."
            )

        return {"summary": summary, "grounded": verdict.grounded, "released": True,
                "path": ["synthesize"]}

    def withhold(state: ViewState) -> dict:
        return {"summary": "", "released": False, "grounded": False,
                "deny_reason": "withheld_pending_approval", "path": ["withhold"]}

    def deny(state: ViewState) -> dict:
        return {"released": False, "summary": "", "grounded": False,
                "domains": {}, "path": ["deny"]}

    builder = StateGraph(ViewState)
    builder.add_node("authorize", authorize)
    builder.add_node("plan", plan)
    builder.add_node("retrieve", retrieve)
    builder.add_node("sensitivity_gate", sensitivity_gate)
    builder.add_node("synthesize", synthesize)
    builder.add_node("withhold", withhold)
    builder.add_node("deny", deny)

    builder.add_edge(START, "authorize")
    builder.add_conditional_edges("authorize", route_authorize,
                                  {"plan": "plan", "deny": "deny"})
    builder.add_conditional_edges("plan", fan_out, ["retrieve"])
    builder.add_edge("retrieve", "sensitivity_gate")
    builder.add_conditional_edges("sensitivity_gate", route_sensitivity,
                                  {"synthesize": "synthesize", "withhold": "withhold"})
    builder.add_edge("synthesize", END)
    builder.add_edge("withhold", END)
    builder.add_edge("deny", END)

    return builder.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _render_context(domains: dict) -> str:
    lines = []
    for name in DOMAINS:
        result = domains.get(name)
        if result is None:
            continue
        if result["status"] != STATUS_OK:
            lines.append(f"[{name}] unavailable: {result.get('note', '')}")
            continue
        body = "; ".join(str(item) for item in result["data"]) or "nothing recorded"
        lines.append(f"[{name}] {body}")
    return "\n".join(lines)


def _stub_summary(domains: dict) -> str:
    """Grounded stub: restates the retrieved sections, never invents."""
    import json

    parts = []
    for name in DOMAINS:
        result = domains.get(name)
        if result is None:
            continue
        if result["status"] != STATUS_OK:
            parts.append(f"{name}: unavailable")
        else:
            body = "; ".join(str(item) for item in result["data"]) or "nothing recorded"
            parts.append(f"{name}: {body}")
    return json.dumps({"summary": "Your record: " + ". ".join(parts) + "."})


def scope_to_dict(scope) -> dict:
    """Accept an AuthorizedScope (or any duck-typed equivalent) as plain data."""
    if isinstance(scope, dict):
        return scope
    if scope is None:
        return {}
    if hasattr(scope, "as_dict"):
        return scope.as_dict()
    return {
        "principal": getattr(scope, "principal", "unknown"),
        "patient_ids": sorted(int(p) for p in getattr(scope, "patient_ids", []) or []),
        "open_to_context": bool(getattr(scope, "open_to_context", False)),
    }


def run(
    graph,
    *,
    patient_id: int,
    scope,
    requested_domains: Optional[list] = None,
    cross_patient: bool = False,
    thread_id: Optional[str] = None,
) -> PatientView:
    config = {"configurable": {"thread_id": thread_id or f"view-{patient_id}"}}
    state: Any = graph.invoke(
        {
            "patient_id": patient_id,
            "scope": scope_to_dict(scope),
            "requested_domains": requested_domains,
            "cross_patient": cross_patient,
        },
        config=config,
    )
    return _to_view(state, patient_id, paused=is_paused(graph, config))


def is_paused(graph, config) -> bool:
    """Did the run stop at an `interrupt()`?

    Asked of the CHECKPOINTER rather than inferred from the returned state,
    because an interrupted `invoke` returns the state from before the
    interrupting node -- so the flag the gate would have set is not there yet.
    """
    try:
        snapshot = graph.get_state(config)
    except Exception:  # noqa: BLE001
        return False

    if getattr(snapshot, "next", None):
        for task in getattr(snapshot, "tasks", ()) or ():
            if getattr(task, "interrupts", None):
                return True
    return False


def resume(graph, *, approved: bool, patient_id: int, thread_id: str) -> PatientView:
    """Resume a run paused at the sensitivity gate.

    `Command(resume=...)` is the only Command shape used as graph INPUT; update /
    goto / graph are for returning from nodes.
    """
    from langgraph.types import Command

    config = {"configurable": {"thread_id": thread_id}}
    state: Any = graph.invoke(Command(resume=approved), config=config)
    return _to_view(state, patient_id, paused=is_paused(graph, config))


def _to_view(state: dict, patient_id: int, paused: bool = False) -> PatientView:
    return PatientView(
        patient_id=patient_id,
        paused=paused,
        authorized=bool(state.get("authorized")),
        released=bool(state.get("released")),
        summary=state.get("summary", ""),
        grounded=bool(state.get("grounded")),
        domains=state.get("domains", {}),
        deny_reason=state.get("deny_reason", ""),
        sensitive=bool(state.get("sensitive")),
        approved=state.get("approved"),
        path=state.get("path", []),
    )
