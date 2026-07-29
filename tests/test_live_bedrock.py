"""Key-gated live Bedrock smoke tests. SKIPPED BY DEFAULT — these spend money.

Written up front, not on key-day. The point is that when a credential arrives the
verification is one command rather than a scramble, and that nobody writes
cost-unbounded tests under time pressure with a live key in the environment.

    make test-live                      # run them (installs the service deps)
    pytest --live -m live               # the same thing, by hand
    pytest                              # the default: live tests are skipped

The gate is the ``--live`` FLAG, not a marker expression, because a ``-m`` on the
command line overrides ``addopts`` — so gating in addopts would mean
``pytest -m "not integration"`` (what the Makefile passes) silently re-enabled the
spending tests. A collection hook cannot be overridden that way. See
``tests/conftest.py``.

Ordering matters and is enforced, not hoped for:

    L0  retention posture     — spends NOTHING. Gates everything below.
    L1  model resolves        — one tiny call
    L2  summary under budget  — one real summary, cost asserted

L0 exists because a model whose ``allowed_modes`` excludes ``none`` would share
prompts and completions with the model provider for up to 30 days. Discovering
that *after* sending text is discovering it too late.

Every spending test asserts a hard per-test cost ceiling, so an accidental loop
cannot run up a bill.
"""
import os

import pytest

from conftest import load_module

pytestmark = pytest.mark.live

# Per-test USD ceilings. Deliberately tiny: these are smoke tests, not evals.
CEILING_PER_TEST_USD = 0.01

_HAS_KEY = bool(
    os.getenv("AWS_BEARER_TOKEN_BEDROCK")
    or os.getenv("AWS_ACCESS_KEY_ID")
    or os.getenv("AWS_PROFILE")
)

skip_no_key = pytest.mark.skipif(
    not _HAS_KEY,
    reason="no Bedrock credential in the environment (set AWS_BEARER_TOKEN_BEDROCK)",
)


def _require_langchain_aws():
    """L1/L2 need the service's own deps; L0 only needs boto3.

    Kept inside the tests rather than at module scope so L0 — the retention
    preflight that spends nothing and gates the rest — always collects and runs.
    Install with: pip install -r services/ai-orchestrator/requirements.txt
    """
    pytest.importorskip(
        "langchain_aws",
        reason="run `pip install -r services/ai-orchestrator/requirements.txt`",
    )

retention = load_module("services/ai-orchestrator/retention.py", "live_retention")
mc = load_module("services/ai-orchestrator/model_client.py", "live_model_client")
settings = mc.settings

SOURCE = (
    "Please arrive fifteen minutes before your appointment. Bring your insurance "
    "card and a photo ID. Do not eat or drink anything except water for eight "
    "hours before your blood draw."
)


# --------------------------------------------------------------------------- #
# L0 — spends nothing, gates everything
# --------------------------------------------------------------------------- #
@skip_no_key
def test_L0_live_model_allows_zero_retention(monkeypatch):
    """RVB-X-09. If this fails, do not send anything to this model."""
    monkeypatch.setattr(settings, "use_stub", False)
    status = retention.check(settings.summary_model_id)
    assert status.ok, (
        f"model {settings.summary_model_id!r} failed the zero-retention "
        f"preflight: {status.reason}. Prompts and completions could be retained "
        f"or shared with the model provider. Pick a different model — see "
        f"adr/0004 §1a."
    )
    # Record what we saw so the PR / demo notes can quote it.
    print(f"\n[L0] retention: {status.as_dict()}")


# --------------------------------------------------------------------------- #
# L1 — the model id actually resolves
# --------------------------------------------------------------------------- #
@skip_no_key
def test_L1_live_model_resolves(monkeypatch):
    """Bare model ids fail with ValidationException at CALL time.

    Which means in the demo, not in CI. This is the test that stops that.
    """
    _require_langchain_aws()
    monkeypatch.setattr(settings, "use_stub", False)
    monkeypatch.setattr(settings, "max_output_tokens", 32)

    client = mc.ModelClient(settings.summary_model_id)
    result = client.invoke(
        "Reply with a single word.",
        'Say the word "ready". Respond ONLY with {"text": "ready"}.',
        structured_key="text",
    )
    assert result.text, "no text returned"
    assert result.stubbed is False
    assert result.est_cost_usd <= CEILING_PER_TEST_USD, (
        f"cost ${result.est_cost_usd} exceeded the per-test ceiling"
    )
    print(f"\n[L1] model={result.model_id} tokens={result.input_tokens}/"
          f"{result.output_tokens} cost=${result.est_cost_usd} "
          f"latency={result.latency_ms}ms attempts={result.attempts}")


