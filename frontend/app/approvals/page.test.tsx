import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ApprovalsPage from "./page";

/**
 * `RVB-W4-U3`, `RVB-W4-U4`, ADR 0015 rule 4, UI-D20.
 *
 * The rule under test: a human gate renders as a **decision**, not a delay. So
 * the row says what is being released, both outcomes are explicit, and the
 * opaque approval id is never the label.
 */

const ROW = {
  approval_id: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  patient_id: 1042,
  requested_by: "maria.gonzalez",
  requested_principal: "patient",
  chart_span: 3,
  authorized_ids: [1042, 1330, 1588],
  reason: "disclosure_shaped_assembly",
  created_at: 1_780_000_000,
  describe:
    "Release the record view for chart 1042, assembled across 3 charts, requested by maria.gonzalez.",
};

function mockFetch(handlers: Record<string, { ok: boolean; status: number; body: unknown }>) {
  return vi.fn(async (url: string) => {
    const key = Object.keys(handlers).find((k) => String(url).includes(k));
    const h = key ? handlers[key] : { ok: false, status: 404, body: {} };
    return { ok: h.ok, status: h.status, json: async () => h.body } as Response;
  });
}

const STAFF_ME = { username: "frontdesk", role: "staff", can_ingest: false,
                   can_approve: true, patient_id: null,
                   scope: { principal: "staff", username: "frontdesk", patient_ids: [], open_to_context: true } };

beforeEach(() => {
  window.localStorage.setItem("riverbend.token", "test-token");
  window.localStorage.setItem(
    "riverbend.user",
    JSON.stringify({ username: "frontdesk", full_name: "Front Desk", role: "staff", patient_id: null })
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("the queue", () => {
  it("describes the decision, not the run", async () => {
    // "Approve run view-a3f9" is a button that gets clicked; a sentence
    // describing a disclosure gets read.
    vi.stubGlobal("fetch", mockFetch({
      "/api/me": { ok: true, status: 200, body: STAFF_ME },
      "/api/ai/approvals": { ok: true, status: 200, body: { approvals: [ROW] } },
    }));
    render(<ApprovalsPage />);

    await waitFor(() =>
      expect(screen.getByTestId(`describe-${ROW.approval_id}`)).toBeInTheDocument()
    );
    const el = screen.getByTestId(`describe-${ROW.approval_id}`);
    expect(el).toHaveTextContent(/chart 1042/);
    expect(el).toHaveTextContent(/3 charts/);
    expect(el).toHaveTextContent(/maria\.gonzalez/);
  });

  it("never uses the opaque id as a label", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/api/me": { ok: true, status: 200, body: STAFF_ME },
      "/api/ai/approvals": { ok: true, status: 200, body: { approvals: [ROW] } },
    }));
    render(<ApprovalsPage />);

    await waitFor(() => expect(screen.getByTestId(`approve-${ROW.approval_id}`)).toBeInTheDocument());
    expect(screen.getByTestId(`approve-${ROW.approval_id}`)).not.toHaveTextContent(ROW.approval_id);
    expect(document.body.textContent).not.toContain(ROW.approval_id);
  });

  it("shows which charts are included and why it paused", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/api/me": { ok: true, status: 200, body: STAFF_ME },
      "/api/ai/approvals": { ok: true, status: 200, body: { approvals: [ROW] } },
    }));
    render(<ApprovalsPage />);

    await waitFor(() => expect(screen.getByText("1042, 1330, 1588")).toBeInTheDocument());
    expect(screen.getByText(/disclosure shaped assembly/i)).toBeInTheDocument();
  });

  it("offers both outcomes, neither as a throwaway", async () => {
    // A queue where "approve" is a button and "deny" is a text link has decided
    // for you.
    vi.stubGlobal("fetch", mockFetch({
      "/api/me": { ok: true, status: 200, body: STAFF_ME },
      "/api/ai/approvals": { ok: true, status: 200, body: { approvals: [ROW] } },
    }));
    render(<ApprovalsPage />);

    await waitFor(() => expect(screen.getByTestId(`deny-${ROW.approval_id}`)).toBeInTheDocument());
    const approve = screen.getByTestId(`approve-${ROW.approval_id}`);
    const deny = screen.getByTestId(`deny-${ROW.approval_id}`);
    expect(approve.className).toBe(deny.className);
  });

  it("says when nothing is waiting", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/api/me": { ok: true, status: 200, body: STAFF_ME },
      "/api/ai/approvals": { ok: true, status: 200, body: { approvals: [] } },
    }));
    render(<ApprovalsPage />);
    await waitFor(() => expect(screen.getByTestId("approvals-empty")).toBeInTheDocument());
  });
});

