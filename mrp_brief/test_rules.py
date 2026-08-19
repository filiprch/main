"""Regression suite. Every fixture is a real incident from #mrp-daily-brief.

Run before shipping any change to the routine prompt or to rules.py:

    python3 -m mrp_brief.test_rules
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from .mute import parse_directives
from .normalize import thread_from_dict
from .rules import MUTED, WAITING, Thread, classify, dedupe

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

    # --- mute directives ------------------------------------------------
    for name, ok, detail in _mute_checks():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<28} {detail}")
        if not ok:
            failures.append((name, "pass", "fail", detail))

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for name, want, got, reason in failures:
            print(f"  {name}: expected {want}, got {got} ({reason})")
        return 1
    print(f"all {len(cases) + 1 + len(_mute_checks())} checks passed")
    return 0


def _thread_with_client_reply_at(ts: str) -> Thread:
    """Alex's thread plus a later client message, to test re-arming."""
    case = dict(json.load(open(FIXTURES))[0])
    case["messages"] = case["messages"] + [{
        "id": "newclient1", "ts": ts, "sender": "alex@wearesynq.com",
        "to": ["mary@myrealprofit.com"], "body": "Actually one more thing - "
        "can you confirm the transfer went through?",
    }]
    case["search_hint_count"] = len(case["messages"])
    return thread_from_dict(case)


def _mute_checks():
    base = thread_from_dict(json.load(open(FIXTURES))[0])  # alex_signoff

    reply = [{
        "sender": "lisa@myrealprofit.com",
        "ts": "2026-08-19T15:00:00Z",
        "body": "!mute https://mail.google.com/mail/u/0/#inbox/19fdb6a9db073620 "
                "Mary handled this off-thread",
    }]
    mutes, _ = parse_directives(reply)
    out = []

    # a permalink pasted from the brief must match the thread
    v = classify(base, now=NOW, mutes=mutes)
    out.append(("mute_by_pasted_link", v.verdict == MUTED, v.reason))

    # the note and author survive into the reason
    out.append(("mute_records_author",
                "lisa@myrealprofit.com" in v.reason
                and "off-thread" in v.reason, v.reason))

    # a client writing again re-arms the item -- a mute is never a black hole
    rearmed = _thread_with_client_reply_at("2026-08-19T16:00:00Z")
    v2 = classify(rearmed, now=NOW, mutes=mutes)
    out.append(("mute_rearms_on_new_mail", v2.verdict == WAITING, v2.reason))

    # forever does not re-arm
    forever, _ = parse_directives([{
        "sender": "lisa@myrealprofit.com", "ts": "2026-08-19T15:00:00Z",
        "body": "!mute forever 19fd70285eece913",
    }])
    v3 = classify(rearmed, now=NOW, mutes=forever)
    out.append(("mute_forever_holds", v3.verdict == MUTED, v3.reason))

    # unmute clears it
    m4, un = parse_directives(reply + [{
        "sender": "lisa@myrealprofit.com", "ts": "2026-08-19T15:05:00Z",
        "body": "!unmute https://mail.google.com/mail/u/0/#inbox/19fdb6a9db073620",
    }])
    v4 = classify(base, now=NOW, mutes=m4)
    out.append(("unmute_clears", v4.verdict != MUTED and len(un) == 1, v4.reason))

    # a client cannot silence the brief
    outsider, _ = parse_directives([{
        "sender": "alex@wearesynq.com", "ts": "2026-08-19T15:00:00Z",
        "body": "!mute forever 19fd70285eece913",
    }])
    out.append(("outsider_cannot_mute", outsider == [], f"{len(outsider)} parsed"))

    return out


if __name__ == "__main__":
    sys.exit(run())
