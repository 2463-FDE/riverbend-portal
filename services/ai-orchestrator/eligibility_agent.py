"""The front-desk eligibility assistant — one tool, visit-scoped memory.

    "Front-desk eligibility checks are painful. Build a little assistant the
     staff can chat with that checks a patient's insurance eligibility and
     remembers the context of the visit."

Built on LangChain v1 ``create_agent``, **not** the deprecated
``create_react_agent`` (LangGraph v1 deprecates it in favour of ``create_agent``,
which adds middleware and a simpler surface — ADR 0004). ``create_agent``
compiles to a LangGraph runtime, so checkpointing comes for free without
hand-building a graph.

Exactly one tool
----------------
`check_eligibility`, and a test asserts the tool list length is 1. Every tool
added to an agent dilutes its tool-choice accuracy; the client asked for
eligibility, so the agent gets eligibility. If someone later wants scheduling
too, that is a decision with a cost, and the test makes them make it explicitly.

Memory is scoped to a VISIT
---------------------------
``thread_id = visit_id``. A visit is the unit of front-desk work, it has a
natural end, and it bounds how long PHI-bearing conversation state lives.
Patient-scoped memory would accumulate indefinitely and quietly turn the
checkpoint store into an unmanaged clinical record with no retention policy.

Checkpointed state is PHI at rest
---------------------------------
Staff type free text, and free text at a front desk contains patient names. The
production checkpointer therefore uses ``EncryptedSerializer``; tests use
``InMemorySaver``. The failure mode is silent — an unencrypted checkpoint store
looks identical to an encrypted one until someone reads the disk — so a config
test asserts the selection rather than trusting the deploy.

Honest degradation is ENFORCED, not prompted
--------------------------------------------
AWS documents Bedrock contextual grounding as not supporting conversational /
chatbot use cases, so this path deliberately does not get a grounding score
(ADR 0008). It gets a control better suited to its shape: a deterministic
post-check that the coverage status the agent *states* matches the status the
tool *returned*. On mismatch the reply is replaced with a templated statement of
the tool's actual result.

A model that says "you're covered" because that is the statistically common
answer has invented a clinical-billing fact. No prompt reliably prevents that; a
post-check does.
"""
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from config import settings
from logging_config import configure

log = configure(settings.service_name)

SYSTEM_PROMPT = (
    "You are a front-desk assistant at Riverbend Community Health. You help "
    "staff check a patient's insurance eligibility during a visit.\n\n"
    "Rules you must follow:\n"
    "- Use the check_eligibility tool to look up coverage. Never state a "
    "coverage status you did not get from the tool.\n"
    "- If the tool returns 'unknown', say plainly that coverage could not be "
    "verified and that registration should proceed with coverage marked "
    "unverified. Do NOT guess.\n"
    "- If the tool returns a stale result, say so and give the time it was last "
    "checked. Never present a stale status as current.\n"
    "- Be brief. The person reading this has a patient standing in front of them."
)


# --------------------------------------------------------------------------- #
# the deterministic post-check
# --------------------------------------------------------------------------- #
_ACTIVE_CLAIM = re.compile(
    r"\b(is|are|they'?re|you'?re|patient is)?\s*(currently\s+)?"
    r"(active|covered|eligible|in effect|valid)\b",
    re.IGNORECASE,
)
_INACTIVE_CLAIM = re.compile(
    r"\b(not active|inactive|not covered|no coverage|ineligible|lapsed|expired)\b",
    re.IGNORECASE,
)
_UNCERTAIN = re.compile(
    r"\b(could not|couldn'?t|unable|unknown|unverified|not verified|"
    r"unreachable|try again|proceed with registration)\b",
    re.IGNORECASE,
)


@dataclass
class ConsistencyVerdict:
    consistent: bool
    claimed: str            # active | inactive | unknown | none
    tool_status: str
    reason: str = ""


