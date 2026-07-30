"""The HITL queue: runs paused at the sensitivity gate, waiting on a person.

`adr/0015` rule 4 says a human gate must render as a **decision**, not a delay.
That requires three things this module provides, each of which was a finding:

**A queue that survives a restart** (`RVB-AG-13`). Under `InMemorySaver` a paused
run lives in one process's memory, so a queue over it empties on deploy. That is a
session, not a control, and shipping it as a control would be the overstatement
this engagement keeps correcting. The registry is therefore external to the
checkpointer.

**An opaque approval id** (codex F9, UI-D20). Resume previously took a
client-supplied `thread_id` and forwarded it verbatim — the gateway checked the
path `patient_id` and then trusted the client for *which run* to resume. Same
shape as the F1 IDOR: right about one input, silent about another. Worse, it made
a checkpointer implementation detail into part of the API. The client now receives
a token it cannot construct.

**An approver who is not the subject** (codex F8, UI-D18). The old resume route
was guarded by `require_patient_access`, so Maria could approve the sensitivity
gate on Maria's record. That is not human-in-the-loop; it is a rubber stamp that
the audit log records as an approval.

The distinction the single word "approve" was hiding: *may this patient see their
own record* (yes, 164.524) is a different question from *should this assembled,
cross-chart, sensitivity-flagged view be released* — which is a clinical and
compliance judgement about material that may include another person's information.
The gate asks the second. A subject cannot answer it about themselves.
"""
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

import crypto_box

# Long enough that a paused run survives a shift change, short enough that an
# abandoned one does not hold assembled PHI indefinitely.
TTL_SECONDS = 24 * 60 * 60
KEY_PREFIX = "view:approval:"
INDEX_KEY = "view:approvals:open"


class ApprovalsUnavailable(RuntimeError):
    """The registry is unreachable. Fails CLOSED — no queue, no approvals."""


class ApprovalNotFound(LookupError):
    """Unknown, expired, or already decided. 404."""


class ApprovalForbidden(PermissionError):
    """The caller may not decide this one. 403."""


@dataclass
class Approval:
    approval_id: str
    patient_id: int
    thread_id: str
    requested_by: str
    requested_principal: str
    authorized_ids: list = field(default_factory=list)
    reason: str = ""
    created_at: float = 0.0

    @property
    def chart_span(self) -> int:
        return len(self.authorized_ids or [])

    def row(self) -> dict:
        """What the queue renders.

        `thread_id` is deliberately absent: it is a graph implementation detail
        and, since UI-D20, not part of the client contract.

        `describe` exists because "Approve run view-a3f9" is a button that gets
        clicked and a sentence describing a release is a decision that gets read
        (adr/0015 rule 4).
        """
        return {
            "approval_id": self.approval_id,
            "patient_id": self.patient_id,
            "requested_by": self.requested_by,
            "requested_principal": self.requested_principal,
            "chart_span": self.chart_span,
            "authorized_ids": sorted(self.authorized_ids or []),
            "reason": self.reason,
            "created_at": self.created_at,
            "describe": self.describe(),
        }

    def describe(self) -> str:
        span = self.chart_span
        charts = (
            f"assembled across {span} charts" if span > 1 else "for a single chart"
        )
        who = (
            f"requested by {self.requested_by}"
            if self.requested_by
            else "requested by an unidentified session"
        )
        return (
            f"Release the record view for chart {self.patient_id}, {charts}, {who}."
        )


_client = None


def _redis():
    global _client
    if _client is None:
        try:
            import redis as redis_lib
            from config import settings

            _client = redis_lib.from_url(settings.redis_url, decode_responses=True)
            _client.ping()
        except Exception as e:  # noqa: BLE001
            _client = None
            raise ApprovalsUnavailable(f"approvals registry unavailable: {type(e).__name__}")
    return _client


def reset_client() -> None:
    global _client
    _client = None


def register(
    *,
    patient_id: int,
    thread_id: str,
    requested_by: str,
    requested_principal: str,
    authorized_ids: list,
    reason: str,
) -> Approval:
    """Record a paused run. Idempotent per thread, so a retried view does not
    enqueue the same decision twice — a queue that grows on refresh is a queue
    people stop reading."""
    r = _redis()

    for existing in list_open():
        if existing.thread_id == thread_id:
            return existing

    approval = Approval(
        approval_id=uuid.uuid4().hex,
        patient_id=int(patient_id),
        thread_id=thread_id,
        requested_by=requested_by or "",
        requested_principal=requested_principal or "",
        authorized_ids=[int(i) for i in (authorized_ids or [])],
        reason=reason or "disclosure_shaped_assembly",
        created_at=time.time(),
    )
    r.setex(f"{KEY_PREFIX}{approval.approval_id}", TTL_SECONDS,
            crypto_box.seal(asdict(approval)))
    r.sadd(INDEX_KEY, approval.approval_id)
    return approval


def _load(approval_id: str) -> Optional[Approval]:
    blob = _redis().get(f"{KEY_PREFIX}{approval_id}")
    if blob is None:
        return None
    return Approval(**crypto_box.unseal(blob))


def list_open() -> list[Approval]:
    r = _redis()
    out: list[Approval] = []
    for approval_id in sorted(r.smembers(INDEX_KEY) or []):
        approval = _load(approval_id)
        if approval is None:
            # The record expired but the index entry survived. Reap it rather
            # than rendering a row that 404s when clicked.
            r.srem(INDEX_KEY, approval_id)
            continue
        out.append(approval)
    return sorted(out, key=lambda a: a.created_at)


def get(approval_id: str) -> Approval:
    approval = _load(approval_id)
    if approval is None:
        raise ApprovalNotFound("that approval is no longer open")
    return approval


def claim(approval_id: str, *, approver: str, approver_principal: str,
          approver_patient_id: Optional[int]) -> Approval:
    """Take an approval for a decision, enforcing who may decide it.

    Three checks, in the order that leaks least:

      1. exists — 404
      2. the approver is staff — 403
      3. the approver is not the SUBJECT — 403, even for staff, because a staff
         member holding a patient account for their own chart is exactly the case
         a role check alone would miss

    The delete happens only after all three pass, so a rejected caller cannot
    destroy a pending decision.
    """
    approval = get(approval_id)

    if approver_principal != "staff":
        raise ApprovalForbidden("releasing an assembled record view is a staff decision")

    if approver_patient_id is not None and int(approver_patient_id) == approval.patient_id:
        raise ApprovalForbidden(
            "you cannot approve the release of your own record view")

    # Nor your own request. The subject check above stops a patient rubber-stamping
    # their own disclosure; this stops the requester doing the same thing one step
    # removed. Both are "the person with an interest in the answer is giving it",
    # and a queue where you can clear your own items is a queue with no second
    # pair of eyes in it.
    if approver and approval.requested_by and approver == approval.requested_by:
        raise ApprovalForbidden(
            "this view was requested by you — someone else has to release it")

    r = _redis()
    key = f"{KEY_PREFIX}{approval_id}"
    try:
        blob = r.getdel(key)
    except AttributeError:  # redis-py < 4 / older server
        pipe = r.pipeline()
        pipe.get(key)
        pipe.delete(key)
        blob = pipe.execute()[0]

    if blob is None:
        # Lost the race with a concurrent decision.
        raise ApprovalNotFound("that approval has already been decided")

    r.srem(INDEX_KEY, approval_id)
    return approval
