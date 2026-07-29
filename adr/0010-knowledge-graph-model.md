# ADR 0010 — Knowledge-graph model for the patient view (W4)

- **Status:** Accepted
- **Date:** 2026-07-29
- **Spec:** `docs/specs/w4-knowledge-graph-multi-agent.md` §3
- **Related:** ADR 0007 (MPI / `SAME_AS`), ADR 0009 (authorization boundary)
- **Requirement:** `RVB-W4-01`, `RVB-W4-11`, `RVB-W4-12`

## Context

Week 4 pairs multi-agent retrieval with a knowledge graph over the patient
record. Riverbend already has this data in Postgres with foreign keys. The
obvious objection is that a graph adds a modelling layer over a working join.

## Decision

### 1. The schema

```
(Patient)-[:HAD]->(Encounter)-[:WITH]->(Provider)
                      │
                      └-[:PRODUCED]->(Record {kind, status, source_message_id})
(Patient)-[:COVERED_BY]->(Coverage {payer, status, checked_at, stale})
(Patient)-[:SAME_AS]->(Patient)
```

### 2. Why a graph and not the join we already have

Three reasons, in order of weight.

**a. Authorization becomes a reachability question.**
*"May this session see this record?"* is *"is there a path from the session's
authorized patient to this record?"* The graph answers that structurally.

The IDOR exists because the `WHERE patient_id = ?` clause is a thing a developer
has to remember, in every query, forever — and someone forgot it once, in
`records-service`, and nobody noticed until we read a HAR file. Reachability from
an authorized root is a property of the traversal itself; there is no clause to
forget. This is the reason that justifies the layer on its own.

**b. Provenance is an edge property.**
`Record.source_message_id` on the `PRODUCED` edge records which inbound HL7
message produced which record. That matters in W6, where the mapper is shown to
silently drop AL1 (allergy) and RXA (medication) segments — "which records came
from a message we mis-parsed, and what is therefore missing from this chart?" is a
traversal, and it is unanswerable in the current schema.

**c. `SAME_AS` makes W2's finding operational before the merge exists.**
Maria Gonzalez's three fragments become one traversal. ADR 0007 deliberately
proposes *link, don't merge*; the graph is where that link becomes useful
immediately, without a destructive write and without waiting for the MPI project.

### 3. Storage — no new database

The graph is built **in-process at load time** from the existing Postgres rows:
adjacency maps over `patients`, `encounters`, `records`, `providers`,
`insurance_coverages`, plus the proposed `patient_links`.

**We are not adding Neo4j or any graph store this week.** The graph is a modelling
and authorization structure, not an infrastructure commitment. The corpus is a
seeded sample (`RVB-W4-12`) and traversals are depth ≤ 3 from a known root — which
an in-memory adjacency map serves at negligible cost.

**What would justify a real graph store**, recorded now so the decision has a
trigger rather than a vibe:

| Trigger | Why it changes the answer |
|---|---|
| Traversal depth routinely > 3 | In-memory adjacency stops being obviously cheaper than a purpose-built index |
| Cross-patient cohort queries ("all patients seen by provider X with abnormal lab Y") | Fan-out from many roots; also a **new authorization problem**, not just a performance one |
| Graph size beyond working-set memory | Forces paging, at which point a real store is less work than a cache |
| Relationship types needing their own history/versioning | Temporal edges are a graph-database strength and painful to hand-roll |

Below those triggers, a graph database is operational cost for modelling
convenience.

### 4. Loading and freshness

Built at request time from the authorized scope, not maintained as a long-lived
in-memory index. Two reasons: a stale graph containing PHI is a cache nobody
declared, with no invalidation and no retention policy; and building from the
authorized scope means the graph itself **only ever contains authorized nodes**,
which composes with ADR 0009's invariant instead of fighting it.

Cost is bounded by the scope, which is bounded by the authorization gate.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Plain SQL joins** | Works today, and we keep using it underneath. Rejected as the *model* because it reproduces the "remember the WHERE clause" failure that caused D11, and cannot express `SAME_AS` or provenance without schema changes of its own. |
| **Neo4j / a real graph store** | Real operational cost — a service, a backup story, a query language, a version-skew surface — for a seeded sample at depth ≤ 3. Revisit at the triggers above. |
| **Long-lived in-memory graph** | An undeclared PHI cache with no invalidation and no retention policy. Rejected on compliance grounds before performance ones. |
| **Materialized view in Postgres** | Reasonable middle ground and a genuine option if the in-process build becomes a latency problem. Not needed at this corpus size; recorded as the first escalation before a graph database. |

## Consequences

- Authorization and provenance become structural properties rather than remembered clauses.
- No new infrastructure; the graph disappears when the request ends.
- The graph is only as complete as the source rows. It **inherits** the fragmentation (mitigated by `SAME_AS`) and the HL7 drop (D6, surfaced but not fixed until W6). The graph makes both *visible*; it does not repair either, and the client writeup must not imply otherwise.
- Rebuilding per request is wasted work if the same patient view is requested repeatedly. Accepted at this scale; the escalation path is a Postgres materialized view before a graph database.
- `patient_links` does not exist yet (ADR 0007 is Proposed). Until it does, `SAME_AS` is populated from the eval harness's detected duplicates for the seeded sample only — clearly marked as demonstration data, not a production link table.
