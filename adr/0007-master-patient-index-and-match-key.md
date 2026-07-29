# ADR 0007 — Master Patient Index: a match key on the intake write path (W2)

- **Status:** Proposed (design only — implementation roadmapped)
- **Date:** 2026-07-29
- **Spec:** `docs/specs/w2-rag-knowledge-retrieval.md` §4
- **Debt:** D5 (patient fork half), twist #3
- **Client ticket:** RIV-160
- **Requirement:** `RVB-W2-09`

## Context

The Week-2 ask was a retrieval helper. Building it surfaced something larger.

`db/schema.sql` shows the `patients` table with a `mrn` column explicitly
annotated as *not* used as a match key, and **no unique constraint on any identity
tuple**. `intake.yaml` records `match_key: none`. Self-service intake therefore
writes a new row every time, with no attempt to recognise a returning human.

The client's own data dump contains the consequence: **one person — Maria
Gonzalez — exists as three `patient_id` values**, with three different MRNs and
slight name/DOB spelling variants, all created through self-service intake. The
contractor's retrieval gold-set proves the effect: *"show me Maria Gonzalez's
allergies"* returns records from **one** of the three fragments.

Dr. Nguyen already reported the symptom as **RIV-160** — *"Why does the allergy
list look different depending on which chart I open for the same lady?"* — and it
was triaged as a display inconsistency.

**This is not a retrieval problem.** Our eval harness demonstrates the point
numerically: `context_recall` ≥ 0.8 while `fragment_coverage` ≤ 0.5 in the same
run. The retriever is finding what was indexed, accurately. What was indexed is
one third of the patient.

A clinician opening fragment #2 sees an empty allergy list and prescribes against
it. The penicillin allergy is recorded — on fragment #1 — and the system will
never tell them. That is the patient-safety exposure, and it is live today.

## Decision

Propose an MPI approach. **Design and metric this week; implementation is
service-sized and goes on the roadmap** (`RVB-X-06`).

### 1. A deterministic match key on the intake write path

Normalized `(name, dob)` plus last-4 SSN where present, enforced by a unique index.

Normalization is the load-bearing part and must be specified, not assumed:
casefold, strip diacritics, collapse internal whitespace, drop punctuation,
normalize common nickname forms via an explicit table (not a fuzzy library),
`dob` parsed to a real `DATE` rather than compared as an ISO string.

The current schema stores `dob` as `TEXT` — `1971-03-02` and `03/02/1971` are
different strings and the same day. That alone forks patients.

### 2. Probabilistic candidate scoring for near-misses — surfaced, never auto-merged

Below the deterministic threshold, score candidates (edit distance on name, date
proximity on DOB, address/phone agreement) and surface *"possible duplicate"* to a
human at intake.

**Auto-merging charts is irreversible and can itself cause a safety incident** —
merging two different humans creates a chart with one person's allergies and
another's medications. The asymmetry is decisive: a missed link is the status quo;
a wrong merge is a new harm. Humans decide; the system proposes.

### 3. Link, don't merge

A `patient_links(patient_id_a, patient_id_b, confidence, linked_by, linked_at,
method)` table asserting *"these ids are one human."*

- Retrieval and the W4 knowledge graph can span fragments **immediately**, via a `SAME_AS` edge, without any destructive write.
- The underlying rows stay intact, so the link is auditable and reversible.
- Reversibility matters: an incorrect link is undone by deleting a row, not by reconstructing two charts from a merged one.

### 4. The check belongs on the write path

Not in retrieval. Not in a nightly job. **At intake, when the row is created.**

*No retrieval tuning fixes a fragmented source of truth.* Every day the check is
absent, the backlog grows and the merge problem gets harder.

A nightly reconciliation job is still needed for the existing backlog, but it is
remediation, not prevention, and it must not be mistaken for the fix.

### 5. What ships in W2

- The eval harness's **integrity metrics** — `duplicate_patient_rate`,
  `fragment_coverage`, `identity_split_examples` — reported alongside the standard
  retrieval metrics, in that order, so the narrative is unavoidable.
- The finding, tied to RIV-160.
- This ADR.

Not the MPI itself.

## Consequences

- The client learns that her "retrieval helper" week produced a **patient-safety** finding, sourced entirely from data she handed us.
- RIV-160 is re-triaged from display bug to data-integrity defect. Someone has to tell Dr. Nguyen his instinct was right.
- The eval harness now reports a metric that can **fail** even when retrieval is excellent. That is deliberate and must survive future "let's simplify the dashboard" pressure — a report showing only recall would have *validated* the broken system.
- Implementing the match key will surface existing duplicates that intake has been creating for as long as self-service has been live. The backlog is a migration project with a human review queue, and it should be sized before it is promised.
- Interaction with D5's other half (no idempotency on appointment POST → double-booking, RIV-175) is noted: same root cause family — no uniqueness discipline on writes — different table, W5 scope.
- Interaction with W4: the `SAME_AS` edge is where this design becomes operational before the merge exists.