describe("deciding", () => {
  it("shows the outcome rather than just removing the row", async () => {
    // A row vanishing is indistinguishable from a failed request.
    vi.stubGlobal("fetch", mockFetch({
      "/api/me": { ok: true, status: 200, body: STAFF_ME },
      "/api/ai/approvals/": { ok: true, status: 200, body: { released: true, approved: true } },
      "/api/ai/approvals": { ok: true, status: 200, body: { approvals: [ROW] } },
    }));
    render(<ApprovalsPage />);

    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByTestId(`approve-${ROW.approval_id}`)).toBeInTheDocument());
    await user.click(screen.getByTestId(`approve-${ROW.approval_id}`));

    await waitFor(() => expect(screen.getByTestId("decision-outcome")).toBeInTheDocument());
    expect(screen.getByTestId("decision-outcome")).toHaveTextContent(/released/i);
  });

  it("reports a denial as a denial", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/api/me": { ok: true, status: 200, body: STAFF_ME },
      "/api/ai/approvals/": { ok: true, status: 200, body: { released: false, approved: false } },
      "/api/ai/approvals": { ok: true, status: 200, body: { approvals: [ROW] } },
    }));
    render(<ApprovalsPage />);

    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByTestId(`deny-${ROW.approval_id}`)).toBeInTheDocument());
    await user.click(screen.getByTestId(`deny-${ROW.approval_id}`));

    await waitFor(() => expect(screen.getByTestId("decision-outcome")).toBeInTheDocument());
    expect(screen.getByTestId("decision-outcome")).toHaveTextContent(/not released/i);
  });

  it("says nothing was released when the decision fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (String(url).includes("/api/me")) {
        return { ok: true, status: 200, json: async () => STAFF_ME } as Response;
      }
      if (String(url).includes("/api/ai/approvals/")) throw new Error("network");
      return { ok: true, status: 200, json: async () => ({ approvals: [ROW] }) } as Response;
    }));
    render(<ApprovalsPage />);

    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByTestId(`approve-${ROW.approval_id}`)).toBeInTheDocument());
    await user.click(screen.getByTestId(`approve-${ROW.approval_id}`));

    await waitFor(() => expect(screen.getByTestId("decision-error")).toBeInTheDocument());
    expect(screen.getByTestId("decision-error")).toHaveTextContent(/nothing was released/i);
  });
});

describe("authorization", () => {
  it("explains that approval is a separate permission from ingest", async () => {
    // UI-D18. Collapsing the two into one "admin" flag is the coarse-role
    // mistake this engagement is already documenting.
    vi.stubGlobal("fetch", mockFetch({
      "/api/me": { ok: true, status: 200, body: { ...STAFF_ME, can_approve: false } },
      "/api/ai/approvals": { ok: false, status: 403, body: { detail: "no" } },
    }));
    render(<ApprovalsPage />);

    await waitFor(() => expect(screen.getByTestId("approvals-denied")).toBeInTheDocument());
    expect(screen.getByTestId("approvals-denied")).toHaveTextContent(
      /separate permission from adding knowledge-base documents/i
    );
  });

  it("distinguishes a registry outage from an empty queue", async () => {
    // "Nothing waiting" when the registry is down would be a lie that reads as
    // reassurance.
    vi.stubGlobal("fetch", mockFetch({
      "/api/me": { ok: true, status: 200, body: STAFF_ME },
      "/api/ai/approvals": { ok: false, status: 503, body: { detail: "registry down" } },
    }));
    render(<ApprovalsPage />);

    await waitFor(() => expect(screen.getByTestId("approvals-unavailable")).toBeInTheDocument());
    expect(screen.queryByTestId("approvals-empty")).toBeNull();
    expect(screen.getByTestId("approvals-unavailable")).toHaveTextContent(/nothing has been disclosed/i);
  });
});
