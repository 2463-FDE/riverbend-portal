-- 009: bind a login to the patient it belongs to (W4, adr/0011).
--
-- Why this migration exists
-- -------------------------
-- The Week-4 ask is "let patients see their OWN labs and visit summaries."
-- "Own" is not expressible in this schema: `users` has username, password_hash,
-- full_name, role, is_active — and no reference to a patient.
--
-- That is the real shape of debt D11. The missing ownership check on
-- GET /patients/{id}/records is a SYMPTOM; the cause is that the system never
-- modelled who a login belongs to. `require_session` could not have been fixed
-- by adding a comparison, because there was nothing to compare.

ALTER TABLE users ADD COLUMN IF NOT EXISTS patient_id INTEGER REFERENCES patients(id);

-- Nullable ON PURPOSE: staff accounts have no patient identity, and that is a
-- legitimate state rather than missing data.
--
-- Unique WHERE NOT NULL: two logins pointing at one patient row would make the
-- accounting-of-disclosures question ("who viewed this patient?", W10)
-- unanswerable, because a read could not be attributed to a person.
CREATE UNIQUE INDEX IF NOT EXISTS users_patient_id_uniq
    ON users (patient_id) WHERE patient_id IS NOT NULL;

COMMENT ON COLUMN users.patient_id IS
    'Patient-portal accounts point at their own chart. NULL for staff accounts. '
    'Server-derived at login and carried in the session; a request can never '
    'assert its own patient identity. See adr/0011.';

-- Demonstration patient-portal accounts are created by db/seed/generate_seed.py,
-- which owns password hashing so the algorithm, iteration count and salt live in
-- exactly one place. This migration only guarantees the column and constraint.
