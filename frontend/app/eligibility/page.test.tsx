import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import EligibilityPage from "./page";

/**
 * `RVB-W3-U2`, `RVB-W3-U3`, and the decisions behind them — UI-D16, UI-D17.
 *
 * The interesting assertions here are the two negatives: the override IS shown
 * (UI-D16) and the breaker state is NOT (UI-D17). Both were argued rather than
 * assumed, and both are the kind of thing a later refactor quietly flips.
 */

function mockFetch(handlers: Record<string, { ok: boolean; status: number; body: unknown }>) {
  return vi.fn(async (url: string) => {
    const key = Object.keys(handlers).find((k) => String(url).includes(k));
    const h = key ? handlers[key] : { ok: false, status: 404, body: {} };
    return { ok: h.ok, status: h.status, json: async () => h.body } as Response;
  });
}

beforeEach(() => {
  window.localStorage.setItem("rb.token", "test-token");
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

async function checkCoverage(id = "BCBS-40192") {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/insurance member id/i), id);
  await user.click(screen.getByRole("button", { name: /check coverage/i }));
  return user;
}

async function askAgent(text = "is this active?") {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/^Message$/i), text);
  await user.click(screen.getByRole("button", { name: /^ask$/i }));
  return user;
}

describe("the coverage check", () => {
  it("shows a stale value with what to do about it", async () => {
    // The chip states the fact; the page states the action. Front desk needs
    // both -- "payer unreachable" without "register anyway" is a dead end.
    vi.stubGlobal("fetch", mockFetch({
      "/api/eligibility": {
        ok: true, status: 200,
        body: { status: "active", active: true, stale: true, checked_at: "2026-07-29T09:02:00Z" },
      },
    }));
    render(<EligibilityPage />);
    await checkCoverage();

    await waitFor(() => expect(screen.getByTestId("stale-guidance")).toBeInTheDocument());
    expect(screen.getByTestId("stale-guidance")).toHaveTextContent(/mark coverage as unverified/i);
    expect(screen.getByTestId("stale-guidance")).toHaveTextContent(/do not turn the patient away/i);
  });

  it("does not show stale guidance for a fresh check", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/api/eligibility": {
        ok: true, status: 200,
        body: { status: "active", active: true, stale: false, checked_at: "2026-07-29T09:02:00Z" },
      },
    }));
    render(<EligibilityPage />);
    await checkCoverage();

    await waitFor(() => expect(screen.getByTestId("coverage-checked")).toBeInTheDocument());
    expect(screen.queryByTestId("stale-guidance")).toBeNull();
  });

  it("renders an unverifiable payer as 'could not verify', not a denial", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/api/eligibility": {
        ok: true, status: 200,
        body: { status: "unknown", active: null, stale: false, degraded_reason: "timeout" },
      },
    }));
    render(<EligibilityPage />);
    await checkCoverage();

    await waitFor(() =>
      expect(screen.getByTestId("coverage-chip")).toHaveTextContent(/could not verify/i)
    );
    expect(screen.getByTestId("coverage-chip")).not.toHaveTextContent(/not active/i);
  });

  it("shows pending while the check is in flight", async () => {
    // The visible half of the W3 decoupling (RVB-W3-U3). Registration never
    // blocks on the payer, so the UI must have a state for "asking".
    let release: (v: unknown) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise((r) => { release = r; })));
    render(<EligibilityPage />);
    await checkCoverage();

    await waitFor(() =>
      expect(screen.getByTestId("coverage-chip").dataset.status).toBe("pending")
    );
    // Settled inside act(): leaving the in-flight promise dangling makes React
    // warn on unmount, and a suite with warnings in it gets skimmed.
    await act(async () => {
      release({ ok: true, status: 200, json: async () => ({ status: "active", stale: false }) });
    });
  });

  it("reports a failed check without claiming a status", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/api/eligibility": { ok: false, status: 502, body: { detail: "upstream unavailable" } },
    }));
    render(<EligibilityPage />);
    await checkCoverage();

    await waitFor(() => expect(screen.getByTestId("check-error")).toBeInTheDocument());
    // Critically: it falls back to "not checked", not to a status we invented.
    expect(screen.getByTestId("coverage-chip").dataset.status).toBe("none");
  });
});

describe("the assistant", () => {
  it("shows that an answer was corrected from the payer record", async () => {
    // UI-D16. Observability first: a safety control whose activations are
    // invisible cannot be evaluated.
    vi.stubGlobal("fetch", mockFetch({
      "/api/ai/agent/eligibility": {
        ok: true, status: 200,
        body: { request_id: "r1", reply: "Coverage is not active.", overridden: true,
                tool_called: true, tool_status: "inactive" },
      },
    }));
    render(<EligibilityPage />);
    await askAgent();

    await waitFor(() => expect(screen.getByTestId("override-note")).toBeInTheDocument());
    expect(screen.getByTestId("override-note")).toHaveTextContent(/payer record is the authority/i);
  });

  it("frames an override as the payer being authoritative, not the assistant being wrong", async () => {
    // The copy was argued (UI-D16). "The AI was wrong" teaches staff to distrust
    // a tool they must use fifty times a shift; this explains the hierarchy.
    vi.stubGlobal("fetch", mockFetch({
      "/api/ai/agent/eligibility": {
        ok: true, status: 200,
        body: { reply: "Coverage is active.", overridden: true, tool_called: true },
      },
    }));
    render(<EligibilityPage />);
    await askAgent();

    await waitFor(() => expect(screen.getByTestId("override-note")).toBeInTheDocument());
    const note = screen.getByTestId("override-note").textContent ?? "";
    expect(note).not.toMatch(/wrong|incorrect|mistake|error|hallucinat/i);
  });

  it("flags an answer built on a last-known value", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/api/ai/agent/eligibility": {
        ok: true, status: 200,
        body: { reply: "Coverage was active at 9:02.", stale: true, tool_called: true },
      },
    }));
    render(<EligibilityPage />);
    await askAgent();

    await waitFor(() => expect(screen.getByTestId("agent-stale-note")).toBeInTheDocument());
    expect(screen.getByTestId("agent-stale-note")).toHaveTextContent(/unreachable/i);
  });

  it("routes a service failure through the unavailable outcome, not an empty answer", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/api/ai/agent/eligibility": { ok: false, status: 502, body: { detail: "down" } },
    }));
    render(<EligibilityPage />);
    await askAgent();

    await waitFor(() =>
      expect(screen.getByTestId("answer-panel").dataset.outcome).toBe("unavailable")
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/not an empty result/i);
  });

  it("never surfaces the circuit-breaker state", async () => {
    // UI-D17. Withheld because no reading of it changes the front desk's next
    // action -- register, mark unverified -- not merely because it is internal.
    vi.stubGlobal("fetch", mockFetch({
      "/api/eligibility": {
        ok: true, status: 200,
        body: { status: "unknown", stale: true, degraded_reason: "breaker_open",
                breaker: { state: "half_open", failures: 5 } },
      },
    }));
    render(<EligibilityPage />);
    await checkCoverage();

    await waitFor(() => expect(screen.getByTestId("coverage-stale")).toBeInTheDocument());
    const body = document.body.textContent ?? "";
    expect(body).not.toMatch(/breaker|half.?open|circuit/i);
  });
});
