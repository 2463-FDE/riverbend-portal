"use client";

import { useEffect, useState } from "react";
import { SESSION_EVENT, apiFetch, getToken, getUser } from "./session";

/**
 * Who is using the portal — ADR 0012.
 *
 * This hook decides exactly five things and nothing else:
 *   1. which nav items render
 *   2. what the landing page shows
 *   3. whether a patient picker appears
 *   4. whether the knowledge-ingest control is visible
 *   5. whether the approvals queue is offered (W4, UI-D18)
 *
 * `canApprove` is deliberately a SEPARATE flag from `canIngest`, mirroring the
 * gateway. Ingest changes what the assistant believes; approval discloses one
 * patient's assembled record. Collapsing them into one "admin" boolean is the
 * coarse-role mistake the engagement is already documenting.
 *
 * It has NO authority. The gateway re-derives the scope server-side on every
 * request, so a wrong answer here shows the wrong navigation — it cannot grant
 * access. That asymmetry is what lets this stay a hook rather than a duplicate
 * of `services/gateway/scope.py` in TypeScript.
 *
 * One caveat that is NOT cosmetic: a token issued before the identity binding
 * carries no patient_id, so the *gateway* resolves it as staff. Deploying this
 * phase requires flushing sessions (RVB-U-08). That is a backend concern; it is
 * mentioned here because this is where someone will come looking.
 */

export type Principal =
  | { kind: "patient"; patientId: number | null; canIngest: boolean; canApprove: boolean }
  | { kind: "staff"; patientId: null; canIngest: boolean; canApprove: boolean };

export type PrincipalState =
  | { status: "loading"; principal: null }
  | { status: "ready"; principal: Principal };

/**
 * Mirrors `services/gateway/scope.py:resolve_scope`. Three cases, not two.
 *
 * An earlier draft of the spec said "malformed or absent -> staff". That was
 * wrong in the direction that matters: the backend FAILS CLOSED on a malformed
 * value, resolving it to a patient permitted nothing. Describing it as failing
 * open would have shown staff navigation to a principal the gateway allows
 * nothing — confusing at best, and misleading to whoever reads this next.
 */
export function resolvePrincipal(
  rawPatientId: unknown,
  canIngest = false,
  canApprove = false
): Principal {
  const absent =
    rawPatientId === null ||
    rawPatientId === undefined ||
    rawPatientId === "" ||
    rawPatientId === "None";

  if (absent) return { kind: "staff", patientId: null, canIngest, canApprove };

  // Only a number or a numeric string is a candidate. JS coercion is too eager
  // to trust here: `Number(true)` is 1 and `Number([])` is 0, so a session
  // carrying `patient_id: true` would otherwise resolve to CHART 1 — a real
  // patient's record, reached by a type confusion. Caught by
  // `principal.test.ts` before this shipped.
  //
  // Stricter than the Python side, which would accept `int(True)`. Being
  // stricter is safe: the extra cases all land on "patient permitted nothing",
  // which is the fail-closed direction.
  const isCandidate =
    typeof rawPatientId === "number" ||
    (typeof rawPatientId === "string" && /^\d+$/.test(rawPatientId.trim()));

  if (!isCandidate) {
    return { kind: "patient", patientId: null, canIngest, canApprove };
  }

  const parsed = Number(rawPatientId);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    // Malformed: a patient who can see nothing. NOT staff.
    return { kind: "patient", patientId: null, canIngest, canApprove };
  }
  return { kind: "patient", patientId: parsed, canIngest, canApprove };
}

/**
 * `localStorage` is null during SSR and `AppShell` already hydrates in an
 * effect. Reading storage in render causes hydration drift; defaulting to staff
 * flashes staff navigation to a patient. So "loading" is a real state and
 * callers render a skeleton until it clears.
 */
export function usePrincipal(): PrincipalState {
  const [state, setState] = useState<PrincipalState>({
    status: "loading",
    principal: null,
  });

  // Bumped on login and logout so the effect below re-runs. Without this the
  // hook resolves once, on the login page, where there is no session -- and a
  // mount-only resolution in a root-layout component never revisits it. That
  // defect shipped: it showed a patient the staff menu.
  const [sessionTick, setSessionTick] = useState(0);

  useEffect(() => {
    const bump = () => setSessionTick((n) => n + 1);
    window.addEventListener(SESSION_EVENT, bump);
    return () => window.removeEventListener(SESSION_EVENT, bump);
  }, []);

  useEffect(() => {
    let cancelled = false;

    // No token means no session to resolve. Returning to `loading` rather than
    // falling through to a default matters: `resolvePrincipal(null)` is STAFF,
    // mirroring the gateway where an absent patient_id IS staff, and defaulting
    // to it before login would show the staff menu to whoever logs in next.
    if (!getToken()) {
      setState({ status: "loading", principal: null });
      return;
    }

    // Optimistic first paint from the stored login response, then reconciled
    // against /me. The stored copy can be stale; /me is authoritative for what
    // the UI should render.
    const stored = getUser();
    if (stored) {
      setState({
        status: "ready",
        principal: resolvePrincipal(stored.patient_id, false, false),
      });
    }

    apiFetch("/api/me")
      .then((r) => (r.ok ? r.json() : null))
      .then((me) => {
        if (cancelled || !me) return;
        setState({
          status: "ready",
          principal: resolvePrincipal(
            me.patient_id,
            Boolean(me.can_ingest),
            Boolean(me.can_approve)
          ),
        });
      })
      .catch(() => {
        // /me unreachable. Keep whatever the stored session implied rather than
        // guessing — and if there was none, stay in loading so the caller shows
        // a skeleton instead of the wrong navigation.
      })
      .finally(() => {
        if (!cancelled && !stored) {
          setState((s) =>
            s.status === "ready"
              ? s
              : { status: "ready", principal: resolvePrincipal(null, false, false) }
          );
        }
      });

    return () => {
      cancelled = true;
    };
  }, [sessionTick]);

  return state;
}
