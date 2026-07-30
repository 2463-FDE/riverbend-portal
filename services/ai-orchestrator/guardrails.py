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


# Discourse vocabulary: the words an answer adds to be USEFUL rather than to
# assert anything clinical -- hedging, attribution, advice, and the machinery of
# a sentence. Excluded from the support calculation.
#
# This list is the whole fix. `grounding_score` counts what fraction of the
# OUTPUT's terms appear in the source, so an answer is penalised for every word
# it adds -- and the words a careful answer adds are exactly these. Measured, it
# scored faithful live answers 0.176-0.521 while scoring "You have no known drug
# allergies" -- a flat contradiction built from source vocabulary -- at 0.500.
#
# Excluding discourse terms leaves the CLINICAL terms, which is what actually has
# to be supported. See adr/0016 and docs/findings/w2-thresholds-measured-the-stub.md.
_DISCOURSE = frozenset("""
appear appears based clarify clarified confirm confirmed conflicting consult
contains context different discuss doctor documented following found however
indicate indicates indicating information listed multiple note noted provider
provided providers question record records recorded regarding report reported
require requires review reviewed same show showing shown shows similar specific
staff suggest suggests unclear unable verify whether which while
healthcare please should would could may might must need needs advised
according additionally also although because before both cannot definitively
either further given however instead means neither otherwise particularly
rather since therefore though unless unfortunately unless whereas
answer available currently details entry general history overall present
question responses summary text passage passages source sources
conflict conflicts differ differs disagree disagrees discrepancy inconsistent
list lists listed mention mentions one two three several each other others
appear appears seem seems there here this that these those across between
date dated day month year time entry visit visits chart charts number numbers
name named names naming call called known unknown per via within
""".split())

# The stoplist EXCLUDES terms from the support check, so a clinical word landing
# in it would be a hole in the gate -- "pregnant" here would release "You are
# pregnant". Pinned by `test_no_clinical_vocabulary_is_exempted`.


# Citation markers. `[1]` tokenises to "1", and "1" occurs in almost any clinical
# passage (chart 1042, a date, a dose), so an answer earned "support" for the act
# of citing. Measured: it took "You are pregnant. [1]" from 0.000 to 0.500 --
# every attack in the codex:rescue set was inflated by exactly this.
_CITATION = re.compile(r"\[\s*\d+\s*\]")


def clinical_terms(text: str) -> set:
    """Content terms that assert something.

    Discourse vocabulary and citation markers removed: neither asserts a clinical
    fact, and both otherwise count toward support.
    """
    return {t for t in content_terms(_CITATION.sub(" ", text or ""))
            if t not in _DISCOURSE}


def clinical_support(output: str, source: str) -> float:
    """Fraction of the output's CLINICAL terms that appear in the source.

    The same direction as `grounding_score` -- how much of the output is
    supported -- over the terms that carry clinical meaning rather than framing.

    Why this and not the old score: an answer is no longer punished for saying
    "you should clarify this with your provider", and it is no longer excused
    because it reused the words "allergies" and "recorded". Every clinical noun,
    drug, measurement and condition it introduces has to be in the source.

    Measured on the same populations that broke the old metric:

        faithful (hedged synthesis)        1.000
        "You are pregnant"                 0.000
        "blood pressure was 160/100"       0.000
        "elevated A1C"                     0.000
        "anaphylactic reaction"            0.500

    Numbers are kept as terms deliberately: an invented vital sign or lab value
    is a fabricated clinical fact, and dropping digits would make it invisible.
    """
    out_terms = clinical_terms(output)
    if not out_terms:
        # Pure framing with no clinical content asserts nothing to support. The
        # refusal message itself lands here.
        return 1.0
    src_terms = content_terms(source)
    return round(len(out_terms & src_terms) / len(out_terms), 4)


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


def unsupported_clinical_terms(output: str, source: str) -> list[str]:
    """Clinical terms the output asserts and the source does not contain.

    **This is the gate** (ADR 0016 §3, revised after codex:rescue F1).

    Not a fraction. `grounding_score` asked "what proportion of this is
    supported?", which a partial fabrication passes easily by reusing real
    vocabulary -- "You had an anaphylactic reaction to penicillin" is 2/3
    supported and entirely invented. The question that matters is "is there
    anything here the record does not say?", and the answer has to be no.

    Verified against every adversarial case in the codex:rescue set, all of which
    the fraction-based gate released:

        "You are pregnant"                              -> pregnant
        "Your blood pressure was 160/100"               -> blood, pressure, 160, 100
        "Your lab results show elevated A1C"            -> a1c, elevat, lab, result
        "anaphylactic reaction to penicillin"           -> anaphylactic, reaction
        "sinus infection was caused by strep throat"    -> caus, strep, throat
        "penicillin allergy has resolved"               -> resolv, longer, activ

    **It fails CLOSED.** A word missing from `_DISCOURSE` produces a refusal,
    never a release, so the failure mode of an incomplete stoplist is an
    over-cautious assistant rather than a fabricated vital sign. That direction
    is deliberate and is why the list is allowed to be imperfect.
    """
    return sorted(clinical_terms(output) - content_terms(source))


def check(output: str, source: str, threshold: float,
          *, strict_terms: bool = True) -> Verdict:
    """Tier-0 validation. Fails closed.

    Three checks, cheapest and most specific first:

      1. invented medications, dosages and clinical directives -- pattern-based,
         catches the classic "continue your metformin 500 mg" case;
      2. any UNSUPPORTED CLINICAL TERM -- the general case, added after
         codex:rescue showed check 1 alone released six fabrications;
      3. a term-overlap floor, kept only as a coarse backstop for output that
         shares almost no vocabulary with its sources.

    `threshold` applies to check 3 only. It is deliberately no longer the
    decision for synthesis: measured, faithful answers scored 0.176-0.521 on
    overlap and a flat contradiction scored 0.500, so no value of it separates
    the populations.

    **`strict_terms=False` for REWRITE tasks.** Check 2 requires the output to
    stay inside the source's vocabulary, which is right when the job is to report
    what a record says and wrong when the job is to say it in plainer words. The
    W1 summariser exists to turn "NPO x8h prior to phlebotomy" into "do not eat
    for eight hours before your blood draw" -- introducing vocabulary is the
    feature, so it keeps the fraction-based floor it was measured against (0.737
    live, threshold 0.55) plus check 1, which is what withholds the invented
    metformin dose.
    """
    score = grounding_score(output, source)

    reasons = invented_clinical_claims(output, source)
    if reasons:
        return Verdict(grounded=False, score=score, reasons=reasons)

    unsupported = unsupported_clinical_terms(output, source) if strict_terms else []
    if unsupported:
        # Capped: the reason string reaches logs and API responses, and the terms
        # are drawn from model output, which is not a place to be generous about
        # length.
        shown = ", ".join(unsupported[:6])
        return Verdict(
            grounded=False, score=score,
            reasons=[f"unsupported_clinical_terms:{shown}"],
        )

    if score < threshold:
        return Verdict(
            grounded=False, score=score,
            reasons=[f"grounding_score {score} below threshold {threshold}"],
        )

    return Verdict(grounded=True, score=score, reasons=[])


def safe_fallback() -> str:
    return SAFE_MESSAGE
