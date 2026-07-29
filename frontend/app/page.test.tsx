import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import DashboardPage from "./page";

/**
 * RVB-W1-U4 — the dashboard defect we introduced in #16.
 *
 * `app/page.tsx` hard-coded DEFAULT_PATIENT_ID = "1042". Once the authorization
 * gate landed, the seeded account `james.obrien` (chart 1043) fetched 1042, got
 * a 404, and landed on an empty dashboard. `maria.gonzalez` worked only because
 * her chart happened to be the hard-coded one — which is the worst kind of
 * passing, because it looks like the feature works.
 */

function captureFetches() {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      urls.push(String(url));
      if (String(url).includes("/api/me")) {
        return Promise.resolve(
          new Response(JSON.stringify(meBody), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ items: [], encounters: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    })
  );
  return urls;
}

let meBody: Record<string, unknown> = {};

beforeEach(() => {
  window.localStorage.setItem("riverbend.token", "t");
  meBody = {};
});

function signedInAs(user: Record<string, unknown>, me: Record<string, unknown>) {
  window.localStorage.setItem("riverbend.user", JSON.stringify(user));
  meBody = me;
}

describe("a patient reads their own binding", () => {
  it("maria.gonzalez (chart 1042) loads 1042", async () => {
    signedInAs(
      { username: "maria.gonzalez", full_name: "Maria Gonzalez", role: "patient", patient_id: 1042 },
      { username: "maria.gonzalez", patient_id: 1042, can_ingest: false }
    );
    const urls = captureFetches();
    render(<DashboardPage />);

    await waitFor(() =>
      expect(urls.some((u) => u.includes("/api/records?patient_id=1042"))).toBe(true)
    );
  });

  it("james.obrien (chart 1043) loads 1043, NOT the old hard-coded 1042", async () => {
    // The regression, as a test. Before this fix he fetched 1042, got a 404,
    // and saw nothing.
    signedInAs(
      { username: "james.obrien", full_name: "James O'Brien", role: "patient", patient_id: 1043 },
      { username: "james.obrien", patient_id: 1043, can_ingest: false }
    );
    const urls = captureFetches();
    render(<DashboardPage />);

    await waitFor(() =>
      expect(urls.some((u) => u.includes("/api/records?patient_id=1043"))).toBe(true)
    );
    expect(urls.some((u) => u.includes("patient_id=1042"))).toBe(false);
  });
});

describe("a staff principal", () => {
  it("keeps a browsable default until the picker lands in #20", async () => {
    signedInAs(
      { username: "frontdesk", full_name: "Front Desk", role: "staff" },
      { username: "frontdesk", patient_id: null, can_ingest: false }
    );
    const urls = captureFetches();
    render(<DashboardPage />);

    await waitFor(() =>
      expect(urls.some((u) => u.includes("/api/records?patient_id=1042"))).toBe(true)
    );
  });
});

describe("a malformed session", () => {
  it("fetches no chart at all rather than someone else's", async () => {
    // scope.py resolves this to a patient permitted nothing. Showing 1042 here
    // would be the exact bug being fixed, wearing a different hat.
    signedInAs(
      { username: "broken", full_name: "Broken", role: "patient", patient_id: "not-a-number" as never },
      { username: "broken", patient_id: "not-a-number", can_ingest: false }
    );
    const urls = captureFetches();
    render(<DashboardPage />);

    await waitFor(() => expect(screen.getByText(/Good day/)).toBeInTheDocument());
    await waitFor(() =>
      expect(urls.some((u) => u.includes("/api/me"))).toBe(true)
    );
    expect(urls.some((u) => u.includes("patient_id="))).toBe(false);
  });
});