def claimed_status(text: str) -> str:
    """What coverage status does this reply assert? Order matters."""
    body = text or ""
    if _INACTIVE_CLAIM.search(body):
        return "inactive"
    if _UNCERTAIN.search(body):
        return "unknown"
    if _ACTIVE_CLAIM.search(body):
        return "active"
    return "none"


def check_consistency(reply: str, tool_status: str) -> ConsistencyVerdict:
    """Does the reply's coverage claim match what the tool actually returned?"""
    claimed = claimed_status(reply)

    if claimed == "none":
        # Stating no coverage status at all is fine — "what would you like to
        # check?" is a legitimate turn.
        return ConsistencyVerdict(True, claimed, tool_status)

    if tool_status == "unknown":
        if claimed != "unknown":
            return ConsistencyVerdict(
                False, claimed, tool_status,
                "the tool could not verify coverage but the reply asserts a status",
            )
        return ConsistencyVerdict(True, claimed, tool_status)

    if claimed != tool_status:
        return ConsistencyVerdict(
            False, claimed, tool_status,
            f"reply claims {claimed!r} but the tool returned {tool_status!r}",
        )
    return ConsistencyVerdict(True, claimed, tool_status)


def templated_reply(tool_result: dict) -> str:
    """What we say instead, when the model's reply cannot be trusted."""
    status = tool_result.get("status", "unknown")
    if status == "unknown":
        return (
            "Coverage could not be verified right now — the payer is "
            "unreachable. Please proceed with registration and mark coverage as "
            "unverified."
        )
    label = "Active" if status == "active" else "Not active"
    if tool_result.get("stale"):
        return (
            f"{label} — this is the last known status "
            f"({tool_result.get('checked_at_display', 'earlier')}); the payer is "
            f"currently unreachable, so treat it as provisional."
        )
    return f"{label} — verified just now."


# --------------------------------------------------------------------------- #
# checkpointer selection
# --------------------------------------------------------------------------- #
def build_checkpointer():
    """InMemorySaver in dev/CI; an encrypted durable saver in production.

    Conversation state contains what staff typed, which contains patient names.
    That is PHI at rest in a store this feature created, so it is encrypted —
    and notably it is the ONE place in this system where PHI at rest actually is
    encrypted (contrast D3).
    """
    if not settings.agent_durable_memory:
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver(), "memory", False

    from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver
    import sqlite3

    serde = EncryptedSerializer.from_pycryptodome_aes()
    conn = sqlite3.connect(settings.agent_checkpoint_path, check_same_thread=False)
    return SqliteSaver(conn, serde=serde), "sqlite", True


# --------------------------------------------------------------------------- #
# the agent
# --------------------------------------------------------------------------- #
@dataclass
class AgentTurn:
    visit_id: str
    reply: str
    tool_called: bool
    tool_status: str = ""
    stale: bool = False
    overridden: bool = False
    override_reason: str = ""
    usage: dict = field(default_factory=dict)


