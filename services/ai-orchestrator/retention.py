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
import os
import re
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


# Region-scoped inference-profile prefixes. `us.anthropic.claude-haiku-4-5-...`
# is a PROFILE id, not a model id, and the retention API knows only the latter.
_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "us-gov.")

# The trailing release-date + version that foundation-model ids carry and the
# retention API's ids do not: `-20251001-v1:0`.
_VERSION_SUFFIX = re.compile(r"-\d{8}-v\d+:\d+$")


def retention_model_id(model_id: str) -> str:
    """Map an invocation id onto the id the retention API uses.

        us.anthropic.claude-haiku-4-5-20251001-v1:0  ->  anthropic.claude-haiku-4-5

    Two different id namespaces meet here, which is exactly the sort of seam this
    codebase keeps finding defects in, so the mapping is a named function with
    its own test rather than an inline slice.
    """
    out = model_id
    for prefix in _PROFILE_PREFIXES:
        if out.startswith(prefix):
            out = out[len(prefix):]
            break
    return _VERSION_SUFFIX.sub("", out)


def _default_probe(model_id: str) -> dict:
    """Ask Bedrock for the model's effective and allowed retention modes.

    **Corrected after the first live run.** This previously called
    ``bedrock:GetFoundationModel`` and read ``modelDetails.dataRetention`` — a
    field that does not exist. `FoundationModelDetails` contains exactly
    customizationsSupported, inferenceTypesSupported, inputModalities, modelArn,
    modelId, modelLifecycle, modelName, outputModalities, providerName and
    responseStreamingSupported, and nothing about retention:

        https://docs.aws.amazon.com/bedrock/latest/APIReference/API_GetFoundationModel.html

    So the probe raised ``ValidationException`` (the profile id is also rejected
    there) and the preflight failed closed on every call. Failing closed was the
    right behaviour and it masked the cause: the control could never pass, so the
    summariser could never be enabled with a real key.

    The retention posture lives on a different service — ``bedrock-mantle`` —
    which returns exactly the shape the module docstring describes:

        GET https://bedrock-mantle.{region}.api.aws/v1/models/{model}
        { "data_retention": { "mode": ..., "source": ..., "allowed_modes": [...] } }

        https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html

    Kept behind an injectable seam so every test runs offline. The real call is
    exercised by the live smoke test L0, which spends nothing and gates the rest
    of the live tier -- and which is how this defect was found at all.
    """
    import json
    import urllib.error
    import urllib.request

    token = os.getenv("AWS_BEARER_TOKEN_BEDROCK", "")
    if not token:
        # SigV4 against bedrock-mantle is not implemented here. Say so rather
        # than returning an empty posture that reads like a clean answer.
        raise RuntimeError(
            "retention probe needs AWS_BEARER_TOKEN_BEDROCK; SigV4 is not "
            "supported on this path")

    url = (f"https://bedrock-mantle.{settings.aws_region}.api.aws"
           f"/v1/models/{retention_model_id(model_id)}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

    with urllib.request.urlopen(req, timeout=settings.read_timeout_s) as resp:
        body = json.loads(resp.read().decode())

    if isinstance(body.get("error"), dict):
        raise RuntimeError(body["error"].get("message", "retention probe rejected"))

    retention = body.get("data_retention") or {}
    return {
        "effective_mode": retention.get("mode"),
        "allowed_modes": list(retention.get("allowed_modes") or []),
        # `status: unavailable` means the account's effective mode is not in the
        # model's allowed set -- Bedrock's own fail-closed, surfaced rather than
        # discovered at the first PHI request.
        "status": body.get("status"),
        "status_reason": body.get("status_reason"),
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
        #
        # The MESSAGE is included, not just the type. This previously reported
        # only `ValidationException`, which is true and useless: the actual text
        # was "The provided model identifier is invalid" and would have pointed
        # straight at the wrong-API bug instead of reading like a policy refusal.
        # Truncated because it is operator-facing, and it never contains prompt
        # text -- this call sends a model id and nothing else.
        detail = str(e).strip().replace("\n", " ")[:200] or type(e).__name__
        return RetentionStatus(
            ok=False, checked=True, model_id=model_id,
            reason=f"retention probe failed ({type(e).__name__}: {detail}); failing closed",
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
