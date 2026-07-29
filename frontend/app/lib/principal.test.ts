import { describe, expect, it } from "vitest";
import { resolvePrincipal } from "./principal";

/**
 * The UI's principal resolution must mirror `services/gateway/scope.py` exactly.
 *
 * An earlier draft of the UI spec said "malformed or absent patient_id resolves
 * to staff". That was wrong in the direction that matters: the backend FAILS
 * CLOSED on a malformed value, resolving it to a patient permitted nothing.
 * Building to the wrong description would have shown staff navigation to a
 * principal the gateway allows nothing — and taught the next reader the wrong
 * rule. codex:rescue caught it before any code existed.
 *
 * These cases are a transcription of scope.py:80-105. If that changes, this
 * fails, which is the point.
 */

describe("absent -> staff", () => {
  it.each([null, undefined, "", "None"])("%o resolves to staff", (raw) => {
    const p = resolvePrincipal(raw);
    expect(p.kind).toBe("staff");
    expect(p.patientId).toBeNull();
  });
});

describe("malformed -> patient permitted NOTHING, not staff", () => {
  it.each(["not-a-number", "12abc", "NaN", {}, [], true])(
    "%o resolves to a patient with no id",
    (raw) => {
      const p = resolvePrincipal(raw);
      expect(p.kind).toBe("patient");
      expect(p.patientId).toBeNull();
    }
  );

  it("never resolves a malformed value to staff", () => {
    // The specific mistake: "a malformed session is not a licence to see
    // everything" (scope.py:89).
    expect(resolvePrincipal("garbage").kind).not.toBe("staff");
  });
});

describe("valid -> patient with that chart", () => {
  it.each([
    [1042, 1042],
    ["1042", 1042],
    ["1330", 1330],
  ])("%o resolves to patient %i", (raw, expected) => {
    const p = resolvePrincipal(raw);
    expect(p.kind).toBe("patient");
    expect(p.patientId).toBe(expected);
  });

  it("rejects a non-integer id rather than truncating it", () => {
    // 1042.7 truncating to 1042 would silently grant a real chart.
    const p = resolvePrincipal("1042.7");
    expect(p.patientId).toBeNull();
  });
});

describe("can_ingest is carried, not inferred", () => {
  it("defaults to false", () => {
    expect(resolvePrincipal(null).canIngest).toBe(false);
    expect(resolvePrincipal(1042).canIngest).toBe(false);
  });

  it("is taken from the server's answer for either principal", () => {
    expect(resolvePrincipal(null, true).canIngest).toBe(true);
    expect(resolvePrincipal(1042, true).canIngest).toBe(true);
  });
});
