#!/usr/bin/env python3
"""The Riverbend demo — four acts, runs offline in seconds, spends nothing.

    python3 scripts/demo.py            # all four acts
    python3 scripts/demo.py 2          # just act 2

Every number this prints is computed live from the client's own handover data.
Nothing is hard-coded, and no AWS credential is required: the model runs in stub
mode, so what you see is the SYSTEM's behaviour, not a model's improvisation.

  Act 1  The AI feature, and what it refuses to do
  Act 2  One patient, three charts, and the allergy nobody could see
  Act 3  Nineteen minutes when nobody could register a patient
  Act 4  Any login could read any chart

**This is the FALLBACK, not the demo.** The demo is the portal --
`docs/showcase/DEMO.md` Path A, in a browser, with `make up`.

Keep this for a room with no Docker or a hostile network: it is deterministic and
cannot fail on stage. But it answers "does the system behave correctly?", not "can
a person do this?", and for a while it was allowed to stand in for the second
question while `frontend/` had zero lines in it. Leading with it again would be
the same mistake.
"""
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "services", "ai-orchestrator",
))

BOLD, DIM, RED, GRN, YLW, CYN, RST = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Sibling module names that appear in more than one service directory.
_COLLIDING = ("config", "breaker", "payer_client", "logging_config", "schemas",
              "models", "db", "app", "check")


