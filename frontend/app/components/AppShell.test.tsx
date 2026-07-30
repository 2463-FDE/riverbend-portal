import { describe, expect, it } from "vitest";
import { visibleNav } from "./AppShell";

/**
 * `RVB-W4-U1`, UI-D2 — one shell, filtered by principal, not two apps.
 *
 * What this is and is not: the filter is a **navigation** decision. Every route
 * it hides is independently enforced at the gateway, which re-derives the scope
 * server-side on every request. A wrong answer here shows the wrong menu; it
 * cannot grant access.
 *
 * That asymmetry is what makes the filter safe to be optimistic — and it is also
 * why these tests do not stand in for authorization tests. The 403s live in
 * `tests/test_w4_approvals.py` and `tests/test_w2_query_scope_idor.py`.
 */

// Mirrors the real list's shape without importing icons into a node test.
const ITEMS = [
  { href: "/", label: "Dashboard", icon: null },
  { href: "/records", label: "Records", icon: null },
  { href: "/knowledge", label: "Knowledge", icon: null },
  { href: "/eligibility", label: "Eligibility", icon: null, staffOnly: true },
  { href: "/intake", label: "Intake", icon: null, staffOnly: true },
  { href: "/roi", label: "Release of Information", icon: null, staffOnly: true },
  { href: "/approvals", label: "Approvals", icon: null, staffOnly: true, needsApprove: true },
];

const labels = (p: Parameters<typeof visibleNav>[1]) =>
  visibleNav(ITEMS, p).map((i) => i.label);

describe("a patient", () => {
  const patient = { kind: "patient" as const, canApprove: false };

  it("does not see staff workflows", () => {
    // Intake and ROI are staff workflows ABOUT patients. A patient seeing
    // "Release of Information" in their own sidebar would reasonably read it as
    // something they can do for themselves, which it is not.
    const got = labels(patient);
    expect(got).not.toContain("Intake");
    expect(got).not.toContain("Release of Information");
    expect(got).not.toContain("Eligibility");
    expect(got).not.toContain("Approvals");
  });

  it("still sees their own record and the assistant", () => {
    const got = labels(patient);
    expect(got).toContain("Dashboard");
    expect(got).toContain("Records");
    expect(got).toContain("Knowledge");
  });

  it("cannot reach approvals even if the capability flag is somehow set", () => {
    // Defence in depth against a bad /me: `staffOnly` is checked first, so a
    // patient with canApprove true still gets nothing. The gateway also refuses.
    expect(labels({ kind: "patient", canApprove: true })).not.toContain("Approvals");
  });
});

describe("staff", () => {
  it("see the operational workflows", () => {
    const got = labels({ kind: "staff", canApprove: false });
    expect(got).toContain("Intake");
    expect(got).toContain("Eligibility");
    expect(got).toContain("Release of Information");
  });

  it("only see Approvals with the approval capability", () => {
    // UI-D18. Separate from ingest, and separate from merely being staff.
    expect(labels({ kind: "staff", canApprove: false })).not.toContain("Approvals");
    expect(labels({ kind: "staff", canApprove: true })).toContain("Approvals");
  });
});

describe("before the principal resolves", () => {
  it("shows the SAFE subset rather than the full menu", () => {
    // Flashing Intake and ROI at a patient for one paint and then removing them
    // is both a worse experience and a worse signal than showing less and adding
    // to it.
    const got = labels(null);
    expect(got).not.toContain("Intake");
    expect(got).not.toContain("Approvals");
    expect(got).toContain("Dashboard");
  });
});
