"""Bedrock data-retention preflight — requirement RVB-X-09, ADR 0004 §1a.

Why this file exists
--------------------
It is not enough to say "Bedrock is HIPAA-eligible and model providers cannot read
prompts." Bedrock exposes an explicit retention *mode*, resolved as the first
non-inherit value of (project -> account -> model default):

    none                 zero data retention; nothing written to durable storage
                         by AWS, nothing shared with the model provider
    default              the model's own policy; AWS MAY retain for safety and
                         abuse prevention; the provider does not receive it
    provider_data_share  AWS retains AND shares inference data with the model
                         provider; required to access certain models
    inherit              defer to a broader scope (the default for new accounts)

AWS documents that for models requiring ``provider_data_share`` — currently Claude
Mythos 5 and Claude Fable 5 — "user prompts and completions are shared with
Anthropic and retained for up to 30 days for trust and safety purposes."

For a PHI workload that is **debt D13**: PHI to an LLM vendor without a BAA
covering that disclosure. Model choice is therefore a compliance control, not a
capability preference, and it is enforced here in code rather than trusted to a
runbook.

Bedrock does fail closed — a request under mode ``none`` to a model requiring
retention is blocked, and such a model reports ``status: "unavailable"``. We treat
that as a backstop, not as the control: failing at the first PHI request is a
worse outcome than failing at startup, because by then someone is watching a demo.

Offline behaviour
-----------------
In stub mode there is no AWS to ask and nothing leaves the process, so the
preflight passes trivially and records ``checked=False``. The *fail-closed logic*
is still exercised by tests via injected probes.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import settings

# Modes that are acceptable for a PHI-adjacent path. Only one qualifies.
_SAFE_MODES = frozenset({"none"})


class RetentionPolicyError(RuntimeError):
    """Raised when the effective retention posture is unsafe for PHI."""


@dataclass
class RetentionStatus:
    ok: bool
    checked: bool
    effective_mode: Optional[str] = None
    allowed_modes: list[str] = field(default_factory=list)
    model_id: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "effective_mode": self.effective_mode,
            "allowed_modes": self.allowed_modes,
            "model_id": self.model_id,
            "reason": self.reason,
        }


def _default_probe(model_id: str) -> dict:
    """Ask Bedrock for the model's effective and allowed retention modes.

    Kept behind an injectable seam so every test runs offline. The real call is
    exercised by the live smoke test L0, which spends nothing and gates the rest
    of the live tier.
    """
    import boto3

    client = boto3.client("bedrock", region_name=settings.aws_region)
    resp = client.get_foundation_model(modelIdentifier=model_id)
    details = resp.get("modelDetails", {})
    retention = details.get("dataRetention", {}) or {}
    return {
        "effective_mode": retention.get("mode"),
        "allowed_modes": list(retention.get("allowedModes", []) or []),
    }


def check(
    model_id: Optional[str] = None,
    probe: Optional[Callable[[str], dict]] = None,
) -> RetentionStatus:
    """Return the retention posture for ``model_id``. Never raises."""
    model_id = model_id or settings.summary_model_id

    if settings.retention_preflight_skip:
        # Explicit operator override. Only legitimate in a sandbox with no PHI;
        # the audit event records that the check was skipped, so it cannot be a
        # silent bypass.
        return RetentionStatus(
            ok=True, checked=False, model_id=model_id,
            reason="preflight explicitly skipped (BEDROCK_RETENTION_PREFLIGHT_SKIP)",
        )

    if settings.use_stub and probe is None:
        # Nothing leaves the process in stub mode, so there is nothing to retain.
        return RetentionStatus(
            ok=True, checked=False, model_id=model_id,
            reason="stub mode — no inference leaves the process",
        )

    if not settings.require_zero_retention:
        return RetentionStatus(
            ok=True, checked=False, model_id=model_id,
            reason="zero-retention requirement disabled by configuration",
        )

    probe = probe or _default_probe
    try:
        result = probe(model_id)
    except Exception as e:  # noqa: BLE001
        # Fail CLOSED. An unanswerable question about where PHI goes is a "no".
        return RetentionStatus(
            ok=False, checked=True, model_id=model_id,
            reason=f"retention probe failed ({type(e).__name__}); failing closed",
        )

    effective = result.get("effective_mode")
    allowed = list(result.get("allowed_modes") or [])

    if effective not in _SAFE_MODES:
        return RetentionStatus(
            ok=False, checked=True, effective_mode=effective, allowed_modes=allowed,
            model_id=model_id,
            reason=(
                f"effective data-retention mode is {effective!r}; PHI workloads "
                f"require 'none' (ADR 0004 §1a, RVB-X-09)"
            ),
        )

    if not _SAFE_MODES.intersection(allowed):
        return RetentionStatus(
            ok=False, checked=True, effective_mode=effective, allowed_modes=allowed,
            model_id=model_id,
            reason=(
                f"model {model_id!r} does not permit zero data retention "
                f"(allowed_modes={allowed}); it would share prompts and completions "
                f"with the model provider. Disqualified regardless of capability."
            ),
        )

    return RetentionStatus(
        ok=True, checked=True, effective_mode=effective, allowed_modes=allowed,
        model_id=model_id, reason="zero data retention confirmed",
    )


def enforce(
    model_id: Optional[str] = None,
    probe: Optional[Callable[[str], dict]] = None,
) -> RetentionStatus:
    """Like :func:`check`, but raises ``RetentionPolicyError`` when unsafe."""
    status = check(model_id=model_id, probe=probe)
    if not status.ok:
        raise RetentionPolicyError(status.reason)
    return status
