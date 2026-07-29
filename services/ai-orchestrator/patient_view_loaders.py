"""Domain retrievers for the patient view. One per system, each scope-bound.

Each loader takes the AUTHORIZED id set and returns a `DomainResult`. Two rules
they all follow, and both are tested:

  1. **Re-assert scope at the data layer.** The graph already narrowed the scope
     before fan-out, and each branch receives only that scope. This is defence in
     depth: a loader handed a deliberately widened set still returns only rows
     for ids it was given, and never reaches past them.

  2. **Fail into `unavailable`, not into an exception.** One domain going down
     must not take the view with it. The synthesis node is told what is missing
     and has to say so — a section that silently disappears reads as a complete
     record, which is worse than an honest gap.

The four domains are genuinely different systems with different failure modes,
which is the honest justification for fanning out at all (ADR 0009):

  demographics  Postgres, SQL by id            → DB unavailable
  encounters    graph traversal                → N+1 latency (D8, measured not fixed)
  labs          records produced by encounters → silently incomplete (D6, HL7 drops AL1/RXA)
  coverage      live payer call                → third-party outage (D4, W3)
"""
from typing import Callable, Iterable, Optional, Sequence

import corpus
import knowledge_graph
import mpi
from config import settings
from patient_view_graph import (
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    DomainResult,
)


def _scoped(ids: Iterable[int]) -> set[int]:
    return {int(p) for p in (ids or [])}


def build_graph_for(authorized_ids: Sequence[int]) -> knowledge_graph.PatientGraph:
    """Build the KG from the authorized scope only.

    Rows outside the scope are never LOADED, not filtered out afterwards. A graph
    that contains unauthorized nodes has already put them in memory and in logs,
    however carefully it is queried later.
    """
    allowed = _scoped(authorized_ids)
    patients = [p for p in corpus.load_patients() if p.id in allowed]
    encounters = [e for e in corpus.load_encounters() if e.patient_id in allowed]
    clusters = mpi.resolve(corpus.load_patients(), settings.mpi_match_threshold)
    edges = [
        (a, b) for a, b in mpi.same_as_edges(clusters)
        if a in allowed and b in allowed
    ]
    return knowledge_graph.build(allowed, patients, encounters, same_as_edges=edges)


# --------------------------------------------------------------------------- #
# demographics — Postgres, SQL by id
# --------------------------------------------------------------------------- #
def load_demographics(authorized_ids: Sequence[int]) -> DomainResult:
    allowed = _scoped(authorized_ids)
    rows = [p for p in corpus.load_patients() if p.id in allowed]
    if not rows:
        return DomainResult("demographics", STATUS_OK, [], "no chart on file")

    data = [f"{p.name} (chart {p.id}), date of birth {p.dob}" for p in rows]
    note = ""
    if len(rows) > 1:
        # The W2 finding, surfaced to the PATIENT rather than only in an eval
        # report: she sees that her record is spread across several charts.
        note = (
            f"This record is spread across {len(rows)} charts "
            f"({', '.join(str(p.id) for p in rows)}), which appear to be the "
            f"same person. They have been shown together."
        )
    return DomainResult("demographics", STATUS_OK, data, note)


# --------------------------------------------------------------------------- #
# encounters — graph traversal
# --------------------------------------------------------------------------- #
def load_encounters(authorized_ids: Sequence[int]) -> DomainResult:
    graph = build_graph_for(authorized_ids)
    encounters = [n for n in graph.nodes.values() if n.kind == "encounter"]
    encounters.sort(key=lambda n: n.props.get("occurred_at", ""))
    data = [
        f"{n.props.get('occurred_at', '')[:10]}: {n.props.get('summary', '')}"
        for n in encounters
    ]
    return DomainResult("encounters", STATUS_OK, data)


# --------------------------------------------------------------------------- #
# labs / clinical detail — reachable records
# --------------------------------------------------------------------------- #
def load_labs(authorized_ids: Sequence[int]) -> DomainResult:
    allowed = _scoped(authorized_ids)
    graph = build_graph_for(allowed)
    records = graph.records_for(allowed)

    data = []
    allergies = []
    for node in records:
        summary = node.props.get("summary", "")
        if summary:
            data.append(summary)
        if node.props.get("allergies"):
            allergies.append(node.props["allergies"])

    note = ""
    if allergies:
        data.append("Allergies on file: " + ", ".join(sorted(set(allergies))))
    else:
        # Deliberately explicit. An empty allergy field means "no allergy was
        # recorded at that encounter" — it does not mean the patient has none.
        # Saying nothing here is how a clinician reads an absence as an all-clear.
        note = (
            "No allergy was recorded at these encounters. That is not the same "
            "as having no allergies — confirm verbally."
        )
    return DomainResult("labs", STATUS_OK, data, note)


# --------------------------------------------------------------------------- #
# coverage — a live payer call, and it goes down (D4 / W3)
# --------------------------------------------------------------------------- #
def make_coverage_loader(
    eligibility_lookup: Optional[Callable[[str], dict]] = None,
    member_id_for: Optional[Callable[[Sequence[int]], Optional[str]]] = None,
) -> Callable[[Sequence[int]], DomainResult]:
    """Coverage is the branch that will fail, so it is the one that proves the
    per-domain degradation actually works.

    W3 already owns the resilience (timeout, breaker, last-known cache). This
    loader just has to translate a degraded answer into a `DomainResult` the
    patient view can render honestly.
    """

    def load_coverage(authorized_ids: Sequence[int]) -> DomainResult:
        if eligibility_lookup is None:
            return DomainResult(
                "coverage", STATUS_UNAVAILABLE, [],
                "Coverage information is not connected in this environment.",
            )
        member_id = member_id_for(authorized_ids) if member_id_for else None
        if not member_id:
            return DomainResult("coverage", STATUS_OK, [], "No insurance on file.")

        result = eligibility_lookup(member_id) or {}
        status = result.get("status", "unknown")
        if status == "unknown":
            return DomainResult(
                "coverage", STATUS_UNAVAILABLE, [],
                "Coverage could not be verified right now — the payer is unreachable.",
            )
        label = "Active" if status == "active" else "Not active"
        if result.get("stale"):
            return DomainResult(
                "coverage", STATUS_DEGRADED, [f"Coverage: {label}"],
                f"Last known as of {result.get('checked_at_display', 'earlier')}; "
                f"the payer is currently unreachable, so treat this as provisional.",
            )
        return DomainResult("coverage", STATUS_OK, [f"Coverage: {label}"])

    return load_coverage


def default_loaders(
    eligibility_lookup: Optional[Callable[[str], dict]] = None,
) -> dict[str, Callable]:
    return {
        "demographics": load_demographics,
        "encounters": load_encounters,
        "labs": load_labs,
        "coverage": make_coverage_loader(eligibility_lookup),
    }
