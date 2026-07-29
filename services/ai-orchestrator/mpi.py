"""Identity resolution over the patient dump — the Week-2 finding, in code.

`db/schema.sql` annotates `patients.mrn` as explicitly *not* used as a match key,
and there is no unique constraint on any identity tuple. `intake.yaml` records
`match_key: none`. Self-service intake therefore writes a new row every time,
with no attempt to recognise a returning human.

The dump the client handed us contains the consequence. One person, three rows:

    1042  Maria Gonzalez   1971-03-02   412-55-9981   MRN M4471
    1330  Maria Gonzales   1971-03-02   412-55-9981   MRN M4471   <- penicillin allergy
    1588  M. Gonzalez      1971-02-03   412-55-9981   MRN M4471   <- day/month transposed

Same SSN, same address, same phone, same member id. Three charts.

This module does NOT implement an MPI. ADR 0007 proposes one and it is
service-sized, roadmapped work. What lives here is the smaller, sharper thing:
the *detection*, so the eval harness can report a number that makes the problem
undeniable, and so W4's knowledge graph has a `SAME_AS` edge to traverse.

Design notes
------------
**Blocking, then scoring.** Compare only candidates that share a cheap blocking
key (SSN, or MRN, or a name-ish soundalike) rather than every pair. At real
patient volumes an all-pairs comparison is quadratic and never ships.

**Link, do not merge.** The output is a set of assertions "these ids are one
human", not a mutation. Merging charts is irreversible and can itself cause a
safety incident — merge two different people and you get one chart with one
person's allergies and another's medications. A missed link is the status quo; a
wrong merge is a new harm. That asymmetry decides the design.

**Score, then surface.** Anything below the certain threshold is a *candidate*
for a human, not an automatic link.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Deterministic match on any of these is treated as certain: they are issued
# identifiers, not typed-in text.
_STRONG_KEYS = ("ssn", "mrn")

_NICKNAMES = {
    "bill": "william", "billy": "william", "will": "william",
    "bob": "robert", "bobby": "robert", "rob": "robert",
    "jim": "james", "jimmy": "james", "jamie": "james",
    "mike": "michael", "mikey": "michael",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine",
    "liz": "elizabeth", "beth": "elizabeth", "betty": "elizabeth",
    "tony": "anthony", "chris": "christopher", "dave": "david",
    "steve": "stephen", "sue": "susan", "peg": "margaret", "maggie": "margaret",
}


def normalize_name(name: str) -> str:
    """Casefold, strip diacritics and punctuation, collapse whitespace.

    Deliberately explicit rather than a fuzzy library: a rule you can read is a
    rule you can defend to an auditor asking why two charts were linked.
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-zA-Z\s]", " ", text).lower()
    return re.sub(r"\s+", " ", text).strip()


def name_tokens(name: str) -> list[str]:
    out = []
    for tok in normalize_name(name).split():
        out.append(_NICKNAMES.get(tok, tok))
    return out


def name_key(name: str) -> str:
    """Surname plus first initial — the blocking key.

    'Maria Gonzalez', 'Maria Gonzales' and 'M. Gonzalez' must land in the same
    block, or the three fragments are never even compared. Note 'Gonzalez' and
    'Gonzales' differ in the final consonant, so the key drops trailing s/z.
    """
    toks = name_tokens(name)
    if not toks:
        return ""
    surname = re.sub(r"[sz]+$", "", toks[-1])
    initial = toks[0][0] if toks[0] else ""
    return f"{surname}|{initial}"


def normalize_dob(dob: str) -> str:
    return re.sub(r"[^0-9]", "", dob or "")


def dob_is_transposition(a: str, b: str) -> bool:
    """True when two DOBs differ only by a day/month swap.

    `1971-03-02` vs `1971-02-03`. This is not an exotic edge case — it is what
    happens when a US form and a patient's mental format disagree, and it is
    exactly the third Maria fragment. The current schema stores `dob` as TEXT, so
    the two strings are simply unequal and nothing notices.
    """
    da, db = normalize_dob(a), normalize_dob(b)
    if len(da) != 8 or len(db) != 8 or da == db:
        return False
    return da[:4] == db[:4] and da[4:6] == db[6:8] and da[6:8] == db[4:6]


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@dataclass
class PatientRow:
    id: int
    name: str
    dob: str = ""
    ssn: str = ""
    mrn: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    created_via: str = ""


@dataclass
class MatchScore:
    a: int
    b: int
    score: float
    certain: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class IdentityCluster:
    """One human, and every patient_id the system believes is them."""

    patient_ids: list[int]
    display_name: str
    certain: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def is_fragmented(self) -> bool:
        return len(self.patient_ids) > 1


