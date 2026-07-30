import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { resolvePrincipal, usePrincipal } from "./principal";
import { clearSession, setSession } from "./session";

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

// --------------------------------------------------------------------------- //
// The live-only defect: a mount-only resolution in a root-layout component
// --------------------------------------------------------------------------- //
describe("usePrincipal re-resolves when the session changes", () => {
  /**
   * This is the regression test for a defect that shipped and that every
   * component test passed straight through.
   *
   * `AppShell` lives in the root layout, so it mounts ONCE — on `/login`, where
   * there is no token. A mount-only effect therefore resolved identity as
   * "absent", and `resolvePrincipal(null)` is STAFF (correct, mirroring the
   * gateway, where a session with no patient_id IS staff). After logging in,
   * client-side navigation re-renders the shell but never remounts it, so the
   * principal stayed staff — and a patient was shown Intake, Release of
   * Information and Eligibility in their sidebar.
   *
   * Component tests never saw it because they mount fresh with a token already
   * present. Only a browser, logging in, could.
   */
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("stays in loading with no token instead of defaulting to staff", async () => {
    // The load-bearing half. Falling through to a default before login is what
    // showed the staff menu.
    const { result } = renderHook(() => usePrincipal());
    await waitFor(() => expect(result.current.status).toBe("loading"));
    expect(result.current.principal).toBeNull();
  });

  it("resolves to the patient once a session is written", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        username: "maria.gonzalez", role: "patient", patient_id: "1042",
        can_ingest: false, can_approve: false,
        scope: { principal: "patient", username: "maria.gonzalez",
                 patient_ids: [1042, 1330, 1588], open_to_context: false },
      }),
    } as Response)));

    const { result } = renderHook(() => usePrincipal());
    await waitFor(() => expect(result.current.status).toBe("loading"));

    // The event setSession dispatches is what makes the shell notice.
    act(() => {
      setSession("tok", {
        username: "maria.gonzalez", full_name: "Maria Gonzalez",
        role: "patient", patient_id: 1042,
      });
    });

    await waitFor(() => expect(result.current.principal?.kind).toBe("patient"));
    expect(result.current.principal?.patientId).toBe(1042);
  });

  it("returns to loading on logout rather than leaving the last principal", async () => {
    // Otherwise the next person to reach the login page inherits the previous
    // user's menu.
    setSession("tok", {
      username: "frontdesk", full_name: "Front Desk", role: "staff", patient_id: null,
    });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 401, json: async () => null } as Response)));

    const { result } = renderHook(() => usePrincipal());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    act(() => clearSession());
    await waitFor(() => expect(result.current.status).toBe("loading"));
  });
});
