"""Regression suite. Every fixture is a real incident from #mrp-daily-brief.

Run before shipping any change to the routine prompt or to rules.py:

    python3 -m mrp_brief.test_rules
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from .normalize import thread_from_dict
from .rules import Thread, classify, dedupe

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "cases.json")
NOW = datetime(2026, 8, 19, 15, 20, tzinfo=timezone.utc)  # the 19 Aug evening run


def run() -> int:
    cases = json.load(open(FIXTURES))
    failures = []

    for case in cases:
        got = classify(thread_from_dict(case), now=NOW)
        want = case["_expect"]
        status = "PASS" if got.verdict == want else "FAIL"
        if status == "FAIL":
            failures.append((case["_case"], want, got.verdict, got.reason))
        print(f"  [{status}] {case['_case']:<28} want={want:<11} "
              f"got={got.verdict:<11} ({got.reason})")

    # One thread must never occupy two ledger rows (the David/Maria bug).
    dup = [Thread(thread_id="T1", source="gmail"),
           Thread(thread_id="T1", source="gmail"),
           Thread(thread_id="T2", source="gmail")]
    merged = len(dedupe(dup))
    ok = merged == 2
    print(f"  [{'PASS' if ok else 'FAIL'}] dedupe_same_thread          "
          f"want=2          got={merged}")
    if not ok:
        failures.append(("dedupe_same_thread", "2", str(merged), "dedupe"))

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for name, want, got, reason in failures:
            print(f"  {name}: expected {want}, got {got} ({reason})")
        return 1
    print(f"all {len(cases) + 1} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
