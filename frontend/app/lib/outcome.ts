/**
 * What an agent call actually did — ADR 0015, `RVB-AG-25`.
 *
 * A CRUD endpoint has two outcomes: it worked, or it errored. This system has
 * seven, and **five of them return HTTP 200**. Built the default way, all five
 * render as either a spinner that ends or a red box, and one of those is
 * dangerous: a refusal rendered as empty space reads as *"nothing on file"* —
 * the exact shape of the gold-set failure that opened this engagement, where the
 * assistant reported "No known allergies on file" for a patient with a
 * penicillin allergy.
 *
 * codex F12: the seven outcomes were asserted across endpoints whose payload
 * shapes differ, with no contract. Without one, every screen invents its own
 * mapping and they drift apart silently.
 *
 * So the mapping lives here, once, and is tested per endpoint. Same treatment as
 * `resolvePrincipal` mirroring `scope.py`: one place, pinned, so a backend shape
 * change fails a test instead of rendering the wrong state at a clinician.
 */

export type AgentOutcome =
  | "grounded"     // normal
  | "refused"      // worked; the answer does not exist in scope
  | "withheld"     // produced output and chose not to show it
  | "ungrounded"   // generated, but below the grounding floor — do not act on it
  | "overridden"   // the agent's prose contradicted the tool; the tool won
  | "stale"        // correct as of a time, not as of now
  | "unavailable"; // transport or service failure

/** The union of fields the agent endpoints return. All optional by design. */
export interface AgentEnvelope {
  request_id?: string;
  answer?: string;
  reply?: string;
  summary?: string;
  grounded?: boolean;
  refused?: boolean;
  reason?: string;
  needs_review?: boolean;
  overridden?: boolean;
  stale?: boolean;
  citations?: Citation[];
  path?: string[];
  usage?: Record<string, unknown> & { refused?: string; grounding_score?: number };
  detail?: string;
}

export interface Citation {
  marker: number;
  doc_id: string;
  doc_title: string;
  chunk_index: number;
  score: number;
  patient_id?: number | null;
}

/**
 * Order matters, and it is not arbitrary.
 *
 * `unavailable` first: if the call did not complete, nothing else in the body
 * can be trusted. Then `withheld` and `refused` — both are deliberate acts by
 * the system and must never be reported as a successful answer. `overridden` and
 * `stale` are *qualified* successes, so they sit below the outright refusals but
 * above plain grounded. `ungrounded` last among the negatives, because it is the
 * weakest signal and the others subsume it.
 */
export function resolveOutcome(
  data: AgentEnvelope | null | undefined,
  ok = true
): AgentOutcome {
  if (!ok || !data) return "unavailable";

  // W1's summariser signals a guardrail block through `usage.refused` rather
  // than a top-level field. Reading only the top level here is how the withheld
  // state would silently become "grounded".
  const withheldReason = data.usage?.refused;
  if (withheldReason) return "withheld";
  if (data.needs_review === true && data.grounded === false) return "withheld";

  if (data.refused === true) return "refused";
  if (data.overridden === true) return "overridden";
  if (data.stale === true) return "stale";
  if (data.grounded === false) return "ungrounded";
  return "grounded";
}

/** The text the user reads, whichever endpoint produced it. */
export function outcomeBody(data: AgentEnvelope | null | undefined): string {
  return (data?.answer ?? data?.reply ?? data?.summary ?? "").trim();
}

interface Presentation {
  label: string;
  tone: "ok" | "warn" | "info" | "error";
  /** What the reader must take away. Never a raw status word. */
  meaning: string;
}

/**
 * Copy lives beside the discriminator so a new outcome cannot be added without
 * someone writing the sentence a clinician will read.
 *
 * Every `meaning` states what is true, not what the system did. "The system
 * worked and there is no answer here" is actionable; "refused" is not.
 */
export const PRESENTATION: Record<AgentOutcome, Presentation> = {
  grounded: {
    label: "Grounded answer",
    tone: "ok",
    meaning: "Every claim below is supported by the cited sources.",
  },
  refused: {
    label: "No answer in scope",
    tone: "info",
    meaning:
      "The assistant found nothing relevant in the records it is allowed to " +
      "read. This is not an error, and it does not mean the answer is no.",
  },
  withheld: {
    label: "Withheld — needs review",
    tone: "warn",
    meaning:
      "A response was produced and held back by the safety check. A person " +
      "should read the source material directly.",
  },
  ungrounded: {
    label: "Not grounded — do not act on this",
    tone: "warn",
    meaning:
      "The assistant produced text the grounding check could not tie to a " +
      "source. Treat it as unverified.",
  },
  overridden: {
    label: "Corrected from the payer record",
    tone: "info",
    meaning:
      "The assistant's wording disagreed with the coverage check, so the " +
      "checked value is shown instead.",
  },
  stale: {
    label: "Last known value",
    tone: "warn",
    meaning:
      "The payer could not be reached. This is the last value successfully " +
      "retrieved, not the current one.",
  },
  unavailable: {
    label: "Assistant unavailable",
    tone: "error",
    meaning:
      "The request did not complete. Nothing was returned — this is not an " +
      "empty result.",
  },
};

/** An outcome that must never be rendered as a plain answer. */
export function isQualified(outcome: AgentOutcome): boolean {
  return outcome !== "grounded";
}
