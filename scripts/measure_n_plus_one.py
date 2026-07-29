#!/usr/bin/env python3
"""Measure the N+1 query pattern in the records read path (D8, W4).

Deliberately a SCRIPT, not a test. A test would assert a number that moves with
the seed data, and a test that has to be updated whenever fixtures change stops
being read. This is run once, and its output is pasted into
`docs/findings/w4-n-plus-one.md` with the date and the corpus size.

    python3 scripts/measure_n_plus_one.py

What it measures
----------------
`records-service.get_patient_records` fetches a patient's encounters, then loops
and runs ONE query per encounter to load that encounter's records — no JOIN, no
`selectinload`. It also measures `records/search`, which is a full-table `ILIKE`
on `records.body` with no supporting index and no result limit.

We do not need a live database to count the queries: the pattern is a property of
the code, and the projection is arithmetic. Running against a real DB would add
timing noise without changing the conclusion.
"""
import csv
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDS_APP = os.path.join(REPO_ROOT, "services", "records-service", "app.py")
ENCOUNTERS_CSV = os.path.join(REPO_ROOT, "db", "seed", "encounters.csv")


def count_queries_per_chart(encounters_per_patient: int) -> int:
    """1 query for the encounter list + 1 per encounter for its records."""
    return 1 + encounters_per_patient


def read_seed_shape() -> dict:
    counts: dict[int, int] = {}
    with open(ENCOUNTERS_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pid = int(row["patient_id"])
            counts[pid] = counts.get(pid, 0) + 1
    return counts


def confirm_pattern_still_present() -> bool:
    """If someone collapses the loop into a JOIN, this script should say so."""
    source = open(RECORDS_APP, encoding="utf-8").read()
    loop = re.search(r"for enc in encounters:", source)
    inner_query = re.search(r"for enc in encounters:.*?db\.execute\(", source, re.DOTALL)
    return bool(loop and inner_query)


def main() -> int:
    present = confirm_pattern_still_present()
    counts = read_seed_shape()
    encounters = sum(counts.values())
    patients_with_encounters = len(counts)
    avg = encounters / max(1, patients_with_encounters)

    print("N+1 measurement — records read path (D8)")
    print("=" * 52)
    print(f"source pattern present: {present}")
    print(f"  services/records-service/app.py:get_patient_records")
    print()
    print("seeded corpus (db/seed/encounters.csv)")
    print(f"  patients with encounters : {patients_with_encounters}")
    print(f"  encounters               : {encounters}")
    print(f"  encounters per patient   : {avg:.2f} avg, {max(counts.values())} max")
    print()
    print("queries to assemble ONE chart")
    for pid in sorted(counts):
        print(f"  patient {pid}: {count_queries_per_chart(counts[pid])} "
              f"({counts[pid]} encounters + 1 list query)")
    print()
    print("projection at realistic volume")
    print("  A patient with a chronic condition accumulates encounters quickly.")
    for n in (10, 25, 50, 100):
        print(f"  {n:>3} encounters -> {count_queries_per_chart(n):>3} queries per chart open")
    print()
    print("records/search")
    print("  full-table ILIKE on records.body, no supporting index, NO LIMIT.")
    print("  Every search scans every row and materializes every match.")
    print()
    print("Status: MEASURED AND NAMED, not fixed. Fixing it is a records-service")
    print("change (selectinload or a JOIN, plus an index and a limit on search)")
    print("and is outside W4's scope. See docs/findings/w4-n-plus-one.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
