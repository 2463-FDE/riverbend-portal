import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SummaryPanel from "./SummaryPanel";

/**
 * Every state `POST /ai/summary` can produce. The spec named three of eight
 * before the codex:rescue review; a state with no rendering is a blank panel in
 * a demo.
 *
 * These are COMPONENT tests on purpose (adr/0013). The withheld case cannot be
 * reached from a browser journey — the stub model is deliberately grounded, so
 * forcing it would mean misconfiguring the grounding threshold, and the
 * resulting test would prove only that a threshold can be misconfigured.
 */

const INSTRUCTIONS =
  "Please arrive fifteen minutes before your appointment and bring your " +
  "insurance card and a photo ID. Do not eat for eight hours beforehand.";

function mockFetch(handlers: Record<string, () => Response>) {
  return vi.fn((url: string) => {
    const key = Object.keys(handlers).find((k) => String(url).includes(k));
    if (!key) throw new Error(`unexpected fetch: ${url}`);
    return Promise.resolve(handlers[key]());
  });
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const HEALTHY = () => json({ status: "ok", stub: true, retention: { ok: true, reason: "", checked: false } });

beforeEach(() => {
  window.localStorage.setItem("riverbend.token", "t");
});

describe("grounded", () => {
  it("renders the summary and says it was checked", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/ai/health": HEALTHY,
        "/api/ai/summary": () =>
          json({
            request_id: "r1",
            summary: "Arrive fifteen minutes early and bring your ID.",
            grounded: true,
            needs_review: false,
            model: "m",
            stubbed: true,
            usage: { grounding_score: 0.92 },
          }),
      })
    );
    render(<SummaryPanel instructions={INSTRUCTIONS} />);
    await userEvent.click(await screen.findByTestId("summarise"));

    const box = await screen.findByTestId("summary-grounded");
    expect(box).toHaveTextContent("Arrive fifteen minutes early");
    expect(box).toHaveTextContent(/checked against the instructions/i);
  });

  it("hands the summary back to the caller", async () => {
    const onSummary = vi.fn();
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/ai/health": HEALTHY,
        "/api/ai/summary": () =>
          json({ request_id: "r", summary: "Bring your card.", grounded: true,
                 needs_review: false, model: "m", stubbed: true, usage: {} }),
      })
    );
    render(<SummaryPanel instructions={INSTRUCTIONS} onSummary={onSummary} />);
    await userEvent.click(await screen.findByTestId("summarise"));
    await waitFor(() => expect(onSummary).toHaveBeenCalledWith("Bring your card."));
  });
});

describe("withheld — the state this panel exists for", () => {
  it("shows the safe message and the review note, never raw model text", async () => {
    const SAFE =
      "A summary could not be generated safely for this text. A staff member will review it.";
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/ai/health": HEALTHY,
        "/api/ai/summary": () =>
          json({
            request_id: "r2",
            summary: SAFE,
            grounded: false,
            needs_review: true,
            model: "m",
            stubbed: true,
            usage: { grounding_score: 0.6 },
          }),
      })
    );
    render(<SummaryPanel instructions={INSTRUCTIONS} />);
    await userEvent.click(await screen.findByTestId("summarise"));

    const box = await screen.findByTestId("summary-withheld");
    expect(box).toHaveTextContent(SAFE);
    expect(box).toHaveTextContent(/held back for review/i);
    // The hallucination this guards against, in the shape it actually took.
    expect(document.body.textContent).not.toMatch(/metformin/i);
    expect(screen.queryByTestId("summary-grounded")).toBeNull();
  });

  it("does not render guardrail reason codes", async () => {
    // They are not in the response and must not be added: returning
    // `invented_medication:metformin` here would hand a clinical string to
    // patients, and hiding it client-side would be theatre. It lives in the
    // audit log.
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/ai/health": HEALTHY,
        "/api/ai/summary": () =>
          json({ request_id: "r", summary: "Held.", grounded: false,
                 needs_review: true, model: "m", stubbed: true, usage: {} }),
      })
    );
    render(<SummaryPanel instructions={INSTRUCTIONS} />);
    await userEvent.click(await screen.findByTestId("summarise"));
    await screen.findByTestId("summary-withheld");
    expect(document.body.textContent).not.toMatch(/invented_|guardrail|reason/i);
  });
});

