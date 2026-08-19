"""Parsing mute directives written by humans in a brief's Slack thread.

The point is that silencing something takes one line typed where the
complaint already lives -- as a reply under the brief that flagged it.

    !mute <link>                    stay quiet until the client writes again
    !mute forever <link>            never flag this again
    !unmute <link>                  undo either of the above

Anything after the link is kept as a note:

    !mute https://mail.google.com/... Mary handled this off-thread

Only @myrealprofit.com authors are honoured.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone

from .rules import MRP_DOMAIN, Mute

STORE = os.path.join(os.path.dirname(__file__), "mutes.json")

DIRECTIVE = re.compile(
    r"^\s*!(?P<verb>mute|unmute)\b"
    r"(?:\s+(?P<forever>forever))?"
    r"\s+(?P<target><?[^\s>|]+>?)"
    r"(?P<note>.*)$",
    re.IGNORECASE,
)


def _clean(target: str) -> str:
    return target.strip().strip("<>").split("|")[0].strip()


def parse_directives(replies: list[dict]) -> tuple[list[Mute], list[str]]:
    """Read mute/unmute lines out of a brief thread's replies.

    `replies` is a list of {"sender": ..., "ts": ..., "body": ...}.
    Returns (mutes, unmuted_tokens). Later directives win over earlier ones.
    """
    mutes: dict[str, Mute] = {}
    unmuted: list[str] = []

    for reply in replies:
        sender = (reply.get("sender") or "").lower().strip()
        if not sender.endswith(MRP_DOMAIN):
            continue  # only the team can silence the brief
        for line in (reply.get("body") or "").splitlines():
            m = DIRECTIVE.match(line)
            if not m:
                continue
            token = _clean(m.group("target"))
            if not token:
                continue
            if m.group("verb").lower() == "unmute":
                mutes.pop(token, None)
                unmuted.append(token)
                continue
            ts = reply.get("ts")
            baseline = None
            if ts is not None:
                baseline = (
                    ts if isinstance(ts, datetime)
                    else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                )
                if baseline.tzinfo is None:
                    baseline = baseline.replace(tzinfo=timezone.utc)
            mutes[token] = Mute(
                tokens=[token],
                scope="forever" if m.group("forever") else "until_new_client_message",
                baseline_ts=baseline,
                muted_by=sender,
                note=(m.group("note") or "").strip(" -–—:"),
            )
            unmuted[:] = [u for u in unmuted if u != token]

    return list(mutes.values()), unmuted


def load(path: str = STORE) -> list[Mute]:
    if not os.path.exists(path):
        return []
    out = []
    for row in json.load(open(path)):
        ts = row.get("baseline_ts")
        out.append(Mute(
            tokens=row["tokens"],
            scope=row.get("scope", "until_new_client_message"),
            baseline_ts=datetime.fromisoformat(ts) if ts else None,
            muted_by=row.get("muted_by", ""),
            note=row.get("note", ""),
        ))
    return out


def save(mutes: list[Mute], path: str = STORE) -> None:
    rows = []
    for m in mutes:
        d = asdict(m)
        d["baseline_ts"] = m.baseline_ts.isoformat() if m.baseline_ts else None
        rows.append(d)
    with open(path, "w") as fh:
        json.dump(rows, fh, indent=2, sort_keys=True)
        fh.write("\n")


def merge(stored: list[Mute], fresh: list[Mute], unmuted: list[str]) -> list[Mute]:
    """Fold this run's directives into the stored set."""
    by_token = {m.tokens[0]: m for m in stored if m.tokens}
    for m in fresh:
        by_token[m.tokens[0]] = m
    for token in unmuted:
        by_token.pop(token, None)
    return list(by_token.values())
