"""Tier-0 output validation — offline, free, deterministic, always on.

Two checks, both cheap enough to run on every request and every commit:

  1. **Grounding score.** Stemmed, stopword-filtered token overlap of the model's
     output against its source. Below the threshold, the output is withheld.

  2. **Clinical-claim heuristic.** The output is scanned for medication names,
     dosage patterns and diagnosis-shaped assertions that are absent from the
     source. Any hit fails the check regardless of the overlap score.

Check 2 exists for one specific reason. The contractor's version produced a
transcript inventing a medication the patient was not taking. A high-overlap
summary that adds "continue taking metformin 500mg" is *more* dangerous than an
obviously off-topic one, because it reads as competent. Overlap alone cannot catch
that: adding one clinical fact barely moves the score.

A failed check never returns raw model text. It returns a fixed safe message and
sets ``needs_review``, which routes to an asynchronous human queue rather than
blocking the request (debate D5 — human gates belong on low-volume, high-blast-
radius paths, not on every request).

These are heuristics and are meant to be. They are the FLOOR: no spend, runs in
CI on every commit, no credentials. The Bedrock-managed contextual grounding check
is the Tier-1 upgrade behind the same call site (ADR 0004 §4).
"""
import re
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    "a an the is are was were be been being of to in on at for from with by and "
    "or not no do does did i you we they it this that these those my your our "
    "how what when where which who whom why can could should would will shall "
    "have has had if then than so as about into out up down over under me your "
    "please make sure bring arrive before after your you'll".split()
)

SAFE_MESSAGE = (
    "A summary could not be generated safely for this text. A staff member will "
    "review it."
)

# Dosage is the highest-signal invented-clinical-fact pattern: prose rarely
# contains "500 mg" by accident, and a hallucinated dose is the failure with the
# worst consequence.
_DOSAGE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|µg|ug|ml|g|units?|iu)\b", re.IGNORECASE)

_DIAGNOSIS_FRAME = re.compile(
    r"\b(?:you (?:have|are diagnosed with)|diagnosed with|suffering from|"
    r"your diagnosis is|you should (?:take|stop taking)|prescribed)\b",
    re.IGNORECASE,
)

# A deliberately small, high-precision list. A big drug dictionary would produce
# false positives on ordinary words ("lead", "cortisone" in a policy doc) and the
# cost of a false positive here is a withheld summary, which is cheap, while the
# cost of a false negative is a clinical claim reaching a patient.
_COMMON_MEDS = frozenset(
    "metformin insulin lisinopril atorvastatin amlodipine metoprolol omeprazole "
    "levothyroxine albuterol gabapentin sertraline losartan prednisone warfarin "
    "amoxicillin penicillin ibuprofen acetaminophen hydrochlorothiazide "
    "simvastatin furosemide tramadol oxycodone".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens with a crude suffix stem. Deterministic."""
    out = []
    for tok in _WORD.findall((text or "").lower()):
        for suffix in ("ing", "ed", "es", "s"):
            if len(tok) > 4 and tok.endswith(suffix):
                tok = tok[: -len(suffix)]
                break
        out.append(tok)
    return out


def content_terms(text: str) -> set[str]:
    return {t for t in tokenize(text) if t not in _STOPWORDS}


@dataclass
class Verdict:
    grounded: bool
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return not self.grounded


def grounding_score(output: str, source: str) -> float:
    """Fraction of the output's content terms that appear in the source.

    Direction matters: we measure how much of the OUTPUT is supported by the
    source, not how much of the source was covered. A short faithful summary
    should score highly; a summary that adds material should not.
    """
    out_terms = content_terms(output)
    if not out_terms:
        return 0.0
    src_terms = content_terms(source)
    if not src_terms:
        return 0.0
    return round(len(out_terms & src_terms) / len(out_terms), 4)


def invented_clinical_claims(output: str, source: str) -> list[str]:
    """Clinical assertions present in the output and absent from the source."""
    reasons: list[str] = []
    src_lower = (source or "").lower()
    src_terms = content_terms(source)

    for match in _DOSAGE.finditer(output or ""):
        if match.group(0).lower() not in src_lower:
            reasons.append(f"invented_dosage:{match.group(0).strip()}")

    for tok in set(tokenize(output)):
        if tok in _COMMON_MEDS and tok not in src_terms:
            reasons.append(f"invented_medication:{tok}")

    if _DIAGNOSIS_FRAME.search(output or "") and not _DIAGNOSIS_FRAME.search(source or ""):
        reasons.append("unsupported_clinical_directive")

    return sorted(set(reasons))


def check(output: str, source: str, threshold: float) -> Verdict:
    """Tier-0 validation. Fails closed: any invented claim fails outright."""
    score = grounding_score(output, source)
    reasons = invented_clinical_claims(output, source)

    if reasons:
        # An invented clinical fact fails REGARDLESS of overlap. This is the
        # whole point of check 2: a mostly-faithful summary that adds a dose is
        # the dangerous case, and it scores well.
        return Verdict(grounded=False, score=score, reasons=reasons)

    if score < threshold:
        return Verdict(
            grounded=False, score=score,
            reasons=[f"grounding_score {score} below threshold {threshold}"],
        )

    return Verdict(grounded=True, score=score, reasons=[])


def safe_fallback() -> str:
    return SAFE_MESSAGE
