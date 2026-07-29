"""Production Bedrock client — the Week-1 deliverable.

Replaces the contractor's raw passthrough (removed at git ``d0905a1``), which had
no timeout, no retry policy, no token budget, no cost guard, and no output
validation, and which returned raw model text. A saved transcript shows it
inventing a medication the patient was not taking.

Everything here is one of those gaps closed. Nothing here is a feature.

Design notes worth reading before changing anything
---------------------------------------------------
**One model path.** All inference goes through ``ChatBedrockConverse``
(``langchain-aws``, Bedrock Converse API). One auth path, one retry envelope, one
place to change a timeout. Per ADR 0004.

**We own retries, botocore does not.** The boto client is built with
``retries={"max_attempts": 0}``. Two retry layers compound multiplicatively and
make the wall-clock budget unanalysable.

**The deadline is the real bound.** Socket timeouts bound a *hop*. Only the
deadline bounds a *request*: read_timeout 20s x 3 retries + backoff is a
70-second page load for someone who was promised a page.

**Injectable clock and RNG.** ``sleep``/``monotonic``/``rng`` are constructor
arguments so the retry and backoff behaviour is asserted exactly, with a fixed
seed, instead of statistically. A flaky test of a resilience control is worse than
no test, because it trains people to re-run CI.

**Stub mode is the default.** ``USE_STUB_BEDROCK=true`` shapes the call
identically, spends nothing, and needs no credentials.
"""
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from config import settings

# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class BudgetError(Exception):
    """Request refused before any spend: over the token cap or the cost ceiling."""


class ModelUnavailable(Exception):
    """Bedrock could not be reached within the deadline / retry budget."""


class GuardrailBlocked(Exception):
    """A Bedrock guardrail (Tier 1) blocked the content."""


# --------------------------------------------------------------------------- #
# retry classification
#
# Retrying a request that is *wrong* turns a fast, clear config error into a slow,
# ambiguous outage. The bad-model-id case (ResourceNotFoundException) is the one
# that bites in a demo, so it fails in under a second.
# --------------------------------------------------------------------------- #
RETRYABLE = frozenset({
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelTimeoutException",
    "ReadTimeoutError",
    "ConnectTimeoutError",
    "EndpointConnectionError",
})

NON_RETRYABLE = frozenset({
    "ValidationException",          # the request is wrong; resending it is wrong again
    "AccessDeniedException",        # credentials/permissions; retrying masks the cause
    "UnrecognizedClientException",
    "ResourceNotFoundException",    # usually a bare model id instead of a profile id
    "ServiceQuotaExceededException",
    "ModelNotReadyException",
})


