"""Keep PHI out of the model prompt, the trace, and the index.

The contractor's version put the full patient record (name, DOB, MRN, notes) into
the prompt and its "deidentify" dropped only the ``name`` field. That is an
impermissible-disclosure risk under 164.502(e) and it is not Safe-Harbor
de-identification under 164.514(b) — dropping one of eighteen identifiers is not
de-identification, it is a rounding error.

W1 removes the problem by construction: the intake-summary feature summarizes
*instruction text*, which carries no patient identifiers. This module is the
enforcement backstop for text that contains an identifier anyway (a patient pastes
their SSN into a free-text field), not the primary control. The primary control is
that the request model has no field in which a patient record could be expressed.

Two scrubs, deliberately different
----------------------------------
``scrub_instructions`` is aggressive. Instruction text should carry no identifiers
at all, so over-redacting costs nothing.

``scrub_document`` is lenient about dates and contact details, because a clinic
policy document legitimately contains "call 555-0100 to reschedule" and "effective
2026-01-01". Redacting those destroys the document's meaning for no privacy gain.

KNOWN LIMITATION, stated rather than buried
-------------------------------------------
Regex scrubbing does NOT detect patient **names**. W1's instructions-only contract
makes that acceptable. It becomes material in W3, where staff type free-text
questions that will contain names ("check eligibility for Maria Gonzalez"), which
is why third-party tracing is off by default on that path. See ADR 0008.
"""
import re
from dataclasses import dataclass, field

# Ordered so the most specific patterns run first: an SSN would otherwise be
# partially consumed by the phone pattern.
_PATTERNS = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b(?<!\d)\d{9}(?!\d)\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "mrn": re.compile(r"\bMRN[:#]?\s*[\w-]+", re.IGNORECASE),
    "phone": re.compile(r"\b\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
    # DOB-shaped bare dates
    "date": re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b"),
    "long_num": re.compile(r"\b\d{10,}\b"),
}

# Identifier kinds that must never enter a knowledge document even by accident.
# Dates, clinic phone and clinic email are legitimate content in a procedure doc.
_DOC_REDACT = ("ssn", "mrn", "long_num")

# A patient RECORD is a different animal: it goes to its own Chroma collection
# which is declared a PHI store (W2 spec §4b). We strip direct identifiers that
# retrieval does not need, and deliberately KEEP clinical content and dates,
# because a record corpus stripped of dates cannot answer "what did her last three
# visits say?" — that would destroy the feature in order to protect it.
_RECORD_REDACT = ("ssn", "email", "phone", "mrn", "long_num")


@dataclass
class ScrubResult:
    text: str
    found: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.found


def _apply(text: str, kinds) -> ScrubResult:
    found: list[str] = []
    out = text or ""
    for kind in kinds:
        pat = _PATTERNS[kind]
        if pat.search(out):
            found.append(kind)
            out = pat.sub(f"[REDACTED-{kind.upper()}]", out)
    return ScrubResult(text=out, found=found)


def scrub_instructions(text: str) -> ScrubResult:
    """Redact anything PHI-shaped from instruction text before the model sees it.

    We redact rather than reject: a front-desk clerk pasting a phone number into
    the instructions field made a mistake, and failing their request teaches them
    to route around the tool. ``found`` reports the *kinds* so the endpoint can
    flag it — the values are never returned and never logged, because logging what
    you redacted defeats the redaction.
    """
    return _apply(text, _PATTERNS.keys())


def scrub_document(text: str) -> ScrubResult:
    """Lenient scrub for knowledge-base documents (policy / procedure / plan)."""
    return _apply(text, _DOC_REDACT)


def scrub_record(text: str) -> ScrubResult:
    """Scrub for patient-record text destined for the PHI-declared collection."""
    return _apply(text, _RECORD_REDACT)


class ClinicalPathNotAvailable(RuntimeError):
    """Raised if anyone routes a clinical record through the W1 summary path."""


def safe_harbor_scrub(_text: str) -> ScrubResult:
    """Placeholder for the full 45 CFR 164.514(b)(2) Safe-Harbor scrub.

    Deliberately raises. The clinical-record summary path needs all eighteen
    identifiers handled AND an executed AWS BAA, and both are W8 scope. This
    tripwire exists so that wiring an encounter through the W1 endpoint fails
    loudly at import-time-of-use rather than quietly shipping PHI to a model.
    """
    raise ClinicalPathNotAvailable(
        "The clinical-record summary path is W8 scope. It requires the full "
        "Safe-Harbor de-identification (164.514(b)(2), 18 identifiers) and an "
        "executed AWS BAA. W1 summarizes intake INSTRUCTION text only."
    )
