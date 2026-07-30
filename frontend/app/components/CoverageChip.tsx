"use client";

/**
 * Coverage status, with where it came from — UI-D15, `RVB-W3-U1`.
 *
 * The decision this component encodes: **staleness is carried by the words, not
 * the colour.** A red banner shown fifty times a shift is a warning nobody
 * reads, so the tone stays neutral and the copy does the work.
 *
 * And the copy is specific for a reason. "Active · as of 9:02am" reads as
 * *verified at 9:02*, which is the opposite of what happened — it was verified at
 * 9:02 and **has not been verifiable since**. The word "unreachable" is the
 * difference between a stale value and an old one, so it is not optional.
 *
 * `unknown` is not `inactive`. The backend is careful about that (`active` is
 * `null`, never `false`, when the payer could not answer) and this component must
 * not undo it: telling someone they have no coverage when we simply could not
 * check is how a patient gets turned away from care they are entitled to.
 */

export type CoverageStatus = "active" | "inactive" | "pending" | "unknown";

export interface Coverage {
  status?: string;
  active?: boolean | null;
  stale?: boolean;
  checked_at?: string;
  payer?: string | null;
  degraded_reason?: string;
}

const LABEL: Record<CoverageStatus, string> = {
  active: "Active",
  inactive: "Not active",
  pending: "Checking",
  unknown: "Could not verify",
};

// Maps onto the existing rb-badge variants. No new palette (RVB-U-01).
const VARIANT: Record<CoverageStatus, string> = {
  active: "rb-badge--ok",
  inactive: "rb-badge--bad",
  pending: "rb-badge--neutral",
  unknown: "rb-badge--warn",
};

export function normalizeStatus(raw?: string): CoverageStatus {
  const v = (raw ?? "").toLowerCase();
  if (v === "active" || v === "inactive" || v === "pending") return v;
  // Anything unrecognised is `unknown`, never `inactive`. An unknown status
  // shown as "not active" is a clinical and billing error, not a display bug.
  return "unknown";
}

export function formatCheckedAt(iso?: string): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function CoverageChip({ coverage }: { coverage: Coverage | null | undefined }) {
  if (!coverage) {
    return (
      <span className="rb-badge rb-badge--neutral" data-testid="coverage-chip" data-status="none">
        Not checked
      </span>
    );
  }

  const status = normalizeStatus(coverage.status);
  const stale = coverage.stale === true;
  const at = formatCheckedAt(coverage.checked_at);

  return (
    <span
      className="rb-coverage"
      data-testid="coverage-chip"
      data-status={status}
      data-stale={stale ? "true" : "false"}
    >
      <span className={`rb-badge ${VARIANT[status]}`}>{LABEL[status]}</span>

      {/*
        A stale chip may NEVER render a bare status (UI-D15). Showing "Active"
        with no qualification asserts something we do not know — the payer was
        unreachable, so this is the last value we successfully retrieved.
      */}
      {stale && (
        <span className="rb-coverage__note" data-testid="coverage-stale">
          last confirmed {at ?? "earlier"} · payer unreachable since
        </span>
      )}

      {!stale && at && status !== "pending" && (
        <span className="rb-coverage__note" data-testid="coverage-checked">
          checked {at}
        </span>
      )}

      {coverage.payer && (
        <span className="rb-coverage__payer" data-testid="coverage-payer">
          {coverage.payer}
        </span>
      )}
    </span>
  );
}
