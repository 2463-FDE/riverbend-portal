# Finding W2-1 — One patient, three charts, and one of them has the allergy

- **Debt ref:** D5 (patient-fork half), twist #3 · **Requirement:** `RVB-W2-08`
- **Severity:** **High — patient safety**
- **Status:** Detected and measured. Remediation designed in `adr/0007`; not implemented (service-sized).
- **Client ticket:** **RIV-160** — already reported, already mis-triaged
- **Found during:** building the Week-2 retrieval helper

---

## What we were asked for

> *"Clinicians waste time hunting through a patient's history. Build me a
> retrieval helper that pulls the right past records when they open a chart."*

We built it. It works. Against your contractor's own gold-set it scores
**recall 1.0, precision 1.0, groundedness 1.0**.

**And it would have hurt someone.**

## What we found

The patient dump you handed over contains this:

| chart | name | DOB | SSN | MRN | allergies recorded |
|---|---|---|---|---|---|
| **1042** | Maria Gonzalez | 1971-03-02 | 412-55-9981 | M4471 | *(none)* |
| **1330** | Maria Gonzale**s** | 1971-03-02 | 412-55-9981 | M4471 | **penicillin** |
| **1588** | M. Gonzalez | 1971-**02-03** | 412-55-9981 | M4471 | *(none)* |

Same Social Security number. Same MRN. Same address. Same phone. Same insurance
member id.

**This is one person with three charts.** Two of them say she has no allergies.
The third says penicillin.

The third chart's date of birth is not a different date — it is the same date with
the day and month transposed. That is what happens when a form and a person's
mental format disagree. `dob` is stored as `TEXT` in your schema, so
`1971-03-02` and `1971-02-03` are simply two unequal strings and nothing notices.

## Why the retrieval helper made it worse, not better

Your contractor's gold-set — `db/seed/goldset.json`, the file used to demonstrate
that the retrieval worked — contains this case:

```json
{
  "query": "show me Maria Gonzalez's allergies",
  "expected_patient_id": 1042,
  "expected_answer": "No known allergies on file."
}
```

**The expected answer is wrong, and the evaluation was built to expect it.** A
retrieval system that scores 100% against this gold-set will confidently tell a
clinician that Maria Gonzalez has no known allergies.

That is not a retrieval-quality problem. No amount of chunking, reranking, or
upgrading the embedding model fixes it. The retriever is doing its job perfectly
on a source of truth that has been split into thirds.

## The measurement

Our eval harness reports both families of number, together, in this order — and
it does that deliberately, because a report showing only the first family would
have *validated* the broken system:

```
RETRIEVAL QUALITY (what was asked for)
  context_recall       1.0
  context_precision    1.0
  groundedness         1.0

DATA INTEGRITY (what we found)
  patient_rows                     5
  distinct_humans                  3
  duplicate_patient_rate           0.333
  fragment_coverage                0.556
  clinically_incomplete_answers    1

  ⚠ CLINICALLY INCOMPLETE ANSWERS
    query:          show me Maria Gonzalez's allergies
    charts today:   [1042] of [1042, 1330, 1588]
    why it matters: chart 1330 records 'penicillin' and was not retrieved; the
                    answer therefore reports a clean allergy history for a
                    patient who has a documented allergy
    once linked:    allergy record recovered = True

WITH THE PROPOSED MATCH KEY (ADR 0007)
  fragment_coverage                0.556 -> 1.0
  allergies recovered              1
```

Both numbers on the left are true at the same time. **Recall 1.0 and fragment
coverage 0.556.** The retriever finds what was indexed. What was indexed is a
third of the patient.

The last block is the same query, the same corpus, the same retriever, the same
embeddings — with one change: the system knows the three charts are one person.
The difference is a documented penicillin allergy.

## Why it matters, in your terms

**Patient safety, not data hygiene.** A clinician opens chart 1042 or 1588, sees
an empty allergy list, and prescribes a penicillin-class antibiotic. The allergy
is recorded. It is on the chart they did not open, and the system has no way to
tell them it exists.

That is not hypothetical. **Dr. Nguyen already filed it as RIV-160:**

> *"Why does the allergy list look different depending on which chart I open for
> the same lady (Maria Gonzalez)?"*

It was triaged as a display inconsistency. He was right and the triage was wrong.

**It is also getting worse every day.** Self-service intake has no match key, so
every visit by a returning patient who types their name slightly differently
creates another fragment. The backlog grows while this is open.

**Regulatory framing.** 45 CFR 164.312(c)(1) requires protecting ePHI from
improper alteration or destruction — integrity. A record that is *silently
incomplete* at the point of clinical decision is an integrity failure whether or
not any byte was altered.

## What we are proposing

Design only — an MPI is service-sized and belongs on the roadmap, not in a
one-week increment. Full detail in **`adr/0007`**:

1. **A deterministic match key on the intake write path** — normalized name + DOB + last-4 SSN, enforced by a unique index. And `dob` migrated from `TEXT` to `DATE`, which alone would have caught fragment 1588.
2. **Probabilistic scoring for near-misses, surfaced to a human — never auto-merged.** Merging two different people creates a chart with one person's allergies and another's medications. A missed link is the status quo; a wrong merge is a *new* harm. That asymmetry decides the design.
3. **Link, do not merge.** A `patient_links` table asserting "these ids are one human", so retrieval can span fragments immediately, reversibly, and auditably — without a destructive write and without waiting for the full MPI project.
4. **The check belongs on the write path**, at intake, when the row is created. Not in retrieval, and not in a nightly job. A nightly reconciliation is still needed for the existing backlog, but that is remediation, not prevention, and it must not be mistaken for the fix.

> **No retrieval tuning fixes a fragmented source of truth.**

## What we shipped this week

- The retrieval helper you asked for — ChromaDB-backed, hybrid dense + lexical, cited answers, refuses rather than guesses.
- The eval harness, reporting retrieval quality **and** data integrity together.
- The detection: `duplicate_patient_rate`, `fragment_coverage`, and the concrete identity splits with the reasons they matched.
- `adr/0007`, the proposed match-key design.

## What we did not do

We did not implement the MPI, and we did not merge any charts. We also did not
"fix" the gold-set — it is evidence, and it should stay exactly as your
contractor wrote it.

**One thing needs your decision:** RIV-160 should be re-triaged from a display bug
to a data-integrity defect, and Dr. Nguyen should be told his instinct was right.
That conversation is yours, not ours.
