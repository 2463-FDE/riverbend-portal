"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import Card from "./components/Card";
import StatusBadge from "./components/StatusBadge";
import {
  IconCalendar,
  IconLab,
  IconIntake,
  IconRecords,
  IconRoi,
  IconClock,
  IconPin,
  IconStethoscope,
  IconHeart,
} from "./components/icons";
import { apiFetch, getUser } from "./lib/session";
import { usePrincipal } from "./lib/principal";
import type { Appointment, EncounterBlock, RecordItem } from "./lib/types";
import { fmtDateTime, firstName } from "./lib/format";

// Staff have no chart of their own, so the dashboard needs *a* patient to show
// until the name-search picker lands in #20. This is a browsing default for
// staff only -- a patient principal always reads their own binding. It is NOT
// the old DEFAULT_PATIENT_ID, which was applied to everyone and left
// james.obrien staring at an empty dashboard after #16.
const STAFF_BROWSE_PATIENT_ID = 1042;

function isResult(r: RecordItem): boolean {
  return Boolean(r.test || r.value !== undefined || r.reference_range);
}

export default function DashboardPage() {
  const [appts, setAppts] = useState<Appointment[] | null>(null);
  const [results, setResults] = useState<RecordItem[] | null>(null);
  const [name, setName] = useState("there");
  const { status, principal } = usePrincipal();

  // W1 (2/2) — the defect this fixes, stated plainly because we shipped it.
  //
  // This used to read a hard-coded DEFAULT_PATIENT_ID = "1042". After the
  // authorization gate landed in #16, the seeded account `james.obrien` (bound
  // to chart 1043) fetched 1042, received a 404, and landed on an empty
  // dashboard. `maria.gonzalez` worked only because her chart happened to be
  // the hard-coded one.
  //
  // Staff keep a browsable default until the picker arrives in #20; a patient
  // reads their own binding, which is the only value that can be correct for
  // them. A patient whose session is malformed has `patientId: null` and gets no
  // fetch at all rather than someone else's chart.
  const patientId =
    principal?.kind === "patient"
      ? principal.patientId
      : principal?.kind === "staff"
        ? STAFF_BROWSE_PATIENT_ID
        : null;

  useEffect(() => {
    const u = getUser();
    if (u?.full_name) setName(firstName(u.full_name));
  }, []);

  useEffect(() => {
    if (status !== "ready") return;
    if (patientId === null) {
      // Nothing safe to load. Empty is the correct render — silently showing
      // another patient's chart is what we are fixing.
      setAppts([]);
      setResults([]);
      return;
    }

    apiFetch(`/api/appointments?patient_id=${patientId}`)
      .then((r) => (r.ok ? r.json() : []))
      .then((d) => setAppts(Array.isArray(d) ? d : (d.items ?? [])))
      .catch(() => setAppts([]));

    apiFetch(`/api/records?patient_id=${patientId}`)
      .then((r) => (r.ok ? r.json() : { encounters: [] }))
      .then((d) => {
        const encounters: EncounterBlock[] = d.encounters ?? [];
        const recs = encounters.flatMap((e) => e.records ?? []).filter(isResult);
        setResults(recs.slice(0, 5));
      })
      .catch(() => setResults([]));
  }, [status, patientId]);

  const upcoming = (appts ?? [])
    .filter((a) => !["cancelled", "canceled", "completed"].includes(a.status?.toLowerCase()))
    .sort((a, b) => (a.start_at ?? "").localeCompare(b.start_at ?? ""));
  const next = upcoming[0];

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Good day, {name}</h1>
        <p>Here&apos;s a summary of your care at Riverbend Community Health.</p>
      </div>

      <div className="rb-grid rb-grid--2">
        {/* Next appointment */}
        <Card title="Next appointment" icon={<IconCalendar />}
          action={<Link href="/appointments">View all</Link>}>
          {appts === null ? (
            <Loading label="Loading appointments…" />
          ) : next ? (
            <div>
              <div className="rb-listrow__title" style={{ fontSize: "1.05rem" }}>
                {next.reason || "Office visit"}
              </div>
              <div className="rb-listrow__meta" style={{ marginTop: 6 }}>
                <span><IconStethoscope width={15} height={15} /> {next.provider}</span>
                <span><IconClock width={15} height={15} /> {fmtDateTime(next.start_at)}</span>
                {next.location && <span><IconPin width={15} height={15} /> {next.location}</span>}
              </div>
              <div style={{ marginTop: 12 }}>
                <StatusBadge status={next.status} />
              </div>
            </div>
          ) : (
            <div className="rb-empty">
              No upcoming appointments.
              <div style={{ marginTop: 10 }}>
                <Link className="rb-btn rb-btn--primary rb-btn--sm" href="/appointments">
                  Schedule a visit
                </Link>
              </div>
            </div>
          )}
        </Card>

        {/* Recent results */}
        <Card title="Recent results" icon={<IconLab />}
          action={<Link href="/records">View records</Link>}>
          {results === null ? (
            <Loading label="Loading results…" />
          ) : results.length ? (
            <table className="rb-table">
              <thead>
                <tr>
                  <th>Test</th>
                  <th>Value</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {results.map((r) => (
                  <tr key={r.id}>
                    <td>{r.test || r.kind}</td>
                    <td className="rb-table__num">
                      {r.value ?? "—"}
                      {r.unit ? ` ${r.unit}` : ""}
                    </td>
                    <td><StatusBadge status={r.status || "normal"} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="rb-empty">No recent lab results on file.</div>
          )}
        </Card>
      </div>

      {/* Quick actions */}
      <div>
        <h2 style={{ marginBottom: 14 }}>Quick actions</h2>
        <div className="rb-grid rb-grid--4">
          <Tile href="/intake" icon={<IconIntake />} title="Start Intake"
            sub="Complete new-patient forms" />
          <Tile href="/records" icon={<IconRecords />} title="View Records"
            sub="Encounters & lab results" />
          <Tile href="/roi" icon={<IconRoi />} title="Request Records"
            sub="Release of information" />
          <Tile href="/appointments" icon={<IconCalendar />} title="Schedule"
            sub="Find an open appointment" />
        </div>
      </div>

      <Card title="Your care team" icon={<IconHeart />}>
        <p className="rb-muted" style={{ margin: 0 }}>
          Riverbend Community Health — Primary Care &amp; Specialty Clinics. For urgent
          medical concerns, call 911. For portal help, call (555) 014-2200.
        </p>
      </Card>
    </div>
  );
}

function Tile({
  href,
  icon,
  title,
  sub,
}: {
  href: string;
  icon: React.ReactNode;
  title: string;
  sub: string;
}) {
  return (
    <Link href={href} className="rb-tile">
      <span className="rb-tile__icon">{icon}</span>
      <span className="rb-tile__title">{title}</span>
      <span className="rb-tile__sub">{sub}</span>
    </Link>
  );
}

function Loading({ label }: { label: string }) {
  return (
    <div className="rb-loading">
      <span className="rb-spinner rb-spinner--dark" aria-hidden="true" /> {label}
    </div>
  );
}
