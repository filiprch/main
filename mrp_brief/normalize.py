"""Turn raw MCP dumps into a verdict report.

The routine writes whatever the Gmail/Slack/Intercom MCP tools returned into
JSON files, then runs:

    python3 -m mrp_brief.normalize raw/*.json --out report.json

`report.json` splits every thread into buckets. The model then reads ONLY the
`judge` bucket and the `waiting` bucket -- never the raw dumps.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timezone

from .rules import (
    HANDED_OFF, JUDGE, RESOLVED, UNVERIFIED, WAITING,
    Message, Thread, classify, dedupe,
)


def _parse_ts(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def thread_from_dict(d: dict) -> Thread:
    return Thread(
        thread_id=d["thread_id"],
        source=d.get("source", "gmail"),
        subject=d.get("subject", ""),
        fetch_complete=d.get("fetch_complete", True),
        search_hint_count=d.get("search_hint_count"),
        parent_reactions=d.get("parent_reactions", []),
        messages=[
            Message(
                id=m["id"],
                ts=_parse_ts(m["ts"]),
                sender=m["sender"],
                body=m.get("body", ""),
                to=m.get("to", []),
                cc=m.get("cc", []),
                labels=m.get("labels", []),
                reactions=m.get("reactions", []),
                is_forward=m.get("is_forward", False),
            )
            for m in d.get("messages", [])
        ],
    )


def load(paths: list[str]) -> list[Thread]:
    threads: list[Thread] = []
    for pattern in paths:
        for path in sorted(glob.glob(pattern)) or [pattern]:
            with open(path) as fh:
                payload = json.load(fh)
            items = payload if isinstance(payload, list) else [payload]
            threads.extend(thread_from_dict(i) for i in items)
    return threads


def build_report(threads: list[Thread], now: datetime | None = None) -> dict:
    threads = dedupe(threads)
    buckets: dict[str, list] = {
        "waiting": [], "judge": [], "resolved": [],
        "handed_off": [], "unverified": [],
    }
    key = {
        WAITING: "waiting", JUDGE: "judge", RESOLVED: "resolved",
        HANDED_OFF: "handed_off", UNVERIFIED: "unverified",
    }
    by_id = {t.thread_id: t for t in threads}
    for thread in threads:
        v = classify(thread, now=now)
        buckets[key[v.verdict]].append({
            "thread_id": v.thread_id,
            "source": by_id[v.thread_id].source,
            "subject": by_id[v.thread_id].subject,
            "verdict": v.verdict,
            "reason": v.reason,
            "wait_seconds": v.wait_seconds,
            "escalated": v.escalated,
            "signals": v.signals,
        })
    for name in buckets:
        buckets[name].sort(key=lambda r: r["wait_seconds"] or 0, reverse=True)
    buckets["counts"] = {k: len(v) for k, v in buckets.items()}
    return buckets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="raw JSON dump files or globs")
    ap.add_argument("--out", help="write report here (default: stdout)")
    args = ap.parse_args(argv)

    report = build_report(load(args.paths))
    text = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        c = report["counts"]
        print(f"waiting={c['waiting']} judge={c['judge']} resolved={c['resolved']} "
              f"handed_off={c['handed_off']} unverified={c['unverified']}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
