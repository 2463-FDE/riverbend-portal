# Finding W4-2 — Four defects in the assembled patient view, all live, none reachable by the existing tests

- **Severity:** one Critical (a refused disclosure was performed anyway), one High
  (clinicians saw an incomplete record), two Medium (the human gate was
  unreachable, and repeat reads re-ran the graph).
- **Status:** all four fixed and verified live. Regression tests added.
- **Found by:** building the `/approvals` UI and then exercising the whole HITL
  cycle against the running stack.

---

## Why these were invisible

The W4 backend has **62 passing tests**, including a thorough suite over
`patient_view_graph`. Every one of them calls `run()` and `resume()` directly with
a hand-built scope. Not one went through the gateway, and the gateway is where
three of these four defects live.

The fourth lives in the boundary between a LangGraph reducer and the response
serializer — a seam no single-layer test can see.

---

## 1. The human gate was unreachable from the product

**Medium, and it would have demoed as working.**

`default_sensitive` is `bool(state.get("cross_patient"))`. Nothing ever set it:
the gateway's `/ai/patient-view/{id}` never sent the field, so the flag was always
absent, the gate never fired, and `GET /ai/approvals` could only ever return an
empty list.

We would have shown the client an approvals screen, said "human-in-the-loop is
implemented", and been right about the code and wrong about the system. **A
control nobody can reach is not a control.**

*Fixed:* the gateway decides whether an assembly is disclosure-shaped, because it
is the only place that knows both the principal and the identity cluster. It
fires when **both** hold: the requester is staff, and the record is merged.

Deliberately narrow, and the narrowness is the design. The graph's own docstring
says a human gate on a high-volume path is a workaround generator rather than a
control:

- **A patient reading their own record is never gated.** That is a 45 CFR 164.524
  right of access, not a disclosure decision — and gating it would deadlock,
  because UI-D18 says a subject may never approve their own release.
- **Staff pulling a single-chart record is never gated.** Nothing is being merged.

## 2. Clinicians saw an incomplete record — the Week-2 finding, inverted

**High. This is the engagement's defining failure pointed at the clinician.**

`authorize` narrowed a staff principal to exactly the id in the URL:

```python
ids = [patient_id] if open_to_context else sorted(allowed_ids)
```

Sound-looking, and documented as "never everything staff could ask for". But
Maria Gonzalez is three charts, and chart **1042 is the fragment without her
penicillin allergy**.

Verified live, same patient, two sessions:

```
maria.gonzalez   penicillin visible: True
                 labs: [... 'Allergies on file: penicillin']

frontdesk        penicillin visible: False
                 labs: ['Annual physical. Unremarkable.']
                 note: "No allergy was recorded at these encounters..."
```

**The patient could see her allergy. Her clinician could not.**

The note is honest — that mitigation works — but a prescriber reading an assembled
patient view got a record that looked complete and omitted a penicillin allergy.
This is the exact shape of the gold-set case that opened the engagement, and the
Week-2 fix had been applied to the patient's own view and not to the clinician's.

*Fixed:* the gateway supplies the identity cluster as the staff scope, so staff
assembly spans the merged record. Supplied by the gateway rather than widened
inside the graph, because `adr/0009` makes the authorize node a *receiver* of
scope and never an author of it. `open_to_context` is unchanged: this narrows
what is assembled, it does not grant anything new.

## 3. A refused disclosure was performed anyway

**Critical.**

`_view_payload` returned `domains` unconditionally, and the `withhold` node cannot
clear them — `domains` carries a merge reducer:

```python
def _merge_domains(left: dict, right: dict) -> dict:
    return {**(left or {}), **(right or {})}
```

so a node returning `{"domains": {}}` is a **no-op**. The assembled content
survived into the response.

Verified live: a staff member denied the release, and the response still contained
`Allergies on file: penicillin`.

```
HTTP 200  released=False approved=False
phi leaked despite denial? True
```

The human gate fired, a person refused the disclosure, the audit log recorded a
refusal — **and the record was returned.** A gate that refuses and then performs
is worse than no gate, because it manufactures evidence of a control that did not
hold.

*Fixed* at the serialization boundary, which is the last reducer-independent
point: nothing is released unless `released` is true. Domain **status** survives
so a caller still learns which sections exist and which failed; the content does
not.

```
phi leaked despite denial? False
labs shape kept: 'ok'  data=[]
```

## 4. Repeat reads resumed the previous run

**Medium.**

The thread id was `view-{username}-{patient_id}` — stable per user and patient, so
a second GET resumed the earlier checkpoint instead of starting a fresh assembly.
Visible as a doubled node path:

```
authorize > plan > retrieve:... > authorize > plan > retrieve:...
```

Stable ids existed so that resume could find the run. That reason is gone: the
approvals registry holds the thread id server-side (UI-D20), so the client never
needs a guessable one.

*Fixed:* a fresh thread per request.

---

## What to take from this

Two of the four are the same mistake in different places: **a check that was
correct about one question and silent about another.** `authorize` was right that
staff should not get everything and silent on whether they should get the whole
person. `withhold` was right that the summary should go and silent on the domains.

That is the third time this pattern has produced a finding in this engagement —
the `patient_scope` IDOR and the four staff-only diagnostic routes were the first
two. It is worth naming as a review question rather than a coincidence:
*what else does this check not decide?*

And the reachability defect is its own lesson. Every test of the sensitivity gate
passed, because every test passed `cross_patient=True` explicitly. **Nothing
asserted that anything in production ever would.** `RVB-AG-12` now has a gateway
test that pins it.
