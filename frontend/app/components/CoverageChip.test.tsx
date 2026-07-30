import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CoverageChip, normalizeStatus } from "./CoverageChip";

/**
 * UI-D15, `RVB-W3-U1`.
 *
 * Two failures this component exists to prevent, and both are billing or
 * clinical rather than cosmetic:
 *
 *   1. A stale value rendered as a current one. The payer was unreachable, so
 *      what we hold is the LAST value we retrieved — the patient may have lost
 *      coverage yesterday.
 *   2. `unknown` rendered as `inactive`. The backend is careful to keep `active`
 *      null rather than false when it could not check; telling someone they have
 *      no coverage when we simply could not verify is how a patient gets turned
 *      away from care they are entitled to.
 */

describe("normalizeStatus", () => {
  it("passes through the three known states", () => {
    expect(normalizeStatus("active")).toBe("active");
    expect(normalizeStatus("inactive")).toBe("inactive");
    expect(normalizeStatus("pending")).toBe("pending");
  });

  it("never turns an unrecognised value into 'inactive'", () => {
    // The whole point. `inactive` is a denial; `unknown` is an absence of
    // information, and they must not collapse.
    for (const raw of ["unknown", "", "ERROR", "null", "n/a", undefined]) {
      expect(normalizeStatus(raw)).toBe("unknown");
    }
  });

  it("is case-insensitive", () => {
    expect(normalizeStatus("ACTIVE")).toBe("active");
  });
});

describe("stale coverage", () => {
  const STALE = {
    status: "active",
    active: true,
    stale: true,
    checked_at: "2026-07-29T09:02:00Z",
    payer: "BlueCross",
  };

  it("never renders a bare status when stale", () => {
    // UI-D15's testable clause. "Active" alone asserts something we do not know.
    render(<CoverageChip coverage={STALE} />);
    expect(screen.getByTestId("coverage-stale")).toBeInTheDocument();
  });

  it("says the payer is unreachable, not merely when it was checked", () => {
    // "as of 9:02am" reads as VERIFIED AT 9:02. The opposite is true: verified
    // at 9:02 and not verifiable since. "unreachable" is the load-bearing word.
    render(<CoverageChip coverage={STALE} />);
    expect(screen.getByTestId("coverage-stale")).toHaveTextContent(/unreachable since/i);
    expect(screen.getByTestId("coverage-stale")).toHaveTextContent(/last confirmed/i);
  });

  it("marks staleness in the DOM so a journey can assert it", () => {
    render(<CoverageChip coverage={STALE} />);
    expect(screen.getByTestId("coverage-chip").dataset.stale).toBe("true");
  });

  it("does not shout — the words carry it, not the colour", () => {
    // A warning shown fifty times a shift is a warning nobody reads, so an
    // active-but-stale chip keeps its ordinary variant.
    render(<CoverageChip coverage={STALE} />);
    const chip = screen.getByTestId("coverage-chip");
    expect(chip.querySelector(".rb-badge--ok")).not.toBeNull();
    expect(chip.querySelector(".rb-badge--bad")).toBeNull();
  });

  it("still qualifies a stale value with no timestamp", () => {
    render(<CoverageChip coverage={{ status: "active", stale: true }} />);
    expect(screen.getByTestId("coverage-stale")).toHaveTextContent(/unreachable since/i);
  });
});

describe("fresh coverage", () => {
  it("shows when it was checked, without the staleness clause", () => {
    render(
      <CoverageChip
        coverage={{ status: "active", active: true, stale: false, checked_at: "2026-07-29T09:02:00Z" }}
      />
    );
    expect(screen.getByTestId("coverage-checked")).toHaveTextContent(/checked/i);
    expect(screen.queryByTestId("coverage-stale")).toBeNull();
  });

  it("renders an unverifiable check as 'could not verify', not as a denial", () => {
    render(<CoverageChip coverage={{ status: "unknown", active: null, stale: false }} />);
    const chip = screen.getByTestId("coverage-chip");
    expect(chip).toHaveTextContent(/could not verify/i);
    expect(chip).not.toHaveTextContent(/not active/i);
    expect(chip.dataset.status).toBe("unknown");
  });

  it("renders a genuine denial as a denial", () => {
    render(<CoverageChip coverage={{ status: "inactive", active: false }} />);
    expect(screen.getByTestId("coverage-chip")).toHaveTextContent(/not active/i);
  });

  it("renders in-flight as checking, with no timestamp", () => {
    render(<CoverageChip coverage={{ status: "pending" }} />);
    expect(screen.getByTestId("coverage-chip")).toHaveTextContent(/checking/i);
    expect(screen.queryByTestId("coverage-checked")).toBeNull();
  });

  it("distinguishes 'not checked' from every checked state", () => {
    render(<CoverageChip coverage={null} />);
    const chip = screen.getByTestId("coverage-chip");
    expect(chip).toHaveTextContent(/not checked/i);
    expect(chip.dataset.status).toBe("none");
  });
});