describe("refusals", () => {
  it.each([
    ["retention_policy", /data-retention configuration/i],
    ["source_too_short", /not enough text/i],
    ["budget", /too long to summarise/i],
    ["guardrail_blocked", /held back for review/i],
    ["model_unavailable", /temporarily unavailable/i],
  ])("renders %s with copy that says what to do", async (refused, matcher) => {
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/ai/health": HEALTHY,
        "/api/ai/summary": () =>
          json({ request_id: "r", summary: "…", grounded: false,
                 needs_review: true, model: "m", stubbed: true,
                 usage: { refused } }),
      })
    );
    render(<SummaryPanel instructions={INSTRUCTIONS} />);
    await userEvent.click(await screen.findByTestId("summarise"));
    expect(await screen.findByTestId(`summary-refused-${refused}`)).toHaveTextContent(
      matcher
    );
  });

  it("handles a refusal reason it has never seen", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/ai/health": HEALTHY,
        "/api/ai/summary": () =>
          json({ request_id: "r", summary: "…", grounded: false,
                 needs_review: true, model: "m", stubbed: true,
                 usage: { refused: "something_new_next_year" } }),
      })
    );
    render(<SummaryPanel instructions={INSTRUCTIONS} />);
    await userEvent.click(await screen.findByTestId("summarise"));
    expect(await screen.findByTestId("summary-refused-unknown")).toBeInTheDocument();
  });
});

describe("retention preflight (RVB-W1-U3)", () => {
  it("disables submit BEFORE any request when retention is unsafe", async () => {
    const summary = vi.fn();
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/ai/health": () =>
          json({ status: "ok", stub: false,
                 retention: { ok: false, reason: "effective mode is 'default'", checked: true } }),
        "/api/ai/summary": () => {
          summary();
          return json({});
        },
      })
    );
    render(<SummaryPanel instructions={INSTRUCTIONS} />);

    await waitFor(() =>
      expect(screen.getByTestId("summarise")).toBeDisabled()
    );
    expect(screen.getByRole("status")).toHaveTextContent(/data-retention/i);
    expect(summary).not.toHaveBeenCalled();
  });

  it("enables submit when retention is safe", async () => {
    vi.stubGlobal("fetch", mockFetch({ "/api/ai/health": HEALTHY }));
    render(<SummaryPanel instructions={INSTRUCTIONS} />);
    await waitFor(() => expect(screen.getByTestId("summarise")).toBeEnabled());
  });
});

describe("transport", () => {
  it("renders a distinct message for a 422", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/ai/health": HEALTHY,
        "/api/ai/summary": () => json({ detail: "bad" }, 422),
      })
    );
    render(<SummaryPanel instructions={INSTRUCTIONS} />);
    await userEvent.click(await screen.findByTestId("summarise"));
    expect(await screen.findByTestId("summary-failed")).toHaveTextContent(
      /could not be accepted/i
    );
  });

  it("renders a retryable message for a 502", async () => {
    // Only distinguishable from success since RVB-U-09 -- the gateway used to
    // return 200 with an error body.
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/ai/health": HEALTHY,
        "/api/ai/summary": () => json({ error: "upstream unavailable" }, 502),
      })
    );
    render(<SummaryPanel instructions={INSTRUCTIONS} />);
    await userEvent.click(await screen.findByTestId("summarise"));
    expect(await screen.findByTestId("summary-failed")).toHaveTextContent(
      /could not be reached/i
    );
  });
});

describe("guards", () => {
  it("will not submit text too short to summarise", async () => {
    vi.stubGlobal("fetch", mockFetch({ "/api/ai/health": HEALTHY }));
    render(<SummaryPanel instructions="hi" />);
    await waitFor(() => expect(screen.getByTestId("summarise")).toBeDisabled());
    expect(screen.getByTestId("too-short-hint")).toBeInTheDocument();
  });
});
