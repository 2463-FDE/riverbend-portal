# Finding W2-3 — Any patient could read any chart through the knowledge endpoint

- **Severity:** Critical. Cross-patient PHI disclosure, 45 CFR 164.502(a).
- **Status:** Fixed and verified live. Regression tests mutation-checked.
- **Found by:** `codex:rescue` on the Part III design documents (finding F1), then
  confirmed by exploiting the running stack.
- **Introduced:** `#14` (W2). **Reachable by patients since `#16`** (W4 gave
  patients sessions). Present in every merged build between those and this fix.

---

## The exploit

Logged in as `maria.gonzalez` — a patient, whose authorized scope is charts
1042, 1330 and 1588:

```http
POST /ai/knowledge/query
Authorization: Bearer <maria's session>

{"query": "what medications is James OBrien on?", "patient_scope": [1043]}
```

```json
{
  "answer": "Patient James O'Brien (chart 1043). [1]",
  "grounded": true,
  "refused": false,
  "citations": [
    {"doc_id": "chart-1043-3", "doc_title": "James O'Brien — office_visit 2026-02-20",
     "patient_id": 1043}
  ]
}
```

Chart 1043 is a different patient. Maria has no relationship to it. The system
returned his record, asserted it was **grounded**, and cited him by name.

## Why it worked

Two correct-looking pieces with nothing joining them.

The orchestrator selects its collection from the *presence* of a field:

```python
kind = KIND_RECORD if req.patient_scope is not None else KIND_KNOWLEDGE
```

The gateway forwarded the client's payload verbatim:

```python
@app.post("/ai/knowledge/query")
def proxy_kb_query(payload: dict, session: dict = Depends(require_session)):
    return _post("ai", "/query", payload)
```

So the *client* chose which collection to read, and supplied the filter.

The endpoint named `knowledge` was in fact a switch: without `patient_scope` it
read clinic policy, with it, patient records. Nothing in its name, its route, or
its gateway handler said so.

## The comment that made it invisible

`QueryRequest.patient_scope` had carried this the entire time:

```python
# Present only for record-collection queries. The gateway supplies it from
# the session; a client cannot widen its own scope.
```

Both sentences describe the intended design. **Neither was implemented.** The
gateway supplied nothing and stripped nothing.

This is the mechanism worth taking from the finding. `scope.py` was correct.
`resolve_scope` was correct. `require_patient_access` was correct and is applied
on `/patients/{id}` and `/patients/{id}/records`. The W4 authorization work was
sound — it simply never covered this route, and a comment asserting that it did
is why nobody looked. **A comment that describes an invariant is not an
invariant, and it is worse than no comment, because it answers the question a
reviewer would otherwise have asked.**

## Why 253 tests missed it

- `tests/test_w4_authorization.py` tests `scope.py` thoroughly — and `scope.py`
  was never wrong.
- `tests/test_w2_retrieval.py` calls `index.query(..., patient_scope=[...])`
  **directly**, at the layer below the gateway, where supplying a scope is the
  correct interface.
- No test posted a `patient_scope` **through the gateway**, because no client
  ever did — the field only existed for the orchestrator's internal callers.

The gap is a route that no test and no caller exercised, guarded by a comment
saying it was fine.

## The fix

**1. `/ai/knowledge/query` refuses a client-supplied scope — loudly.**

```python
if "patient_scope" in payload:
    raise HTTPException(status_code=400, detail="patient_scope is not accepted here. ...")
```

Rejected rather than stripped: a caller attempting to widen its own scope should
be told no, and a 400 is a thing a test can pin. Silent stripping would have
produced a passing request with quietly different semantics.

The check is on **key presence**, not truthiness. `patient_scope: []` is still
`is not None`, so it flips the collection too — an empty list was an exploit.

**2. `/ai/records/query` — new, and the server owns the scope.**

Chart-shaped questions are a real requirement (`RVB-W2-U5`), so the capability
moves to a route where the gateway derives the scope from the session and
**overwrites** rather than merges. Staff, who are `open_to_context` and therefore
have no fixed id set, must name a patient, which is then checked with the same
`require_patient_access` used by every other record read.

## Verified live, after rebuild

```
1. exploit replayed            -> HTTP 400
2. knowledge query             -> grounded: true, fasting policy answered
3. records query as Maria      -> cited charts [1042, 1330, 1588]
   (client-supplied [1043] discarded)
```

## Regression cover

`tests/test_w2_query_scope_idor.py`, 12 tests. Reverting the gateway to
forwarding the payload verbatim fails 5 of them, including a verbatim replay of
the request above. Mutation-checked, not assumed.

Parametrised over `[1043]`, `[1042, 1043]`, `[]`, and `[999999]` — the empty
list and the caller's own ids included, because the property being defended is
"the collection is never chosen by the client", not "these particular ids are
blocked."

## What this changes about the engagement's claims

`#16`'s PR body says patient records are protected against IDOR. That was true
of the routes it changed and false of the system, and the difference is one
route that a comment excused. The body has been corrected.

The broader lesson matches finding `w1ui-nothing-ever-ran-the-stack`: **the tests
were testing the pieces, and the defect was in the seam.** There, the seam was
between the test environment and the container. Here it is between two services
that each behave correctly given their inputs.

## Follow-up

- An authorization test that enumerates **every** `/ai/*` gateway route and
  asserts each one either derives scope server-side or provably cannot reach the
  record collection. Route coverage, not behaviour coverage — the failure here
  was an unlisted route, and only an enumeration catches that class.
- Tracked in `docs/debt-register.md`.
