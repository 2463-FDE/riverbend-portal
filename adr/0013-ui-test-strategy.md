# ADR 0013 — UI test strategy: what each tier proves, and what it does not

- **Status:** Accepted
- **Date:** 2026-07-29
- **Spec:** `docs/specs/ui-w1-w4.md` §8
- **Debate:** `docs/design-debate-w1-w4.md` UI-D7, UI-D8
- **Closes:** codex finding **R7** — properly this time

## Context

`frontend/` has **no test infrastructure**. CI runs `npm run build` and nothing
else.

That is not merely a gap; it is how a specific mistake survived. codex finding R7
said client-facing surfaces were not acceptance-tested. Four tests were written in
response, named `test_e2e_*`, and they go through the **gateway**. They prove the
API works. They were allowed to stand in for *"the client can see it"*, which they
do not prove, and the name is what made that easy.

**A test whose name overstates its reach is worse than a missing test**, because
a missing test is visible in a coverage gap and an overstated one closes a
finding that is still open.

## Decision

### 1. Two tiers, with a hard line between them

| Tier | Tool | Runs | Proves |
|---|---|---|---|
| **States** | Vitest + Testing Library | default CI gate | Given a response shape, the component renders the right thing — withheld, stale, refused, unavailable, denied |
| **Journeys** | Playwright | opt-in, needs `make up` | A real browser, against the real stack, can complete each week's task |

**Neither is contorted to do the other's job.**

### 2. Why the withheld state is not a Playwright test

The Week-1 withheld state is the most important thing in that panel. It is
nonetheless a **component** test, and the reason is worth writing down.

The stub model is deliberately grounded — it derives its answer from the source
text, so it will not produce an ungrounded summary to be withheld. To force the
state in a browser we would set `SUMMARY_GROUNDING_THRESHOLD` to something
impossible via a compose override. That journey would then prove that a threshold
can be misconfigured. It would prove nothing about the guardrail.

So: given a withheld API response, does the panel render the safe message and the
review flag rather than raw model text? Deterministic, millisecond, in CI. And the
guardrail *logic* already has its own Python test against the real hallucination
transcript (`test_invented_medication_is_caught_despite_high_overlap`).

Three tests, three different claims, none pretending to be another.

### 3. Four journeys, one per week

| Week | Journey |
|---|---|
| W1 | Log in → `/intake` → enter instructions → a grounded summary appears |
| W2 | Ask a chart-shaped clinical question → cited results; the quality screen shows recall beside fragment coverage |
| W3 | With `eligibility-service` stopped → the chip reads stale with a timestamp, and registration still completes |
| W4 | Log in as `maria.gonzalez` → all three charts including the penicillin allergy; chart 1043 shows "not found" |

Happy paths, deliberately. A journey suite that tries to cover every state becomes
slow, then flaky, then ignored — and an ignored suite is worse than an absent one
because it still reports green.

### 4. The gate is weaker than the `--live` gate, and that is correct

The backend `--live` tier is gated by a pytest collection hook that survives a
`-m` override, because a CLI flag must not be able to start **spending money**.

Playwright here spends nothing — the stack runs in stub mode with no credential.
The only cost of running it accidentally is **time**. So the gate is just a
separate npm script, excluded from the default CI job.

**We are not claiming equivalence.** Different risk, different mechanism. Writing
this down so nobody later "hardens" the Playwright gate to match a threat it does
not face, or assumes it carries the same guarantee.

What matters instead is the **failure mode**: a `globalSetup` probe checks the
stack is reachable and fails fast with *"run `make up` first"*, rather than
twenty mystery timeouts.

### 5. The existing tests are renamed, not deleted

`test_e2e_summary_returns_a_grounded_result` → `test_api_summary_returns_a_grounded_result`,
and the same for the other three. They are good tests of a real thing. The name
was the problem.

### 6. No new runtime dependency

Everything added is a `devDependency`. `frontend/package.json` runtime deps stay
`next`, `react`, `react-dom`. A test strategy that changes what ships to a browser
has overstepped.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Playwright only** | Cannot reach the withheld / stale / denied states without contorting configuration. Those states are most of the risk. |
| **Vitest only** | Never opens a browser, so R7 stays open — exactly the failure we are correcting. |
| **No new infra** (rely on `tsc` + `npm run build`) | Type-checks rendering, proves nothing about behaviour. Fastest to feature work and leaves the finding open. |
| **Full pyramid incl. MSW-mocked integration** | A third tier between the two, mocking the gateway. The gateway contract is already covered by the Python API tests; this would duplicate it in a second language. |

## Consequences

**Good.**
- R7 closes against something that actually opens a browser.
- Every named state has a test that can reach it.
- CI stays fast: the default gate adds Vitest only.

**Costs accepted.**
- Two toolchains where there were none. Roughly a day of setup before feature work, agreed with the client up front.
- Playwright needs the full stack, so it will not run in the current CI job. Named as a follow-up rather than pretended away: making it run in CI needs a compose service in the workflow, which is its own decision.
- Journeys are happy-path. Error paths live in the component tier, and if a journey ever starts failing for a reason a component test should have caught, that is a signal the split has drifted.
