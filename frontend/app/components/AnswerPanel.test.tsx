import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnswerPanel } from "./AnswerPanel";

/**
 * ADR 0015 rules 1–3, as component tests (`RVB-AG-07`).
 *
 * The failure these guard against is specific and has already happened once in
 * this system: a refusal rendered as an empty result reads as *"nothing on
 * file"*, and the gold-set case that opened this engagement is an assistant
 * reporting "No known allergies on file" for a patient with a penicillin
 * allergy.
 */

const CITED = [
  { marker: 1, doc_id: "kb-fasting", doc_title: "Pre-visit fasting instructions",
    chunk_index: 0, score: 0.412 },
];

describe("rule 1 — a refusal is a first-class answer", () => {
  it("renders a refusal without the error styling", () => {
    render(<AnswerPanel data={{ refused: true, answer: "I don't have that.", citations: [] }} />);

    const panel = screen.getByTestId("answer-panel");
    expect(panel.dataset.outcome).toBe("refused");
    expect(panel.querySelector(".rb-alert--err")).toBeNull();
  });

  it("says a refusal is not an error and does not mean no", () => {
    render(<AnswerPanel data={{ refused: true, answer: "x" }} />);
    expect(screen.getByText(/not an error/i)).toBeInTheDocument();
    expect(screen.getByText(/does not mean the answer is no/i)).toBeInTheDocument();
  });

  it("never renders a refusal as blank space", () => {
    render(<AnswerPanel data={{ refused: true, answer: "" }} />);
    // Either the body or the stated-absence line must be present. Silence is
    // the one thing this component may not do.
    expect(screen.getByTestId("answer-no-body")).toHaveTextContent(/stated absence/i);
  });

  it("distinguishes a failed call from an empty answer", () => {
    render(<AnswerPanel data={{}} ok={false} />);
    expect(screen.getByTestId("answer-panel").dataset.outcome).toBe("unavailable");
    expect(screen.getByRole("alert")).toHaveTextContent(/not an empty result/i);
  });
});

describe("rule 1 — the qualified 200s each get their own treatment", () => {
  it.each([
    ["withheld", { needs_review: true, grounded: false, usage: { refused: "medication_instruction" } }, /held back/i],
    ["ungrounded", { grounded: false, answer: "maybe four hours" }, /unverified/i],
    ["overridden", { overridden: true, reply: "Coverage is active." }, /disagreed with the coverage check/i],
    ["stale", { stale: true, reply: "Active at 9:02." }, /could not be reached/i],
  ])("renders %s distinctly", (outcome, data, copy) => {
    render(<AnswerPanel data={data} />);
    expect(screen.getByTestId("answer-panel").dataset.outcome).toBe(outcome);
    expect(screen.getByText(copy)).toBeInTheDocument();
  });

  it("still shows the text on a qualified outcome, under the qualifier", () => {
    // Hiding the body pushes people to re-run until they get an unqualified
    // answer, which is worse than showing it framed.
    render(<AnswerPanel data={{ grounded: false, answer: "Probably four hours." }} />);
    expect(screen.getByTestId("answer-body")).toHaveTextContent("Probably four hours.");
    expect(screen.getByText(/do not act on this/i)).toBeInTheDocument();
  });
});

describe("rule 2 — provenance is inline", () => {
  it("renders citations with the answer, not behind a toggle", () => {
    render(<AnswerPanel data={{ grounded: true, answer: "Eight hours. [1]", citations: CITED }} />);

    expect(screen.getByTestId("citations")).toBeInTheDocument();
    expect(screen.getByText("Pre-visit fasting instructions")).toBeVisible();
    // No disclosure widget: the sources must not be collapsed.
    expect(document.querySelector("details")).toBeNull();
  });

  it("shows the relevance score beside each source", () => {
    render(<AnswerPanel data={{ grounded: true, answer: "x", citations: CITED }} />);
    expect(screen.getByText(/relevance 0\.412/)).toBeInTheDocument();
  });

  it("flags a grounded answer that cited nothing", () => {
    // Grounded with no sources is a contradiction, and silently rendering it as
    // a normal answer is how an ungrounded claim gets a green tick.
    render(<AnswerPanel data={{ grounded: true, answer: "Eight hours.", citations: [] }} />);
    expect(screen.getByTestId("grounded-without-citations")).toHaveTextContent(/unverified/i);
  });
});

describe("rule 3 — the path replaces the spinner", () => {
  it("renders the graph path the assistant actually ran", () => {
    render(
      <AnswerPanel
        data={{ grounded: true, answer: "x", path: ["retrieve", "relevance_gate", "answer"] }}
      />
    );
    const path = screen.getByTestId("agent-path");
    expect(path).toHaveTextContent("retrieve");
    expect(path).toHaveTextContent("relevance gate");
  });

  it("labels the path as steps run, never as reasoning", () => {
    // RVB-AG-08. It is the node sequence, not an explanation of the model's
    // thinking, and calling it the latter would be an overstatement.
    render(<AnswerPanel data={{ grounded: true, answer: "x", path: ["retrieve"] }} />);
    expect(screen.getByTestId("agent-path")).toHaveTextContent(/steps the assistant ran/i);
    expect(screen.queryByText(/reasoning/i)).toBeNull();
  });

  it("shows progress while a call is open", () => {
    render(<AnswerPanel data={null} loading pendingPath={["retrieve"]} />);
    expect(screen.getByTestId("agent-path")).toHaveTextContent(/running/i);
  });

  it("renders an empty hint before anything is asked", () => {
    render(<AnswerPanel data={null} emptyHint="Ask a question." />);
    expect(screen.getByTestId("answer-empty")).toHaveTextContent("Ask a question.");
  });
});
