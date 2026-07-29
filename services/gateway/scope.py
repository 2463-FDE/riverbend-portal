"""Authorization scope — turning a session into "which patients may you see".

ADR 0011. This is the module the IDOR was missing.

`require_session` answers *"is someone logged in?"* It has never been able to
answer *"is this the right someone?"*, because the session carried only a
username and a role. The HAR in `docs/handover/portal.har` shows the consequence:
one logged-in patient fetches `/api/patients/1042/records` → 200, then
`/api/patients/1043/records` → 200. Sequential integer ids, walkable by anyone
with a valid session.

The fix is not a comparison bolted onto the proxy. It is that a session now
resolves to an `AuthorizedScope`, and every patient-addressed route resolves the
scope BEFORE it proxies anything.

Two principal kinds
-------------------
**Patient** — `patient_id IS NOT NULL`. Scope is their own chart, plus any chart
reachable by `SAME_AS`, so the Week-2 fragmentation does not become a Week-4
access denial. Maria Gonzalez logging in must see all three of her charts, not
one third of her record.

**Staff** — `patient_id IS NULL`. Scope is the patient explicitly in context.
**This is deliberately coarse and we say so.** With one `staff` role for
everyone, a real minimum-necessary determination is impossible — that is debt D7
and it is Week 9's work. What lands here is the mechanism and the patient-side
enforcement; the staff-side policy plugs into the same gate once roles exist.
Describing this as least-privilege would be exactly the self-assertion we
criticised in the README.

Denials are 404, not 403
------------------------
A `403` on a real patient id and a `404` on a nonexistent one is an enumeration
oracle: it confirms which ids exist, which is most of what the walk in the HAR
was after. Both return the same response.
"""
from dataclasses import dataclass, field
from typing import Optional, Sequence

from fastapi import HTTPException

PRINCIPAL_PATIENT = "patient"
PRINCIPAL_STAFF = "staff"


@dataclass(frozen=True)
class AuthorizedScope:
    """What this session is allowed to see. Derived, never client-supplied."""

    principal: str
    username: str
    patient_ids: frozenset = field(default_factory=frozenset)
    # True when the scope is "whichever patient is in context", i.e. staff.
    # W9 replaces this with a real treatment-relationship check.
    open_to_context: bool = False

    def permits(self, patient_id: int) -> bool:
        if self.open_to_context:
            return True
        return int(patient_id) in self.patient_ids

    def as_dict(self) -> dict:
        return {
            "principal": self.principal,
            "username": self.username,
            "patient_ids": sorted(self.patient_ids),
            "open_to_context": self.open_to_context,
        }


def resolve_scope(
    session: dict,
    same_as_lookup: Optional[callable] = None,
) -> AuthorizedScope:
    """Session → scope. Pure, deterministic, no model, no network by default.

    `same_as_lookup(patient_id) -> Sequence[int]` supplies the identity cluster
    so a fragmented patient sees their whole record. Injected rather than
    imported so this module stays trivially testable and has no opinion about
    where identity resolution lives.
    """
    session = session or {}
    username = session.get("username", "")
    raw_patient_id = session.get("patient_id")

    if raw_patient_id not in (None, "", "None"):
        try:
            patient_id = int(raw_patient_id)
        except (TypeError, ValueError):
            # A malformed session is not a licence to see everything.
            return AuthorizedScope(principal=PRINCIPAL_PATIENT, username=username)

        ids = {patient_id}
        if same_as_lookup is not None:
            try:
                ids |= {int(p) for p in (same_as_lookup(patient_id) or [])}
            except Exception:  # noqa: BLE001 — identity resolution must never widen
                pass           # a failed lookup narrows to self, never broadens
        return AuthorizedScope(
            principal=PRINCIPAL_PATIENT, username=username, patient_ids=frozenset(ids)
        )

    return AuthorizedScope(
        principal=PRINCIPAL_STAFF, username=username, open_to_context=True
    )


def require_patient_access(scope: AuthorizedScope, patient_id: int) -> None:
    """Raise 404 unless `scope` permits `patient_id`.

    404 and not 403 — see the module docstring. The message is identical to a
    genuine miss so the two are indistinguishable from outside.
    """
    if not scope.permits(patient_id):
        raise HTTPException(status_code=404, detail="patient not found")


def filter_patient_ids(scope: AuthorizedScope, ids: Sequence[int]) -> list[int]:
    """Narrow a requested id set to what the scope permits. Never widens."""
    if scope.open_to_context:
        return [int(i) for i in ids]
    return [int(i) for i in ids if int(i) in scope.patient_ids]