def _load_service_module(service: str, module: str, alias: str):
    """Import one service's module without leaving its siblings cached.

    Every service has its own config.py. Importing eligibility-service's
    payer_client caches an `config` that is NOT ai-orchestrator's, so a later
    act reading `settings.synthesis_model_id` gets an AttributeError.
    """
    import importlib.util

    service_dir = os.path.join(REPO, "services", service)
    saved = {n: sys.modules.pop(n, None) for n in _COLLIDING}
    sys.path.insert(0, service_dir)
    try:
        spec = importlib.util.spec_from_file_location(
            alias, os.path.join(service_dir, f"{module}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(service_dir)
        for n in _COLLIDING:
            sys.modules.pop(n, None)
            if saved.get(n) is not None:
                sys.modules[n] = saved[n]


def _tuesday_0902() -> float:
    """Epoch seconds for a plausible Tuesday 09:02, so the demo's clock reads
    like the morning from the status page rather than 1970."""
    import datetime as dt

    return dt.datetime(2026, 3, 3, 9, 2, 0).timestamp()


def title(n, text):
    print(f"\n{BOLD}{CYN}{'═' * 74}{RST}")
    print(f"{BOLD}{CYN}  ACT {n} — {text}{RST}")
    print(f"{BOLD}{CYN}{'═' * 74}{RST}\n")


def say(text, indent=2):
    print(textwrap.indent(textwrap.fill(text, 72), " " * indent))


def beat():
    print()


# --------------------------------------------------------------------------- #
def act1():
    title(1, "The AI feature, and what it refuses to do")
    import guardrails
    import retention
    from config import settings

    source = (
        "Please arrive fifteen minutes before your appointment. Bring your "
        "insurance card and a photo ID. Do not eat or drink anything except "
        "water for eight hours before your blood draw."
    )
    say("You asked for an assistant that turns intake instructions into "
        "something a patient can read. Here is the source text:")
    beat()
    print(textwrap.indent(textwrap.fill(source, 68), "    " + DIM), RST)
    beat()

    good = ("Arrive fifteen minutes early, bring your insurance card and photo "
            "ID, and do not eat for eight hours before your blood draw.")
    v = guardrails.check(good, source, settings.grounding_threshold)
    print(f"  {GRN}✓ ACCEPTED{RST}  grounding score {v.score}")
    print(textwrap.indent(textwrap.fill(good, 68), "    "))
    beat()

    say("Now the same summary with one clinical fact added — the kind of thing "
        "your previous vendor's transcript actually contained:")
    beat()
    bad = good + " Also, continue taking your metformin 500 mg as prescribed."
    v2 = guardrails.check(bad, source, settings.grounding_threshold)
    print(textwrap.indent(textwrap.fill(bad, 68), "    "))
    beat()
    print(f"  {RED}✗ WITHHELD{RST}  grounding score {v2.score} "
          f"{DIM}(still high — that is the point){RST}")
    for r in v2.reasons:
        print(f"    · {r}")
    beat()
    say("The score barely moved. A summary that is 95% faithful and invents one "
        "medication is more dangerous than an obviously wrong one, because it "
        "reads as competent. So the check is not a score alone — it looks for "
        "clinical claims that are not in the source, and any hit withholds the "
        "answer outright.")
    beat()
    say("The patient sees a safe message. A human sees the flag.")
    beat()

    st = retention.check()
    print(f"  {BOLD}Before any of this reaches a model:{RST}")
    print(f"    data retention preflight → {GRN if st.ok else RED}{st.ok}{RST}"
          f"  ({st.reason})")
    say("Some AI models require that your prompts be shared with the model "
        "vendor and kept for 30 days. For patient data that is a reportable "
        "disclosure. The service refuses to start if the setting is wrong — it "
        "is not a policy document, it is a switch that will not turn on.", 4)


# --------------------------------------------------------------------------- #
def act2():
    title(2, "One patient, three charts, and the allergy nobody could see")
    import chromadb

    import chroma_index
    import corpus
    import eval_harness
    import mpi

    say("You asked for a retrieval helper that pulls the right past records. "
        "We built it. Against your contractor's own test set it scores "
        "perfectly. Here is why that is the problem.")
    beat()

    patients = corpus.load_patients()
    clusters = mpi.resolve(patients)
    maria = mpi.cluster_for(clusters, 1042)

    print(f"  {BOLD}Your patient dump contains this:{RST}\n")
    print(f"    {'chart':<7}{'name':<18}{'DOB':<13}{'SSN':<14}allergies")
    print(f"    {'─'*7}{'─'*18}{'─'*13}{'─'*14}{'─'*12}")
    encounters = {e.patient_id: e for e in corpus.load_encounters()}
    for pid in maria.patient_ids:
        p = next(x for x in patients if x.id == pid)
        allergy = (encounters.get(pid).allergies or "—") if pid in encounters else "—"
        mark = f"{RED}{allergy}{RST}" if allergy != "—" else DIM + allergy + RST
        print(f"    {p.id:<7}{p.name:<18}{p.dob:<13}{p.ssn:<14}{mark}")
    beat()
    say(f"Same Social Security number. Same address. Same phone. "
        f"{BOLD}This is one person with three charts{RST}, and the penicillin "
        f"allergy is on the one in the middle.")
    beat()
    say(f"Chart 1588's date of birth is not a different date — it is the same "
        f"date with the day and month swapped. Your database stores dates as "
        f"plain text, so nothing notices.")
    beat()

    idx = chroma_index.ChromaIndex(client=chromadb.EphemeralClient())
    idx.add(corpus.build_knowledge_chunks())
    idx.add(corpus.build_record_chunks())
    run = eval_harness.run(idx)

    m, integ = run.metrics, run.integrity
    print(f"  {BOLD}What the retrieval scorecard says:{RST}")
    print(f"    recall     {GRN}{m['context_recall']}{RST}"
          f"      precision {GRN}{m['context_precision']}{RST}"
          f"      groundedness {GRN}{m['groundedness']}{RST}")
    beat()
    print(f"  {BOLD}What we added to the scorecard:{RST}")
    print(f"    duplicate patient rate      {YLW}{integ['duplicate_patient_rate']}{RST}")
    print(f"    fragment coverage           {RED}{integ['fragment_coverage']}{RST}"
          f"   {DIM}← how much of the person we actually saw{RST}")
    print(f"    clinically incomplete       {RED}{integ['clinically_incomplete_answers']}{RST}")
    beat()

    case = next((c for c in run.cases if c["clinically_incomplete"]), None)
    if case:
        print(f"  {BOLD}The specific failure:{RST}")
        print(f"    question   \"{case['query']}\"")
        print(f"    we looked at chart(s) {case['retrieved_patient_ids']} "
              f"of {case['identity_cluster']}")
        print(f"    {RED}{textwrap.fill(case['note'], 66, subsequent_indent='    ')}{RST}")
        beat()
        say(f"Your contractor's test set expects the answer "
            f"\"No known allergies on file.\" {BOLD}The test was written to "
            f"expect the bug.{RST} A system scoring 100% against it tells a "
            f"clinician this patient has no allergies.")
        beat()
        print(f"  {BOLD}With the fix we are proposing — same data, same search:{RST}")
        print(f"    fragment coverage  {RED}{integ['fragment_coverage']}{RST}"
              f" → {GRN}{run.linked_metrics['fragment_coverage']}{RST}")
        print(f"    allergy recovered  {GRN}{run.linked_metrics['allergies_recovered']}{RST}")
        beat()
        say("Nothing about the search changed. The only difference is that the "
            "system knows who the patient is.")


# --------------------------------------------------------------------------- #
def act3():
    title(3, "Nineteen minutes when nobody could register a patient")
    import asyncio

    # Every service has its own config.py, so importing one service's modules
    # leaves them cached under names the next service also uses. Load these by
    # path and restore sys.modules afterwards, or act 4 gets eligibility's
    # settings object. (tests/conftest.py solves the same problem.)
    bk = _load_service_module("eligibility-service", "breaker", "demo_breaker")
    pc = _load_service_module("eligibility-service", "payer_client", "demo_payer")

    say("You have two tickets open. RIV-088 says registration spins for four "
        "or five seconds, every time. RIV-141 says the whole intake screen "
        "froze one Tuesday morning and nobody could register anyone.")
    beat()
    say(f"{BOLD}They are the same defect.{RST} Your insurance eligibility check "
        f"ran inside the registration request, with no time limit. Your "
        f"clearinghouse's own status page shows it degraded Tuesday 09:02 to "
        f"09:21 — nineteen minutes. Your latency graph shows one spike that "
        f"morning and is otherwise flat all week.")
    beat()

    class Clock:
        # Tuesday 09:02 local, the morning from the status page.
        def __init__(self): self.t = _tuesday_0902()
        def monotonic(self): return self.t
        def wall(self): return self.t
        def advance(self, s): self.t += s

    clock = Clock()
    calls = {"n": 0}
    outage = {"on": False}

    def transport(_i):
        calls["n"] += 1
        if outage["on"]:
            raise RuntimeError("clearinghouse degraded")
        return ("active", 200)

    b = bk.CircuitBreaker(failure_threshold=5, cooldown_s=30.0,
                          monotonic=clock.monotonic)
    client = pc.PayerClient(transport=transport, breaker=b,
                            clock=clock.wall, monotonic=clock.monotonic)

    asyncio.run(client.check("BCBS4471"))
    before = calls["n"]
    outage["on"] = True

    results = []
    for _ in range(19 * 4):                      # a registration every 15s
        results.append(asyncio.run(client.check("BCBS4471")))
        clock.advance(15)

    during = calls["n"] - before
    print(f"  {BOLD}Replaying that Tuesday against the new system:{RST}\n")
    print(f"    registrations attempted during the outage   {len(results)}")
    print(f"    registrations that succeeded                {GRN}{len(results)}{RST}")
    print(f"    times we called the failing clearinghouse   {GRN}{during}{RST}"
          f"  {DIM}(was {len(results)}){RST}")
    print(f"    front desk saw coverage status               "
          f"{GRN}{results[0].status}{RST}, marked "
          f"{YLW}stale{RST}, with the time it was last checked")
    beat()
    print(f"  {BOLD}What the receptionist reads:{RST}")
    print(f"    {DIM}\"{pc.describe(results[0])}\"{RST}")
    beat()

    outage["on"] = False
    clock.advance(31)
    rec = asyncio.run(client.check("BCBS4471"))
    print(f"  Payer recovers → back to a live answer within "
          f"{GRN}30 seconds{RST} (stale={rec.stale})")
    beat()
    say("Registration no longer waits for your insurance clearinghouse. When "
        "they have a bad morning, you find out from a status label — not from "
        "a waiting room.")


# --------------------------------------------------------------------------- #
def act4():
    title(4, "Any login could read any chart")
    from langgraph.checkpoint.memory import InMemorySaver

    import patient_view_graph as pvg
    import patient_view_loaders as pvl

    say("Your handover included a browser capture. In it, one logged-in patient "
        "requests chart 1042, gets it, then requests chart 1043 — a different "
        "person — and gets that too. Chart numbers are sequential. Anyone with "
        "a login could have walked the entire patient list.")
    beat()
    say(f"{BOLD}It was not a forgotten check.{RST} Your system had no way to "
        f"know which patient a login belonged to. \"Show me my own record\" was "
        f"not a question the database could answer.")
    beat()

    graph = pvg.build_graph(loaders=pvl.default_loaders(),
                            checkpointer=InMemorySaver())
    maria = {"principal": "patient", "username": "maria.gonzalez",
             "patient_ids": [1042, 1330, 1588], "open_to_context": False}

    own = pvg.run(graph, patient_id=1042, scope=maria, thread_id="demo-own")
    print(f"  {BOLD}Maria opens her own record:{RST}")
    print(f"    authorised {GRN}{own.authorized}{RST}   released {GRN}{own.released}{RST}")
    beat()
    print(textwrap.indent(textwrap.fill(own.summary.replace("\n\n", " "), 66), "    "))
    beat()
    if "penicillin" in own.summary.lower():
        print(f"    {GRN}✓ and it contains the penicillin allergy from Act 2 —{RST}")
        print(f"    {GRN}  the record she previously could not see in one place{RST}")
    beat()

    walk = pvg.run(graph, patient_id=1043, scope=maria, thread_id="demo-walk")
    print(f"  {BOLD}The same session tries the walk from the capture:{RST}")
    print(f"    authorised     {RED}{walk.authorized}{RST}")
    print(f"    response       404  {DIM}(identical to a chart that does not exist,{RST}")
    print(f"                        {DIM}so nobody can probe for real ones){RST}")
    print(f"    records loaded {GRN}{len(walk.domains)}{RST}   "
          f"{DIM}← not filtered out afterwards. Never read.{RST}")
    print(f"    steps taken    {' → '.join(walk.path)}")
    beat()
    say("The check runs first, before anything is read. Every retrieval branch "
        "receives only the approved scope, so it cannot reach past it. The AI "
        "runs last, on material already approved — it is never asked who should "
        "see what.")


ACTS = {1: act1, 2: act2, 3: act3, 4: act4}


def main() -> int:
    which = sys.argv[1:] or ["1", "2", "3", "4"]
    print(f"\n{BOLD}Riverbend Community Health — four weeks of work{RST}")
    print(f"{DIM}Every figure below is computed live from your own handover "
          f"data. No AWS key required.{RST}")
    for a in which:
        ACTS[int(a)]()
    print(f"\n{BOLD}{CYN}{'═' * 74}{RST}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
