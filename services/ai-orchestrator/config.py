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

    # --- RAG: embeddings (W2) --------------------------------------------- #
    # Titan Text Embeddings V2: 8,192 tokens / 50,000 chars in; 1024 (default),
    # 512 or 256 out. 256 is chosen for the sampled demo corpus — a quarter of
    # the index size and query cost, no measurable retrieval loss at this scale.
    embed_model_id = os.getenv("BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
    embed_dims = _i("RAG_EMBED_DIMS", 256)
    # offline | titan. Offline is deterministic, free, and needs no credentials;
    # it is what CI uses so the eval harness is reproducible.
    embed_backend = os.getenv("RAG_EMBED_BACKEND", "offline")

    # --- RAG: chunking ----------------------------------------------------- #
    chunk_tokens = _i("RAG_CHUNK_TOKENS", 120)
    chunk_overlap_tokens = _i("RAG_CHUNK_OVERLAP_TOKENS", 24)

    # --- RAG: Chroma ------------------------------------------------------- #
    # memory | persistent | http. `http` points at the compose Chroma service and
    # is the production shape; the interface is identical either way (ADR 0006).
    chroma_mode = os.getenv("CHROMA_MODE", "memory")
    chroma_path = os.getenv("CHROMA_PATH", "")
    chroma_host = os.getenv("CHROMA_HOST", "chroma")
    chroma_port = _i("CHROMA_PORT", 8000)

    # --- RAG: retrieval ---------------------------------------------------- #
    retrieval_mode = os.getenv("RAG_RETRIEVAL_MODE", "hybrid")
    retrieve_k = _i("RAG_RETRIEVE_K", 4)
    hybrid_dense_weight = _f("RAG_HYBRID_DENSE_WEIGHT", 1.0)
    # Lexical is weighted up: clinic queries turn on exact tokens ("penicillin",
    # "A1C", "fasting", an MRN) that dense retrieval smears, and the offline
    # embedding backend is a deliberately weak semantic signal.
    hybrid_sparse_weight = _f("RAG_HYBRID_SPARSE_WEIGHT", 2.0)

    # Refuse-vs-answer floors. Term coverage is the primary gate because it is
    # corpus-size independent; dense similarity is the paraphrase fallback. A
    # query must fail BOTH to be refused.
    min_term_coverage = _f("RAG_MIN_TERM_COVERAGE", 0.30)
    min_semantic_score = _f("RAG_MIN_SEMANTIC_SCORE", 0.55)

    # --- RAG: quota discipline (RVB-W2-11) --------------------------------- #
    # Week 2 is flagged as a quota-risk week. Raise this deliberately, never by
    # accident: an uncapped ingest over a full record dump is the failure mode
    # the client packet warns about.
    corpus_max_chunks = _i("RAG_CORPUS_MAX_CHUNKS", 500)

    # --- MPI / identity resolution (W2 finding) ---------------------------- #
    mpi_match_threshold = _f("MPI_MATCH_THRESHOLD", 0.7)

    # --- W3: the eligibility agent ----------------------------------------- #
    eligibility_url = os.getenv("ELIGIBILITY_URL", "http://eligibility-service:8072")
    eligibility_timeout_s = _f("ELIGIBILITY_TIMEOUT_S", 10.0)

    # Durable, ENCRYPTED checkpoints in production. Conversation state contains
    # what staff typed, which contains patient names — PHI at rest in a store
    # this feature created. The failure mode is silent (an unencrypted store
    # looks identical until someone reads the disk), so a test asserts the
    # selection rather than trusting the deploy.
    agent_durable_memory = _b("AGENT_DURABLE_MEMORY", False)
    agent_checkpoint_path = os.getenv("AGENT_CHECKPOINT_PATH", "/data/agent-checkpoints.db")

    # --- third-party tracing ------------------------------------------------ #
    # OFF unless BOTH a flag and a key are present. Staff type patient names and
    # regex scrubbing does not catch names, so a trace sink that uploads prompt
    # bodies is an un-BAA'd disclosure path for the agent endpoint. See adr/0008.
    trace_enabled = _b("LANGSMITH_TRACING", False)
    trace_api_key = os.getenv("LANGSMITH_API_KEY", "")

    # adr/0014 — staging store for the two-phase ingest gate. Named here rather
    # than read ad hoc so the ingest path has one source of truth for it.
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    trace_project = os.getenv("LANGSMITH_PROJECT", "riverbend-portal")

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