def score_pair(a: PatientRow, b: PatientRow) -> MatchScore:
    """Score two rows as the same human. Explainable, not a black box."""
    reasons: list[str] = []
    score = 0.0
    certain = False

    for key in _STRONG_KEYS:
        va, vb = (getattr(a, key) or "").strip(), (getattr(b, key) or "").strip()
        if va and va == vb:
            reasons.append(f"identical_{key}")
            score += 0.6
            certain = True

    ta, tb = name_tokens(a.name), name_tokens(b.name)
    if ta and tb:
        if ta == tb:
            reasons.append("identical_name")
            score += 0.25
        elif ta[-1] and tb[-1] and _edit_distance(ta[-1], tb[-1]) <= 1:
            reasons.append("surname_within_edit_distance_1")
            score += 0.15
            if ta[0][:1] == tb[0][:1]:
                reasons.append("same_given_initial")
                score += 0.05

    if normalize_dob(a.dob) and normalize_dob(a.dob) == normalize_dob(b.dob):
        reasons.append("identical_dob")
        score += 0.2
    elif dob_is_transposition(a.dob, b.dob):
        # Worth its own reason string: it is the single most common DOB defect
        # and it is invisible while dob is stored as TEXT.
        reasons.append("dob_day_month_transposed")
        score += 0.15

    for attr, weight in (("address", 0.1), ("phone", 0.1), ("email", 0.05)):
        va, vb = (getattr(a, attr) or "").strip().lower(), (getattr(b, attr) or "").strip().lower()
        if va and va == vb:
            reasons.append(f"identical_{attr}")
            score += weight

    return MatchScore(a=a.id, b=b.id, score=round(min(score, 1.0), 4),
                      certain=certain, reasons=reasons)


def _blocking_keys(row: PatientRow) -> set[str]:
    keys = set()
    if row.ssn:
        keys.add(f"ssn:{re.sub(r'[^0-9]', '', row.ssn)}")
    if row.mrn:
        keys.add(f"mrn:{row.mrn.strip().lower()}")
    nk = name_key(row.name)
    if nk:
        keys.add(f"name:{nk}")
    return keys


def resolve(rows: Iterable[PatientRow], threshold: float = 0.7) -> list[IdentityCluster]:
    """Cluster rows into humans. Union-find over blocked, scored pairs."""
    rows = list(rows)
    by_id = {r.id: r for r in rows}

    blocks: dict[str, list[int]] = {}
    for row in rows:
        for key in _blocking_keys(row):
            blocks.setdefault(key, []).append(row.id)

    parent = {r.id: r.id for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    reasons_by_root: dict[int, set[str]] = {}
    certain_by_root: dict[int, bool] = {}

    seen_pairs: set[tuple[int, int]] = set()
    for ids in blocks.values():
        if len(ids) < 2:
            continue
        for i, aid in enumerate(sorted(ids)):
            for bid in sorted(ids)[i + 1 :]:
                if (aid, bid) in seen_pairs:
                    continue
                seen_pairs.add((aid, bid))
                match = score_pair(by_id[aid], by_id[bid])
                if match.certain or match.score >= threshold:
                    union(aid, bid)
                    root = find(aid)
                    reasons_by_root.setdefault(root, set()).update(match.reasons)
                    certain_by_root[root] = certain_by_root.get(root, False) or match.certain

    grouped: dict[int, list[int]] = {}
    for row in rows:
        grouped.setdefault(find(row.id), []).append(row.id)

    clusters: list[IdentityCluster] = []
    for root, ids in grouped.items():
        ids = sorted(ids)
        # Prefer the longest name as the display form: "Maria Gonzalez" reads
        # better than "M. Gonzalez" in a duplicate report a human has to review.
        display = max((by_id[i].name for i in ids), key=lambda n: (len(n or ""), n or ""))
        clusters.append(IdentityCluster(
            patient_ids=ids,
            display_name=display,
            certain=certain_by_root.get(root, False),
            reasons=sorted(reasons_by_root.get(root, set())),
        ))
    return sorted(clusters, key=lambda c: c.patient_ids[0])


def duplicate_rate(clusters: list[IdentityCluster]) -> float:
    """Fraction of distinct humans represented by more than one patient_id."""
    if not clusters:
        return 0.0
    fragmented = sum(1 for c in clusters if c.is_fragmented)
    return round(fragmented / len(clusters), 4)


def cluster_for(clusters: list[IdentityCluster], patient_id: int) -> Optional[IdentityCluster]:
    for cluster in clusters:
        if patient_id in cluster.patient_ids:
            return cluster
    return None


def same_as_edges(clusters: list[IdentityCluster]) -> list[tuple[int, int]]:
    """Undirected SAME_AS pairs, for the W4 knowledge graph."""
    edges: list[tuple[int, int]] = []
    for cluster in clusters:
        ids = cluster.patient_ids
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                edges.append((a, b))
    return edges
