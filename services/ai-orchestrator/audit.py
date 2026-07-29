"""Structured, body-free audit events for every model call — the inverse of D1.

The system's existing habit is to log the full request body at INFO. This module
makes the opposite habit mechanical: an audit event has a FIXED key set, and
``emit`` refuses any key outside it.

That refusal is the control. A comment saying "don't log bodies" survives until
the first person who needs to debug something at 6pm. A ``ValueError`` on an
unexpected key survives indefinitely, and the test that asserts the key set makes
the intent explicit to the next reader.

What is deliberately absent: prompt, response, instruction text, summary text,
patient identifiers, and any other user-supplied string. ``phi_redacted`` records
the *kinds* of identifier that were redacted, never the values — logging what you
redacted defeats the redaction.
"""
from typing import Any

# The complete, closed set. Adding a key here is a decision that should show up
# in a diff and be argued for, which is the point.
AUDIT_FIELDS = frozenset({
    "event",
    "request_id",
    "model_id",
    "outcome",
    "stubbed",
    "grounded",
    "grounding_score",
    "input_tokens",
    "output_tokens",
    "est_cost_usd",
    "latency_ms",
    "attempts",
    "phi_redacted",
    "guardrail_action",
    "retention_checked",
    "reason",
})


class AuditFieldError(ValueError):
    """Raised when an audit event carries a field outside the closed set."""


def build(**fields: Any) -> dict:
    """Validate and normalise an audit event. Raises on an unexpected key."""
    unknown = set(fields) - AUDIT_FIELDS
    if unknown:
        raise AuditFieldError(
            f"audit event carries non-allowlisted field(s): {sorted(unknown)}. "
            f"Audit events never carry request or response bodies — if you need "
            f"this value, ask whether it can contain PHI before adding it to "
            f"AUDIT_FIELDS."
        )
    event = {k: v for k, v in fields.items() if v is not None}
    event.setdefault("event", "model_call")
    return event


def render(event: dict) -> str:
    """Render as stable ``key=value`` pairs, ordered for greppability."""
    order = [f for f in (
        "event", "request_id", "outcome", "model_id", "stubbed", "grounded",
        "grounding_score", "input_tokens", "output_tokens", "est_cost_usd",
        "latency_ms", "attempts", "guardrail_action", "retention_checked",
        "phi_redacted", "reason",
    ) if f in event]
    parts = []
    for key in order:
        value = event[key]
        if isinstance(value, (list, tuple, set)):
            value = "|".join(str(v) for v in sorted(value)) or "-"
        parts.append(f"{key}={value}")
    return " ".join(parts)


def emit(logger, **fields: Any) -> dict:
    """Build, validate, and log one audit event. Returns the event."""
    event = build(**fields)
    logger.info(render(event))
    return event