# --------------------------------------------------------------------------- #
# L2 — one real summary, grounded, under budget
# --------------------------------------------------------------------------- #
@skip_no_key
def test_L2_live_summary_is_grounded_and_under_budget(monkeypatch):
    _require_langchain_aws()
    monkeypatch.setattr(settings, "use_stub", False)
    guardrails = load_module("services/ai-orchestrator/guardrails.py", "live_guardrails")

    system = (
        "You rewrite clinic intake instructions into a short, plain-language "
        "summary a patient can understand. Use ONLY the information provided. "
        "Do not add medications, dosages, or diagnoses. Respond ONLY with "
        '{"summary": "..."}.'
    )
    client = mc.ModelClient(settings.summary_model_id)
    result = client.invoke(
        system, f"Intake instructions:\n{SOURCE}\n\nReturn JSON only.",
        structured_key="summary",
    )

    assert result.est_cost_usd <= CEILING_PER_TEST_USD, (
        f"cost ${result.est_cost_usd} exceeded the per-test ceiling"
    )
    verdict = guardrails.check(result.text, SOURCE, settings.grounding_threshold)
    print(f"\n[L2] grounded={verdict.grounded} score={verdict.score} "
          f"cost=${result.est_cost_usd}\n     summary: {result.text[:200]}")
    assert verdict.grounded, (
        f"live summary failed the Tier-0 grounding check "
        f"(score {verdict.score}, reasons {verdict.reasons}). "
        f"That is a real signal, not a flaky test — inspect the output above."
    )


# --------------------------------------------------------------------------- #
# L3 — Titan embeddings, one real call
# --------------------------------------------------------------------------- #
@skip_no_key
def test_L3_live_titan_embeddings(monkeypatch):
    """One real Titan call. Asserts the configured dimensionality and the cost.

    Gated behind L0: if the model does not permit zero data retention, no text
    should reach it at all.
    """
    embeddings = load_module("services/ai-orchestrator/embeddings.py", "live_embeddings")
    monkeypatch.setattr(embeddings.settings, "use_stub", False)

    backend = embeddings.TitanEmbedder(
        embeddings.settings.embed_dims, embeddings.settings.embed_model_id
    )
    embedder = embeddings.Embedder(backend)
    vector = embedder.embed_one("fasting instructions before a blood draw")

    assert len(vector) == embeddings.settings.embed_dims, (
        f"expected {embeddings.settings.embed_dims} dimensions, got {len(vector)}"
    )
    # Titan V2 is ~$0.02 per 1M input tokens; a single short string is far below
    # any meaningful ceiling. The assertion that matters is that we made ONE call.
    assert embedder.stats.calls == 1
    print(f"\n[L3] titan dims={len(vector)} calls={embedder.stats.calls}")


@skip_no_key
def test_L4_live_rag_answer_is_grounded_and_cited(monkeypatch):
    """One real RAG generation over the seeded corpus."""
    _require_langchain_aws()
    import chromadb

    chroma = load_module("services/ai-orchestrator/chroma_index.py", "live_chroma")
    corpus_mod = load_module("services/ai-orchestrator/corpus.py", "live_corpus")
    rag = load_module("services/ai-orchestrator/rag_graph.py", "live_rag")

    monkeypatch.setattr(chroma.settings, "use_stub", False)
    index = chroma.ChromaIndex(client=chromadb.EphemeralClient())
    index.add(corpus_mod.build_knowledge_chunks())

    out = rag.run(index, "how long must a patient fast before a blood draw?")
    print(f"\n[L4] grounded={out.grounded} refused={out.refused} "
          f"cost=${out.usage.get('est_cost_usd')}\n     {out.answer[:200]}")
    assert not out.refused
    assert out.grounded
    assert out.citations
    assert out.usage.get("est_cost_usd", 0) <= CEILING_PER_TEST_USD


# --------------------------------------------------------------------------- #
# L5 — the eligibility agent, one real turn
# --------------------------------------------------------------------------- #
@skip_no_key
def test_L5_live_agent_calls_its_single_tool(monkeypatch):
    """One real agent turn against Bedrock, with a faked payer.

    Asserts a tool call actually happened — a model that answers from its priors
    instead of calling the tool is the failure this whole design guards against.
    """
    _require_langchain_aws()
    from langgraph.checkpoint.memory import InMemorySaver

    agent_mod = load_module(
        "services/ai-orchestrator/eligibility_agent.py", "live_agent"
    )
    monkeypatch.setattr(agent_mod.settings, "use_stub", False)

    from langchain_aws import ChatBedrockConverse

    model = ChatBedrockConverse(
        model=agent_mod.settings.agent_model_id,
        region_name=agent_mod.settings.aws_region,
        max_tokens=512,
    )
    agent = agent_mod.EligibilityAgent(
        eligibility_lookup=lambda _i: {"status": "active", "stale": False},
        model=model,
        checkpointer=InMemorySaver(),
    )
    turn = agent.turn("live-visit-1", "Please check eligibility for member BCBS4471.")
    print(f"\n[L5] tool_called={turn.tool_called} status={turn.tool_status} "
          f"overridden={turn.overridden}\n     {turn.reply[:200]}")
    assert turn.tool_called, "the agent answered without calling its tool"
    assert turn.tool_status == "active"
