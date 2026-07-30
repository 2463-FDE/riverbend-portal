// Shared types mirroring the Riverbend gateway API contract.

export interface PortalUser {
  username: string;
  full_name: string;
  role: string;
  // W4 / adr/0011. Present for patient-portal accounts, null for staff.
  // Server-derived at login -- the client never asserts its own patient identity.
  patient_id?: number | null;
}

// GET /me. `scope` and `can_ingest` are rendering hints; the gateway re-derives
// both server-side on every request and is the only authority.
export interface MeResponse {
  username: string;
  role: string;
  can_ingest: boolean;
  // W4 / UI-D18. Separate from can_ingest on purpose: ingest changes what the
  // assistant believes, approval discloses one patient's assembled record.
  can_approve?: boolean;
  patient_id: number | string | null;
  scope: {
    principal: "patient" | "staff";
    username: string;
    patient_ids: number[];
    open_to_context: boolean;
  };
}

// POST /ai/summary
export interface SummaryResponse {
  request_id: string;
  summary: string;
  grounded: boolean;
  needs_review: boolean;
  model: string;
  stubbed: boolean;
  usage: {
    refused?: string;
    input_tokens?: number;
    output_tokens?: number;
    est_cost_usd?: number;
    grounding_score?: number;
  };
}

// GET /ai/health -- read on mount so the panel can disable submit BEFORE a
// request is made, rather than spending a round trip to learn something static.
export interface AiHealth {
  status: string;
  stub: boolean;
  retention: { ok: boolean; reason: string; checked: boolean };
}

export interface LoginResponse {
  token: string;
  user: PortalUser;
}

export interface PatientSummary {
  id: number;
  mrn: string;
  name: string;
  dob: string;
  gender: string;
  created_at: string;
}

export interface PatientListResponse {
  items: PatientSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface RecordItem {
  id: number;
  kind: string;
  body: string;
  // Lab-style records may carry structured result fields.
  test?: string;
  value?: string | number;
  unit?: string;
  reference_range?: string;
  status?: string; // normal | abnormal | high | low | ...
}

export interface EncounterBlock {
  encounter: {
    id: number;
    type: string;
    provider: string;
    summary: string;
    date?: string;
  };
  records: RecordItem[];
}

export interface RecordsResponse {
  patient_id: number;
  encounters: EncounterBlock[];
}

export interface Slot {
  id: number;
  provider: string;
  location: string;
  start_at: string;
  end_at: string;
  status: string;
}

export interface SlotsResponse {
  items: Slot[];
}

export interface Appointment {
  id: number;
  patient_id: number;
  provider: string;
  reason: string;
  location?: string;
  start_at?: string;
  end_at?: string;
  status: string;
}

export interface RoiRequest {
  id: number;
  patient_id: number;
  recipient: string;
  recipient_type: string;
  purpose: string;
  date_range_start: string;
  date_range_end: string;
  status: string;
  created_at?: string;
}