def is_retryable(err: BaseException) -> bool:
    name = type(err).__name__
    if name in RETRYABLE:
        return True
    if name in NON_RETRYABLE:
        return False
    # botocore ClientError carries the real code in the response envelope.
    code = ""
    response = getattr(err, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code", "")
    if code in RETRYABLE:
        return True
    if code in NON_RETRYABLE:
        return False
    return False  # unknown failures are not retried: fail fast, surface loudly


# --------------------------------------------------------------------------- #
# result
# --------------------------------------------------------------------------- #
@dataclass
class ModelResult:
    text: str
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_usd: float = 0.0
    stubbed: bool = False
    attempts: int = 1
    latency_ms: int = 0
    guardrail_action: str = "NONE"
    parsed: Optional[dict] = field(default=None)


def estimate_tokens(text: str) -> int:
    """~4 chars/token. A pre-call guard only — real usage is read back after."""
    return max(1, len(text or "") // 4)


def _cost(in_tok: int, out_tok: int) -> float:
    return round(
        (in_tok / 1000) * settings.price_in_per_1k
        + (out_tok / 1000) * settings.price_out_per_1k,
        6,
    )


# --------------------------------------------------------------------------- #
# structured output
#
# The model is asked for a single JSON object. Three fallbacks, because a model
# that wraps JSON in prose is a normal Tuesday and must not become a 500.
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_structured(payload: str, key: str) -> tuple[Optional[str], Optional[dict]]:
    """Extract ``payload[key]`` as a string. Returns (value, whole_object)."""
    candidates: list[str] = []
    text = (payload or "").strip()
    if text:
        candidates.append(text)
    fence = _FENCE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get(key), str):
            return obj[key].strip(), obj
    return None, None


# --------------------------------------------------------------------------- #
# the client
# --------------------------------------------------------------------------- #
class ModelClient:
    def __init__(
        self,
        model_id: Optional[str] = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
        chat: Any = None,
        guardrail_client: Any = None,
    ):
        self.model_id = model_id or settings.summary_model_id
        self._sleep = sleep
        self._monotonic = monotonic
        self._rng = rng or random.Random()
        self._chat = chat                      # injected in tests; built lazily otherwise
        self._guardrail_client = guardrail_client

    # -- budget ------------------------------------------------------------ #
    def guard_budget(self, prompt_text: str) -> int:
        """Refuse over-budget requests BEFORE any call. Returns est. input tokens."""
        in_tok = estimate_tokens(prompt_text)
        if in_tok > settings.max_input_tokens:
            raise BudgetError(
                f"estimated input {in_tok} tokens exceeds cap "
                f"{settings.max_input_tokens}"
            )
        projected = _cost(in_tok, settings.max_output_tokens)
        if projected > settings.max_cost_per_request_usd:
            raise BudgetError(
                f"projected worst-case cost ${projected} exceeds ceiling "
                f"${settings.max_cost_per_request_usd}"
            )
        return in_tok

    # -- backoff ----------------------------------------------------------- #
    def backoff_delay(self, attempt: int) -> float:
        """Exponential with full-width jitter, capped.

        ``min(cap, base * 2**attempt) * U(0.5, 1.0)`` — jitter is multiplicative
        on the lower half so a thundering herd de-syncs without any attempt
        collapsing to zero delay.
        """
        raw = min(settings.backoff_cap_s, settings.backoff_base_s * (2 ** attempt))
        return raw * (0.5 + self._rng.random() / 2)

    # -- transport --------------------------------------------------------- #
    def _build_chat(self):
        if self._chat is not None:
            return self._chat
        from botocore.config import Config
        import boto3
        from langchain_aws import ChatBedrockConverse

        client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            config=Config(
                connect_timeout=settings.connect_timeout_s,
                read_timeout=settings.read_timeout_s,
                # We own retries. Two retry layers compound multiplicatively and
                # make the deadline unanalysable.
                retries={"max_attempts": 0},
            ),
        )
        self._chat = ChatBedrockConverse(
            client=client,
            model=self.model_id,
            max_tokens=settings.max_output_tokens,
        )
        return self._chat

    # -- Tier 1 guardrails -------------------------------------------------- #
    def _bedrock_guardrail(self):
        if self._guardrail_client is not None:
            return self._guardrail_client
        import boto3
        self._guardrail_client = boto3.client(
            "bedrock-runtime", region_name=settings.aws_region
        )
        return self._guardrail_client

    def apply_guardrail_input(self, text: str) -> str:
        """INPUT side: content filters, denied topics, word lists, PII detectors.

        NOT contextual grounding — that needs a model response to evaluate and is
        therefore output-side only (ADR 0004 §4).
        """
        if not (settings.guardrail_enabled and settings.guardrail_id):
            return "NONE"
        resp = self._bedrock_guardrail().apply_guardrail(
            guardrailIdentifier=settings.guardrail_id,
            guardrailVersion=settings.guardrail_version,
            source="INPUT",
            content=[{"text": {"text": text}}],
        )
        return resp.get("action", "NONE")

    def apply_guardrail_output(self, response: str, grounding_source: str, query: str) -> str:
        """OUTPUT side: the above PLUS contextual grounding.

        Contextual grounding requires all three components — grounding source,
        query, and the content to guard.
        """
        if not (settings.guardrail_enabled and settings.guardrail_id):
            return "NONE"
        resp = self._bedrock_guardrail().apply_guardrail(
            guardrailIdentifier=settings.guardrail_id,
            guardrailVersion=settings.guardrail_version,
            source="OUTPUT",
            content=[
                {"text": {"text": grounding_source, "qualifiers": ["grounding_source"]}},
                {"text": {"text": query, "qualifiers": ["query"]}},
                {"text": {"text": response}},
            ],
        )
        return resp.get("action", "NONE")

    # -- invoke ------------------------------------------------------------- #
    def invoke(
        self,
        system: str,
        user: str,
        *,
        structured_key: Optional[str] = None,
        stub_text: Optional[str] = None,
        grounding_source: Optional[str] = None,
    ) -> ModelResult:
        """One bounded, budgeted, retried, validated model call."""
        in_tok = self.guard_budget(f"{system}\n{user}")
        started = self._monotonic()

        guardrail_action = self.apply_guardrail_input(user)
        if guardrail_action == "GUARDRAIL_INTERVENED":
            raise GuardrailBlocked("input blocked by Bedrock guardrail")

        if settings.use_stub:
            text = stub_text if stub_text is not None else json.dumps(
                {structured_key or "text": "[stubbed] no Bedrock call was made."}
            )
            return self._finish(text, structured_key, in_tok, started, attempts=1, stubbed=True)

        chat = self._build_chat()
        messages = [("system", system), ("human", user)]
        last_err: Optional[BaseException] = None

        for attempt in range(settings.max_retries + 1):
            if self._monotonic() - started > settings.deadline_s:
                break
            try:
                msg = chat.invoke(messages)
            except BaseException as e:  # noqa: BLE001 — classified immediately below
                last_err = e
                if not is_retryable(e) or attempt >= settings.max_retries:
                    break
                delay = self.backoff_delay(attempt)
                # Never sleep past the deadline: a backoff that overruns the
                # budget is just a slower timeout.
                remaining = settings.deadline_s - (self._monotonic() - started)
                if delay >= remaining:
                    break
                self._sleep(delay)
                continue

            text = _message_text(msg)
            usage = getattr(msg, "usage_metadata", None) or {}
            result = self._finish(
                text, structured_key, in_tok, started,
                attempts=attempt + 1, stubbed=False,
                in_used=int(usage.get("input_tokens", in_tok) or in_tok),
                out_used=int(usage.get("output_tokens", 0) or estimate_tokens(text)),
            )
            if grounding_source is not None:
                action = self.apply_guardrail_output(result.text, grounding_source, user)
                result.guardrail_action = action
                if action == "GUARDRAIL_INTERVENED":
                    raise GuardrailBlocked("output blocked by Bedrock guardrail")
            return result

        raise ModelUnavailable(
            f"bedrock invoke failed after {settings.max_retries + 1} attempt(s) "
            f"within {settings.deadline_s}s: {type(last_err).__name__ if last_err else 'deadline'}"
        )

    # -- helpers ------------------------------------------------------------ #
    def _finish(
        self, text, structured_key, in_tok, started, *,
        attempts, stubbed, in_used=None, out_used=None,
    ) -> ModelResult:
        in_used = in_tok if in_used is None else in_used
        out_used = estimate_tokens(text) if out_used is None else out_used
        parsed = None
        value = text
        if structured_key:
            value, parsed = parse_structured(text, structured_key)
            if value is None:
                # Graceful degradation: prose where JSON was asked for is a bad
                # response, not a server error. The grounding check downstream
                # still has to pass, so an unparseable answer cannot sneak
                # through as if it were valid.
                value = (text or "").strip()
        return ModelResult(
            text=value,
            model_id=self.model_id,
            input_tokens=in_used,
            output_tokens=out_used,
            est_cost_usd=_cost(in_used, out_used),
            stubbed=stubbed,
            attempts=attempts,
            latency_ms=int((self._monotonic() - started) * 1000),
            parsed=parsed,
        )


def _message_text(msg: Any) -> str:
    """Converse returns a content-block list; older shapes return a string."""
    content = getattr(msg, "content", msg)
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        ).strip()
    return str(content or "").strip()
