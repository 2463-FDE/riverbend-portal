"""Build the retrieval corpus from the artifacts the client handed over.

Two sources, two collections, two very different privacy postures:

  * ``db/seed/patients.csv`` + ``db/seed/encounters.csv`` — the patient dump.
    Goes to ``riverbend_records``, which is a **PHI store**. Direct identifiers
    that retrieval does not need are stripped; clinical content and encounter
    dates are kept, because a record corpus stripped of dates cannot answer
    "what did her last three visits say?" — that would destroy the feature in
    order to protect it. The protection is the scope filter, not a scrub.

  * A small set of clinic policy documents. Goes to ``riverbend_knowledge``,
    readable by any authenticated session, scrubbed leniently so the effective
    dates and the clinic's own phone number survive.

Reading from the CSVs rather than Postgres is deliberate: the tests stay hermetic,
and these files are literally the dump we were given.

Quota discipline (`RVB-W2-11`): ``settings.corpus_max_chunks`` caps the corpus and
the cap is logged. This is a quota-risk week and an uncapped ingest over a full
record dump is exactly the failure the client packet warns about.
"""
import csv
import os
from dataclasses import dataclass
from typing import Optional

import deidentify
from chunking import chunk_text
from config import settings
from index_port import KIND_KNOWLEDGE, KIND_RECORD, IndexChunk
from mpi import PatientRow

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SEED_DIR = os.path.join(REPO_ROOT, "db", "seed")


class CorpusCapExceeded(RuntimeError):
    """Raised when an ingest would push the corpus past its configured cap."""


@dataclass
class Encounter:
    patient_id: int
    encounter_type: str
    provider: str
    summary: str
    allergies: str
    medications: str
    occurred_at: str


def load_patients(path: Optional[str] = None) -> list[PatientRow]:
    path = path or os.path.join(SEED_DIR, "patients.csv")
    rows: list[PatientRow] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append(PatientRow(
                id=int(row["id"]),
                name=row.get("name", ""),
                dob=row.get("dob", ""),
                ssn=row.get("ssn", ""),
                mrn=row.get("mrn", ""),
                address=row.get("address", ""),
                phone=row.get("phone", ""),
                email=row.get("email", ""),
                created_via=row.get("created_via", ""),
            ))
    return rows


def load_encounters(path: Optional[str] = None) -> list[Encounter]:
    path = path or os.path.join(SEED_DIR, "encounters.csv")
    out: list[Encounter] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append(Encounter(
                patient_id=int(row["patient_id"]),
                encounter_type=row.get("encounter_type", ""),
                provider=row.get("provider", ""),
                summary=row.get("summary", ""),
                allergies=row.get("allergies", ""),
                medications=row.get("medications", ""),
                occurred_at=row.get("occurred_at", ""),
            ))
    return out


def encounter_text(patient: PatientRow, enc: Encounter) -> str:
    """Render an encounter as retrievable prose.

    The allergy and medication lines are spelled out even when empty. "Allergies:
    none recorded" is a materially different retrieval target from a chart that
    simply has no allergy line — and the difference between those two is the
    entire Week-2 finding.
    """
    allergies = enc.allergies.strip() or "none recorded"
    meds = enc.medications.strip() or "none recorded"
    return (
        f"Patient {patient.name} (chart {patient.id}). "
        f"{enc.encounter_type.replace('_', ' ')} on {enc.occurred_at[:10]} "
        f"with {enc.provider}. {enc.summary} "
        f"Allergies: {allergies}. Medications: {meds}."
    )


