"""Knowledge graph over the patient record — ADR 0010.

    (Patient)-[:HAD]->(Encounter)-[:WITH]->(Provider)
                          │
                          └-[:PRODUCED]->(Record {kind, status})
    (Patient)-[:COVERED_BY]->(Coverage)
    (Patient)-[:SAME_AS]->(Patient)

Why a graph over a join we already have
---------------------------------------
**Authorization becomes a reachability question.** "May this session see this
record?" is *"is there a path from the session's authorized patient to this
record?"* The IDOR exists because `WHERE patient_id = ?` is a clause a developer
has to remember, in every query, forever — and someone forgot it once in
`records-service` and nobody noticed until we read a HAR file. Reachability from
an authorized root is a property of the traversal; there is no clause to forget.

**`SAME_AS` makes the Week-2 finding operational.** Maria Gonzalez's three
fragments become one traversal, before the MPI merge exists and without a
destructive write.

No new database
---------------
The graph is built **in-process, per request, from the authorized scope**. Two
reasons, and the second is the important one:

  1. At depth ≤ 3 from a known root over a seeded sample, an adjacency map is
     obviously cheap enough.
  2. A long-lived in-memory graph containing PHI is an undeclared cache with no
     invalidation and no retention policy. Building from the authorized scope
     means the graph only ever CONTAINS authorized nodes, which composes with the
     ADR 0009 invariant instead of fighting it.

`Record.source_message_id` — provenance — is deliberately absent: the column does
not exist in `db/schema.sql`, and adding it belongs to W6 where the HL7 mapper
work actually needs it. ADR 0010 records it as Proposed.
"""
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class Node:
    id: str
    kind: str          # patient | encounter | provider | record | coverage
    label: str
    props: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    src: str
    rel: str           # HAD | WITH | PRODUCED | COVERED_BY | SAME_AS
    dst: str


class PatientGraph:
    """An in-process graph, scoped to a set of authorized patient ids."""

    def __init__(self, authorized_patient_ids: Iterable[int]):
        self.authorized = {int(p) for p in authorized_patient_ids}
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._out: dict[str, list[Edge]] = {}

    # -- construction ------------------------------------------------------- #
    def add_node(self, node: Node) -> Node:
        self.nodes[node.id] = node
        return node

    def add_edge(self, src: str, rel: str, dst: str) -> None:
        edge = Edge(src=src, rel=rel, dst=dst)
        self.edges.append(edge)
        self._out.setdefault(src, []).append(edge)

    # -- traversal ---------------------------------------------------------- #
    def neighbours(self, node_id: str, rel: Optional[str] = None) -> list[Node]:
        return [
            self.nodes[e.dst]
            for e in self._out.get(node_id, [])
            if (rel is None or e.rel == rel) and e.dst in self.nodes
        ]

    def reachable_from(self, roots: Iterable[str], max_depth: int = 3) -> set[str]:
        """Node ids reachable from `roots` within `max_depth` hops.

        This is the authorization primitive: a record is visible iff it is
        reachable from an authorized patient root.

        **Breadth-first, deliberately.** A depth-first walk marks a node `seen`
        at whatever depth it happens to be popped at, which is not necessarily
        its shortest path — so a node inside the bound can be reached first via a
        longer route, recorded at that depth, and then never expanded. In this
        graph that silently dropped `record:1330:1`, the chart carrying the
        penicillin allergy, from a traversal rooted at chart 1042.

        That is the Week-2 failure re-created inside the Week-4 fix, and it fails
        the same way: quietly, with a plausible-looking result. BFS visits every
        node at its minimum depth, which is the only correctness condition a
        depth bound has.
        """
        from collections import deque

        seen: set[str] = set()
        queue = deque((r, 0) for r in roots if r in self.nodes)
        while queue:
            node_id, depth = queue.popleft()
            if node_id in seen:
                continue
            seen.add(node_id)
            if depth >= max_depth:
                continue
            for edge in self._out.get(node_id, []):
                if edge.dst not in seen and edge.dst in self.nodes:
                    queue.append((edge.dst, depth + 1))
        return seen

    def records_for(self, patient_ids: Iterable[int]) -> list[Node]:
        roots = [f"patient:{int(p)}" for p in patient_ids]
        reachable = self.reachable_from(roots)
        return [
            self.nodes[n] for n in sorted(reachable)
            if self.nodes[n].kind == "record"
        ]

    def stats(self) -> dict:
        by_kind: dict[str, int] = {}
        for node in self.nodes.values():
            by_kind[node.kind] = by_kind.get(node.kind, 0) + 1
        rels: dict[str, int] = {}
        for edge in self.edges:
            rels[edge.rel] = rels.get(edge.rel, 0) + 1
        return {"nodes": len(self.nodes), "edges": len(self.edges),
                "by_kind": by_kind, "by_rel": rels,
                "authorized_patients": sorted(self.authorized)}


