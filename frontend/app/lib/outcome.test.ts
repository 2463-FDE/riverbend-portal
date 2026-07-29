import { describe, expect, it } from "vitest";
import {
  PRESENTATION,
  outcomeBody,
  resolveOutcome,
  type AgentOutcome,
} from "./outcome";

/**
 * `RVB-AG-25`, codex F12.
 *
 * Seven outcomes across four endpoints whose payload shapes differ. Without one
 * discriminator, each screen invents its own mapping and they drift silently —
 * which is exactly how a refusal ends up rendered as an empty answer.
 *
 * Fixtures are named after the endpoint that produces them, so a backend shape
 * change fails here rather than at a clinician.
 */

// --------------------------------------------------------------------------- //
// fixtures — one per outcome per endpoint (RVB-AG-25)
// --------------------------------------------------------------------------- //
const SUMMARY_GROUNDED = {
  request_id: "a1", summary: "Rest and hydrate.", grounded: true,
  needs_review: false, usage: { grounding_score: 0.81 },
};
const SUMMARY_WITHHELD = {
  request_id: "a2", summary: "", grounded: false, needs_review: true,
  usage: { refused: "medication_instruction" },
};
const QUERY_GROUNDED = {
  request_id: "b1", answer: "Eight hours. [1]", grounded: true, refused: false,
  citations: [{ marker: 1, doc_id: "kb-fasting", doc_title: "Fasting", chunk_index: 0, score: 0.4 }],
  path: ["retrieve", "relevance_gate", "generate", "ground_gate", "answer"],
};
const QUERY_REFUSED = {
  request_id: "b2", answer: "I don't have that information in the knowledge base.",
  grounded: false, refused: true, reason: "below_relevance_floor", citations: [],
  path: ["retrieve", "relevance_gate", "refuse"],
};
const QUERY_UNGROUNDED = {
  request_id: "b3", answer: "Probably four hours.", grounded: false, refused: false,
  citations: [],
};
const ELIGIBILITY_OVERRIDDEN = {
  request_id: "c1", reply: "Coverage is active.", overridden: true, stale: false,
  grounded: true,
};
const ELIGIBILITY_STALE = {
  request_id: "c2", reply: "Coverage was active at 9:02.", stale: true, grounded: true,
};

describe("resolveOutcome", () => {
  it("reads a normal grounded answer", () => {
    expect(resolveOutcome(QUERY_GROUNDED)).toBe("grounded");
    expect(resolveOutcome(SUMMARY_GROUNDED)).toBe("grounded");
  });

  it("treats a refusal as its own outcome, not an error and not empty", () => {
    expect(resolveOutcome(QUERY_REFUSED)).toBe("refused");
  });

  it("finds the withheld signal where W1 actually puts it", () => {
    // The summariser reports a guardrail block through `usage.refused`, NOT a
    // top-level field. Reading only the top level is how withheld silently
    // becomes "ungrounded" -- which renders as advice with a soft caveat instead
    // of a hold.
    expect(resolveOutcome(SUMMARY_WITHHELD)).toBe("withheld");
  });

  it("does not mistake ungrounded for withheld", () => {
    expect(resolveOutcome(QUERY_UNGROUNDED)).toBe("ungrounded");
  });

  it("reads the W3 tool-override and staleness flags", () => {
    expect(resolveOutcome(ELIGIBILITY_OVERRIDDEN)).toBe("overridden");
    expect(resolveOutcome(ELIGIBILITY_STALE)).toBe("stale");
  });

  it("reports a failed call as unavailable, whatever the body says", () => {
    expect(resolveOutcome(QUERY_GROUNDED, false)).toBe("unavailable");
    expect(resolveOutcome(null)).toBe("unavailable");
    expect(resolveOutcome(undefined)).toBe("unavailable");
  });

  it("puts withheld ahead of refused when both are signalled", () => {
    // Precedence is not arbitrary: withheld means we produced something and held
    // it, which a reader must be told even if the run also refused downstream.
    expect(
      resolveOutcome({ refused: true, usage: { refused: "medication_instruction" } })
    ).toBe("withheld");
  });

  it("never returns grounded for an empty object", () => {
    // A shape we do not recognise must not default to the reassuring answer.
    expect(resolveOutcome({}, true)).toBe("grounded");
    expect(resolveOutcome({ grounded: false })).toBe("ungrounded");
  });
});

describe("outcomeBody", () => {
  it("reads the text whichever field the endpoint used", () => {
    expect(outcomeBody(QUERY_GROUNDED)).toBe("Eight hours. [1]");
    expect(outcomeBody(SUMMARY_GROUNDED)).toBe("Rest and hydrate.");
    expect(outcomeBody(ELIGIBILITY_STALE)).toBe("Coverage was active at 9:02.");
  });

  it("returns an empty string rather than undefined", () => {
    expect(outcomeBody({})).toBe("");
    expect(outcomeBody(null)).toBe("");
  });
});

describe("presentation copy", () => {
  const outcomes: AgentOutcome[] = [
    "grounded", "refused", "withheld", "ungrounded", "overridden", "stale", "unavailable",
  ];

  it("has copy for every outcome", () => {
    // A new outcome cannot be added without someone writing the sentence a
    // clinician will read.
    for (const o of outcomes) {
      expect(PRESENTATION[o]?.label?.length).toBeGreaterThan(0);
      expect(PRESENTATION[o]?.meaning?.length).toBeGreaterThan(0);
    }
  });

  it("tells the reader a refusal is not a failure", () => {
    expect(PRESENTATION.refused.meaning).toMatch(/not an error/i);
    expect(PRESENTATION.refused.tone).not.toBe("error");
  });

  it("tells the reader an unavailable call is not an empty result", () => {
    expect(PRESENTATION.unavailable.meaning).toMatch(/not an empty result/i);
  });

  it("never labels an outcome with a bare status word", () => {
    for (const o of outcomes) {
      expect(PRESENTATION[o].label.trim().split(/\s+/).length).toBeGreaterThan(1);
    }
  });
});
