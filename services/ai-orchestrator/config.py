"""ai-orchestrator configuration. Environment-driven, with compose-friendly defaults.

This service is the production Bedrock client for the AI intake-summary feature
(ADR 0005). Every knob that governs cost, latency, or a compliance control is here
rather than inline, so an operator can see the whole envelope in one file.

Two settings are load-bearing for compliance and are NOT ordinary tunables:

  * ``require_zero_retention`` — see retention.py and ADR 0004 §1a. Bedrock's
    effective data-retention mode must be ``none`` for a PHI workload. A model
    requiring ``provider_data_share`` shares prompts and completions with the
    model provider for up to 30 days, which recreates debt D13.

  * ``use_stub`` — default true. The stack runs, and the whole test suite passes,
    with no AWS credentials and zero spend.
"""
import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    service_name = "ai-orchestrator"
    port = _i("PORT", 8077)
    environment = os.getenv("ENVIRONMENT", "development")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    # --- Bedrock -------------------------------------------------------- #
    # Stub by default: no credentials needed, no tokens spent. Set
    # USE_STUB_BEDROCK=false to reach real Bedrock.
    use_stub = _b("USE_STUB_BEDROCK", True)
    aws_region = os.getenv("AWS_REGION", "us-east-1")

    # Model IDs are region-scoped INFERENCE PROFILE ids. A bare model id fails
    # on-demand invocation with a ValidationException at call time — i.e. in the
    # demo, not in CI — so it is env-driven and verified by the live smoke test.
    # One default, overridable per path (ADR 0004 §1, debate D9): we do not pay
    # for multi-model complexity until a measurement justifies it.
    _default_model = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0")
    summary_model_id = os.getenv("BEDROCK_SUMMARY_MODEL_ID", _default_model)
    rag_model_id = os.getenv("BEDROCK_RAG_MODEL_ID", _default_model)
    agent_model_id = os.getenv("BEDROCK_AGENT_MODEL_ID", _default_model)
    synthesis_model_id = os.getenv("BEDROCK_SYNTHESIS_MODEL_ID", _default_model)

    # --- resilience: timeouts + retry ----------------------------------- #
    # Socket timeouts bound a HOP. Only the deadline bounds a REQUEST: a 20s read
    # timeout with 3 retries plus backoff is a 70-second page load. See ADR 0005.
    connect_timeout_s = _f("BEDROCK_CONNECT_TIMEOUT_S", 3.0)
    read_timeout_s = _f("BEDROCK_READ_TIMEOUT_S", 20.0)
    deadline_s = _f("BEDROCK_DEADLINE_S", 30.0)
    max_retries = _i("BEDROCK_MAX_RETRIES", 3)
    backoff_base_s = _f("BEDROCK_BACKOFF_BASE_S", 0.25)
    backoff_cap_s = _f("BEDROCK_BACKOFF_CAP_S", 4.0)

    # --- token + cost guard ---------------------------------------------- #
    max_input_tokens = _i("BEDROCK_MAX_INPUT_TOKENS", 4000)
    max_output_tokens = _i("BEDROCK_MAX_OUTPUT_TOKENS", 600)
    # Order-of-magnitude prices per 1K tokens for the in-process guard and the
    # audit line. NOT billing truth — the ceiling exists to refuse absurd
    # requests before they spend, not to reconcile an invoice.
    price_in_per_1k = _f("BEDROCK_PRICE_IN_PER_1K", 0.0008)
    price_out_per_1k = _f("BEDROCK_PRICE_OUT_PER_1K", 0.004)
    max_cost_per_request_usd = _f("BEDROCK_MAX_COST_PER_REQUEST_USD", 0.05)

    # --- guardrails (Tier 0: offline, free, always on) -------------------- #
    min_source_chars = _i("SUMMARY_MIN_SOURCE_CHARS", 20)
    grounding_threshold = _f("SUMMARY_GROUNDING_THRESHOLD", 0.55)

    # --- guardrails (Tier 1: Bedrock-managed, key/BAA-gated) -------------- #
    # Same call sites as Tier 0; enabling is configuration, not code (ADR 0004
    # §4). INPUT side carries content/PII/topic policies; contextual grounding is
    # OUTPUT-only because it needs a model response to evaluate.
    guardrail_enabled = _b("BEDROCK_GUARDRAIL_ENABLED", False)
    guardrail_id = os.getenv("BEDROCK_GUARDRAIL_ID", "")
    guardrail_version = os.getenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    guardrail_grounding_threshold = _f("BEDROCK_GUARDRAIL_GROUNDING_THRESHOLD", 0.7)
    guardrail_relevance_threshold = _f("BEDROCK_GUARDRAIL_RELEVANCE_THRESHOLD", 0.7)

    # --- data retention (RVB-X-09 — compliance control, not a tunable) ---- #
    # Bedrock resolves the effective mode as the first non-inherit value of
    # (project -> account -> model default). For a PHI workload it must be
    # "none". A model whose allowed_modes excludes "none" is disqualified
    # regardless of capability: it would share prompts and completions with the
    # provider for up to 30 days. See ADR 0004 §1a.
    require_zero_retention = _b("BEDROCK_REQUIRE_ZERO_RETENTION", True)
    # Escape hatch for a non-PHI sandbox ONLY. Setting this in an environment
    # that touches PHI is the same class of error as the README's
    # "HIPAA compliant" claim.
    retention_preflight_skip = _b("BEDROCK_RETENTION_PREFLIGHT_SKIP", False)


settings = Settings()
