import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PatientView } from "./PatientView";

/**
 * UI-D19 and ADR 0015 rule 4, as component tests.
 *
 * The assertion that matters most: **a domain that could not load never reads as
 * a domain that is empty.** "No labs on file" and "labs could not be loaded" are
 * the same pixels and opposite facts, and one of them is the Week-2 allergy
 * failure wearing a different hat.
 */

const OK = {
  patient_id: 1042,
  authorized: true,
  released: true,
  grounded: true,
  summary: "Three visits on file. Penicillin allergy confirmed.",
  path: ["authorize", "assemble", "synthesize"],
  domains: {
    demographics: { domain: "demographics", status: "ok", data: ["Maria Gonzalez, 1984-06-02"] },
    encounters: { domain: "encounters", status: "ok", data: ["Office visit 2026-01-12"] },
    labs: { domain: "labs", status: "ok", data: ["Allergies on file: penicillin"] },
    coverage: { domain: "coverage", status: "ok", data: ["BlueCross, active"] },
  },
};

describe("a released view", () => {
  it("renders each domain independently", () => {
    render(<PatientView view={OK} isOwnRecord />);
    for (const d of ["demographics", "encounters", "labs", "coverage"]) {
      expect(screen.getByTestId(`domain-${d}`)).toBeInTheDocument();
    }
  });

  it("shows the summary and the graph path", () => {
    render(<PatientView view={OK} isOwnRecord />);
    expect(screen.getByTestId("view-summary")).toHaveTextContent(/penicillin/i);
    expect(screen.getByTestId("agent-path")).toHaveTextContent(/authorize/);
  });

  it("flags a summary the grounding check could not tie to the record", () => {
    render(<PatientView view={{ ...OK, grounded: false }} isOwnRecord />);
    expect(screen.getByTestId("summary-ungrounded")).toHaveTextContent(/read the sections/i);
  });
});

describe("UI-D19 — a failed domain is not an empty domain", () => {
  const partial = {
    ...OK,
    domains: {
      ...OK.domains,
      coverage: { domain: "coverage", status: "unavailable", note: "payer timeout" },
    },
  };

  it("keeps the three domains that loaded", () => {
    // One rejected promise must not take down the page and discard real data.
    render(<PatientView view={partial} isOwnRecord />);
    expect(screen.getByTestId("rows-labs")).toHaveTextContent(/penicillin/i);
    expect(screen.getByTestId("rows-encounters")).toBeInTheDocument();
  });

  it("words the failure so it cannot be read as an absence", () => {
    render(<PatientView view={partial} isOwnRecord />);
    const el = screen.getByTestId("unavailable-coverage");
    expect(el).toHaveTextContent(/could not be loaded/i);
    expect(el).toHaveTextContent(/not the same as it being empty/i);
    expect(el).not.toHaveTextContent(/^nothing recorded/i);
  });

  it("words a genuine absence as one, and says the section loaded", () => {
    render(
      <PatientView
        view={{ ...OK, domains: { ...OK.domains, labs: { domain: "labs", status: "ok", data: [] } } }}
        isOwnRecord
      />
    );
    const el = screen.getByTestId("empty-labs");
    expect(el).toHaveTextContent(/loaded correctly and is empty/i);
    expect(el).not.toHaveTextContent(/could not be loaded/i);
  });

  it("announces incompleteness at the TOP, not only in place", () => {
    // Someone who scrolls to the section they care about never sees a notice
    // three sections down.
    render(<PatientView view={partial} isOwnRecord />);
    expect(screen.getByTestId("view-incomplete")).toHaveTextContent(/this view is incomplete/i);
    expect(screen.getByTestId("view-incomplete")).toHaveTextContent(/it is not everything/i);
  });

  it("says nothing about incompleteness when everything loaded", () => {
    render(<PatientView view={OK} isOwnRecord />);
    expect(screen.queryByTestId("view-incomplete")).toBeNull();
  });
});

describe("the human gate renders as a decision, not a delay", () => {
  const paused = {
    patient_id: 1042,
    authorized: true,
    released: false,
    sensitive: true,
    approved: null,
    path: ["authorize", "assemble", "sensitivity_gate"],
    domains: {},
  };

  it("tells a patient their record is being reviewed, not withheld or empty", () => {
    // UI-D18. Three states a patient must be able to tell apart: being reviewed,
    // withheld, and empty. A spinner here would be a hang.
    render(<PatientView view={paused} isOwnRecord />);
    const el = screen.getByTestId("view-pending-review");
    expect(el).toHaveTextContent(/pending review/i);
    expect(el).toHaveTextContent(/not being withheld from you/i);
    expect(el).toHaveTextContent(/waiting on a person/i);
    expect(screen.queryByTestId("path-waiting")).toBeNull();
  });

  it("surfaces a registry outage instead of leaving a silent pause", () => {
    render(
      <PatientView
        view={{ ...paused, approval_error: "the approvals queue is unavailable" }}
        isOwnRecord
      />
    );
    expect(screen.getByTestId("approval-error")).toBeInTheDocument();
  });

  it("distinguishes a reviewed-and-denied view from a pending one", () => {
    render(
      <PatientView
        view={{ ...paused, approved: false, released: false }}
        isOwnRecord
      />
    );
    expect(screen.getByTestId("view-withheld")).toHaveTextContent(/did not release/i);
    expect(screen.queryByTestId("view-pending-review")).toBeNull();
  });

  it("renders a denial as a denial, and says not to retry", () => {
    render(
      <PatientView view={{ patient_id: 1043, authorized: false, domains: {} }} isOwnRecord={false} />
    );
    expect(screen.getByTestId("view-denied")).toHaveTextContent(/do not retry/i);
  });
});

describe("the chart span", () => {
  it("tells a patient their record spans several charts, and that all are included", () => {
    render(
      <PatientView
        view={{
          ...OK,
          domains: {
            ...OK.domains,
            demographics: {
              domain: "demographics", status: "ok",
              data: ["chart 1042", "chart 1330", "chart 1588"],
            },
          },
        }}
        isOwnRecord
      />
    );
    const el = screen.getByTestId("chart-span");
    expect(el).toHaveTextContent(/3 charts/);
    expect(el).toHaveTextContent(/all of them are included/i);
  });

  it("words it differently for staff", () => {
    render(
      <PatientView
        view={{
          ...OK,
          domains: {
            ...OK.domains,
            demographics: {
              domain: "demographics", status: "ok",
              data: ["chart 1042", "chart 1330"],
            },
          },
        }}
        isOwnRecord={false}
      />
    );
    expect(screen.getByTestId("chart-span")).toHaveTextContent(/belonging to the same person/i);
  });

  it("says nothing when the record is a single chart", () => {
    render(<PatientView view={OK} isOwnRecord />);
    expect(screen.queryByTestId("chart-span")).toBeNull();
  });
});