def build_record_chunks(
    patients: Optional[list[PatientRow]] = None,
    encounters: Optional[list[Encounter]] = None,
) -> list[IndexChunk]:
    """Patient chart text → chunks bound to a patient_id."""
    patients = patients if patients is not None else load_patients()
    encounters = encounters if encounters is not None else load_encounters()
    by_id = {p.id: p for p in patients}

    chunks: list[IndexChunk] = []
    for i, enc in enumerate(encounters):
        patient = by_id.get(enc.patient_id)
        if patient is None:
            continue
        raw = encounter_text(patient, enc)
        # Strip direct identifiers retrieval does not need. Clinical content and
        # the encounter date stay — see the module docstring.
        scrubbed = deidentify.scrub_record(raw).text
        doc_id = f"chart-{enc.patient_id}-{i}"
        title = f"{patient.name} — {enc.encounter_type} {enc.occurred_at[:10]}"
        for chunk in chunk_text(scrubbed, settings.chunk_tokens, settings.chunk_overlap_tokens):
            chunks.append(IndexChunk(
                id=f"{doc_id}::{chunk.index}",
                text=chunk.text,
                doc_id=doc_id,
                doc_title=title,
                chunk_index=chunk.index,
                kind=KIND_RECORD,
                source="db/seed/encounters.csv",
                patient_id=enc.patient_id,
            ))
    return chunks


# --------------------------------------------------------------------------- #
# clinic knowledge — the non-PHI half of the corpus
# --------------------------------------------------------------------------- #
KNOWLEDGE_DOCS = [
    ("kb-fasting", "Pre-visit fasting instructions", (
        "Patients scheduled for a blood draw must not eat or drink anything "
        "except water for eight hours before the appointment. Water is "
        "encouraged. Patients taking daily medication should continue it with "
        "water unless their clinician has said otherwise. If a patient has "
        "eaten, the draw is rescheduled rather than run, because a non-fasting "
        "sample invalidates the lipid and glucose panels."
    )),
    ("kb-arrival", "Arrival and check-in procedure", (
        "Patients should arrive fifteen minutes before the appointment time and "
        "bring their insurance card and a photo ID. Front desk verifies "
        "demographics at every visit, including for returning patients. "
        "Effective 2026-01-01, cancellations must be made at least 24 hours "
        "ahead. Call 555-0100 to reschedule."
    )),
    ("kb-roi", "Release of information requests", (
        "A release of information request requires a signed authorization from "
        "the patient before any record leaves the clinic. Staff must confirm "
        "the authorization is on file, covers the date range requested, and has "
        "not expired. Requests from attorneys and insurers follow the same rule "
        "as requests from patients. Every disclosure is logged."
    )),
    ("kb-eligibility", "Insurance eligibility verification", (
        "Front desk verifies coverage before the visit using the payer "
        "eligibility check. If the payer does not respond, staff should proceed "
        "with registration and mark coverage as unverified rather than turning "
        "the patient away. Coverage status shown as stale means the payer was "
        "unreachable and the value is the last one successfully retrieved."
    )),
    ("kb-allergy", "Allergy documentation standard", (
        "Allergies are recorded at every encounter, not only at first "
        "registration. An empty allergy field means no allergy was recorded at "
        "that encounter. It does not mean the patient has no allergies. Staff "
        "must confirm verbally with the patient at each visit before "
        "prescribing."
    )),
]


def build_knowledge_chunks() -> list[IndexChunk]:
    chunks: list[IndexChunk] = []
    for doc_id, title, body in KNOWLEDGE_DOCS:
        scrubbed = deidentify.scrub_document(body).text
        for chunk in chunk_text(scrubbed, settings.chunk_tokens, settings.chunk_overlap_tokens):
            chunks.append(IndexChunk(
                id=f"{doc_id}::{chunk.index}",
                text=chunk.text,
                doc_id=doc_id,
                doc_title=title,
                chunk_index=chunk.index,
                kind=KIND_KNOWLEDGE,
                source="clinic policy",
                added_by="seed",
            ))
    return chunks


def enforce_cap(chunks: list[IndexChunk], existing: int = 0) -> list[IndexChunk]:
    cap = settings.corpus_max_chunks
    if existing + len(chunks) > cap:
        raise CorpusCapExceeded(
            f"ingest of {len(chunks)} chunks would take the corpus to "
            f"{existing + len(chunks)}, above the configured cap of {cap}. "
            f"Week 2 is a quota-risk week: raise RAG_CORPUS_MAX_CHUNKS "
            f"deliberately, do not raise it by accident."
        )
    return chunks