def build(
    authorized_patient_ids: Iterable[int],
    patients,
    encounters,
    same_as_edges: Optional[Iterable[tuple]] = None,
) -> PatientGraph:
    """Build the graph from the authorized scope only.

    Rows outside the scope are not filtered out later — they are never loaded.
    That ordering is the whole point: a graph that contains unauthorized nodes,
    however carefully it is queried afterwards, has already put them in memory,
    in logs, and potentially in a model's context.
    """
    graph = PatientGraph(authorized_patient_ids)
    allowed = graph.authorized
    by_id = {p.id: p for p in patients}

    for pid in sorted(allowed):
        patient = by_id.get(pid)
        if patient is None:
            continue
        graph.add_node(Node(
            id=f"patient:{pid}", kind="patient", label=patient.name,
            props={"patient_id": pid, "dob": patient.dob, "mrn": patient.mrn},
        ))

    # SAME_AS — both endpoints must be authorized, so a link can never widen the
    # scope. It reflects an identity the caller was ALREADY granted.
    for a, b in (same_as_edges or []):
        if int(a) in allowed and int(b) in allowed:
            graph.add_edge(f"patient:{int(a)}", "SAME_AS", f"patient:{int(b)}")
            graph.add_edge(f"patient:{int(b)}", "SAME_AS", f"patient:{int(a)}")

    providers: dict[str, str] = {}
    for i, enc in enumerate(encounters):
        if enc.patient_id not in allowed:
            continue
        enc_id = f"encounter:{enc.patient_id}:{i}"
        graph.add_node(Node(
            id=enc_id, kind="encounter",
            label=f"{enc.encounter_type} {enc.occurred_at[:10]}",
            props={
                "patient_id": enc.patient_id,
                "encounter_type": enc.encounter_type,
                "occurred_at": enc.occurred_at,
                "summary": enc.summary,
                "allergies": enc.allergies,
                "medications": enc.medications,
            },
        ))
        graph.add_edge(f"patient:{enc.patient_id}", "HAD", enc_id)

        if enc.provider:
            prov_id = providers.get(enc.provider)
            if prov_id is None:
                prov_id = f"provider:{len(providers)}"
                providers[enc.provider] = prov_id
                graph.add_node(Node(id=prov_id, kind="provider", label=enc.provider))
            graph.add_edge(enc_id, "WITH", prov_id)

        rec_id = f"record:{enc.patient_id}:{i}"
        graph.add_node(Node(
            id=rec_id, kind="record",
            label=enc.summary or enc.encounter_type,
            props={
                "patient_id": enc.patient_id,
                "kind": enc.encounter_type,
                "summary": enc.summary,
                "allergies": enc.allergies,
                "medications": enc.medications,
                "occurred_at": enc.occurred_at,
            },
        ))
        graph.add_edge(enc_id, "PRODUCED", rec_id)

    return graph
