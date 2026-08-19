"""Durable ledger state.

Replaces re-parsing a Slack thread reply every run. The ledger is a file the
routine commits, so state survives a bad run and every change is diffable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "ledger.json")


def load(path: str = DEFAULT_PATH) -> dict:
    if not os.path.exists(path):
        return {"updated_at": None, "items": {}}
    with open(path) as fh:
        return json.load(fh)


def save(state: dict, path: str = DEFAULT_PATH) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


def apply_report(state: dict, report: dict) -> dict:
    """Fold a verdict report into the ledger and report what changed.

    Carries first_seen forward so ages keep climbing. Items that came back
    UNVERIFIED are left exactly as they were -- a failed fetch must never
    silently close an item.
    """
    items = state.setdefault("items", {})
    now = datetime.now(timezone.utc).isoformat()
    changed = {"new": [], "closed": [], "handed_off": [], "unverified": []}

    for row in report.get("waiting", []) + report.get("judge", []):
        tid = row["thread_id"]
        if tid in items:
            items[tid].update(
                last_seen=now, wait_seconds=row["wait_seconds"],
                escalated=row["escalated"], verdict=row["verdict"],
            )
        else:
            items[tid] = {
                "first_seen": now, "last_seen": now, "source": row["source"],
                "subject": row["subject"], "wait_seconds": row["wait_seconds"],
                "escalated": row["escalated"], "verdict": row["verdict"],
            }
            changed["new"].append(tid)

    for row in report.get("resolved", []):
        if items.pop(row["thread_id"], None):
            changed["closed"].append(row["thread_id"])

    for row in report.get("handed_off", []):
        if items.pop(row["thread_id"], None):
            changed["handed_off"].append(row["thread_id"])

    changed["unverified"] = [r["thread_id"] for r in report.get("unverified", [])]
    return changed