class EligibilityAgent:
    def __init__(
        self,
        *,
        eligibility_lookup: Callable[[str], dict],
        model: Any = None,
        checkpointer: Any = None,
    ):
        self._lookup = eligibility_lookup
        self._model = model
        self._checkpointer = checkpointer
        self._agent = None
        self._last_tool_result: dict = {}

    # -- the single tool ---------------------------------------------------- #
    def _build_tool(self):
        from langchain_core.tools import tool

        lookup = self._lookup
        record = self._record_tool_result

        @tool
        def check_eligibility(insurance_id: str) -> str:
            """Check a patient's insurance eligibility by member id.

            Returns the coverage status. A 'unknown' status means the payer could
            not be reached — it does NOT mean the patient is uncovered.
            """
            result = lookup(insurance_id)
            record(result)
            status = result.get("status", "unknown")
            if status == "unknown":
                return ("status=unknown — the payer could not be reached. "
                        "Coverage is unverified, not uncovered.")
            stale = " (STALE: last known value, payer currently unreachable)" if result.get("stale") else ""
            return f"status={status}{stale}"

        return check_eligibility

    def _record_tool_result(self, result: dict) -> None:
        self._last_tool_result = dict(result or {})

    # -- graph -------------------------------------------------------------- #
    def _build(self):
        if self._agent is not None:
            return self._agent
        from langchain.agents import create_agent

        model = self._model
        if model is None:
            from langchain_aws import ChatBedrockConverse
            model = ChatBedrockConverse(
                model=settings.agent_model_id,
                region_name=settings.aws_region,
                max_tokens=settings.max_output_tokens,
            )

        checkpointer = self._checkpointer
        if checkpointer is None:
            checkpointer, _kind, _enc = build_checkpointer()

        self._agent = create_agent(
            model=model,
            tools=[self._build_tool()],
            system_prompt=SYSTEM_PROMPT,
            checkpointer=checkpointer,
        )
        return self._agent

    @property
    def tools(self) -> list:
        return [self._build_tool()]

    # -- a turn -------------------------------------------------------------- #
    def turn(self, visit_id: str, message: str) -> AgentTurn:
        """One conversational turn, scoped to a visit."""
        self._last_tool_result = {}
        agent = self._build()
        config = {"configurable": {"thread_id": visit_id}}

        state = agent.invoke({"messages": [{"role": "user", "content": message}]},
                             config=config)
        messages = state.get("messages", [])
        reply = _last_text(messages)

        tool_result = self._last_tool_result
        tool_called = bool(tool_result)
        tool_status = tool_result.get("status", "")

        overridden = False
        reason = ""
        if tool_called:
            verdict = check_consistency(reply, tool_status)
            if not verdict.consistent:
                # The model asserted a coverage status the tool did not support.
                # Replace it — do not "warn" and serve it anyway.
                reply = templated_reply(tool_result)
                overridden = True
                reason = verdict.reason
                log.warning(
                    "agent visit=%s OVERRIDDEN claimed=%s tool=%s",
                    _redact_visit(visit_id), verdict.claimed, tool_status,
                )

        log.info(
            "agent visit=%s tool_called=%s status=%s stale=%s overridden=%s",
            _redact_visit(visit_id), tool_called, tool_status or "-",
            tool_result.get("stale", False), overridden,
        )

        return AgentTurn(
            visit_id=visit_id,
            reply=reply,
            tool_called=tool_called,
            tool_status=tool_status,
            stale=bool(tool_result.get("stale")),
            overridden=overridden,
            override_reason=reason,
        )


def _last_text(messages: list) -> str:
    for message in reversed(messages or []):
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _redact_visit(visit_id: str) -> str:
    """Visit ids are opaque, but log a prefix rather than the whole thing."""
    return (visit_id or "")[:8]


# --------------------------------------------------------------------------- #
# third-party tracing — off unless BOTH a flag and a key are present
# --------------------------------------------------------------------------- #
def configure_tracing() -> bool:
    """Staff type patient names, and regex scrubbing does not catch names.

    Any trace sink that uploads prompt bodies is therefore an un-BAA'd disclosure
    path for THIS endpoint specifically (W1 recorded the limitation; here it
    becomes material). Requires both an explicit flag and a key — a stale ambient
    flag alone must not be able to start shipping prompts off-box.
    """
    enabled = bool(settings.trace_enabled and settings.trace_api_key)
    os.environ["LANGSMITH_TRACING"] = "true" if enabled else "false"
    if enabled:
        os.environ["LANGSMITH_API_KEY"] = settings.trace_api_key
        os.environ["LANGSMITH_PROJECT"] = settings.trace_project
        log.warning(
            "third-party tracing ENABLED — prompt bodies leave the PHI boundary. "
            "This requires a BAA covering the trace vendor, or name detection on "
            "this path. See adr/0008."
        )
    return enabled
