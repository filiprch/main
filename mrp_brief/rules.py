"""Deterministic verdict engine for the MRP brief.

Everything in here is decidable without a model. The routine's job is to fetch
raw data and hand it to `classify`; the model only ever sees the residue that
comes back as JUDGE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

MRP_DOMAIN = "@myrealprofit.com"
SUPPORT_ADDRESS = "support@myrealprofit.com"

LABEL_NO_REPLY = "Label_4532895706505684286"   # ✅ No Reply Needed
LABEL_NEEDS_REPLY = "Label_3966147451254597849"  # 🟡 Needs Reply

RESOLVE_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}

# A message whose ENTIRE content is one of these is a closing phrase.
CLOSING_PHRASES = {
    "thanks", "thank you", "thx", "got it", "ok", "okay", "sounds good",
    "perfect", "great thanks", "understood", "clear", "appreciate it",
    "that works", "all good", "cheers", "resolved", "done", "nice thanks",
}

# Courtesy tokens that may appear alongside real content.
COURTESY_TOKENS = (
    "thanks", "thank you", "thx", "cheers", "appreciate it", "much appreciated",
)

# Signals that the client is still asking for something.
REQUEST_PATTERNS = (
    r"\bcan (?:we|you|i)\b", r"\bcould (?:we|you|i)\b", r"\bwould (?:we|you|i)\b",
    r"\bplease\b", r"\bwe need\b", r"\bi need\b", r"\bany update\b",
    r"\bchecking on\b", r"\bfollowing up\b", r"\bhow do (?:we|i)\b",
    r"\bis (?:it|there|this)\b", r"\bwhen (?:will|can|do)\b", r"\blet me know\b",
)

# Verdicts
UNVERIFIED = "UNVERIFIED"   # could not confirm -> warn, never flag
RESOLVED = "RESOLVED"       # closed -> drop from ledger
HANDED_OFF = "HANDED_OFF"   # routed to a teammate -> drop, count only
WAITING = "WAITING"         # flag it
JUDGE = "JUDGE"             # model must read this one


@dataclass
class Message:
    id: str
    ts: datetime
    sender: str
    body: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    reactions: list[str] = field(default_factory=list)
    is_forward: bool = False

    @property
    def sender_is_mrp(self) -> bool:
        return self.sender.lower().strip().endswith(MRP_DOMAIN)

    @property
    def recipients(self) -> list[str]:
        return [a.lower().strip() for a in (self.to + self.cc)]


@dataclass
class Thread:
    thread_id: str
    source: str                      # "gmail" | "slack" | "intercom"
    subject: str = ""
    messages: list[Message] = field(default_factory=list)
    fetch_complete: bool = True
    # How many messages the *search* result claimed. If the full fetch returns
    # fewer, the fetch is untrustworthy -- this is the Kennon failure.
    search_hint_count: int | None = None
    parent_reactions: list[str] = field(default_factory=list)

    def sorted_messages(self) -> list[Message]:
        return sorted(self.messages, key=lambda m: m.ts)


@dataclass
class Verdict:
    thread_id: str
    verdict: str
    reason: str
    wait_seconds: int | None = None
    escalated: bool = False
    signals: dict = field(default_factory=dict)


def _normalise(text: str) -> str:
    return re.sub(r"[\s\.\!\,]+", " ", (text or "").strip().lower()).strip()


def is_closing_phrase(body: str) -> bool:
    """True when the ENTIRE message is a courtesy phrase and nothing else."""
    return _normalise(body) in CLOSING_PHRASES


def signoff_signals(body: str) -> dict:
    """Evidence about whether a content-bearing message is a client sign-off.

    This does NOT decide. It routes: when a message carries courtesy but no
    question and no request, a human-level judgement is needed and the item
    goes to the model as JUDGE.
    """
    low = (body or "").lower()
    return {
        "has_courtesy": any(t in low for t in COURTESY_TOKENS),
        "has_question": "?" in low,
        "has_request": any(re.search(p, low) for p in REQUEST_PATTERNS),
    }


def is_outbound_forward(msg: Message) -> bool:
    """The scanned mailbox routing a client request OUT to a teammate.

    Direction is decided by who SENT it. support@ -> mary@ is a handoff;
    mary@ -> support@ is the opposite and is handled by is_inbound_forward.
    """
    if not (msg.is_forward and msg.sender_is_mrp):
        return False
    if msg.sender.lower().strip() != SUPPORT_ADDRESS:
        return False
    rcpts = msg.recipients
    return bool(rcpts) and all(r.endswith(MRP_DOMAIN) for r in rcpts)


def is_inbound_forward(msg: Message) -> bool:
    """A teammate forwarding a client request INTO support@."""
    if not (msg.is_forward and msg.sender_is_mrp):
        return False
    if msg.sender.lower().strip() == SUPPORT_ADDRESS:
        return False
    return SUPPORT_ADDRESS in msg.recipients


def classify(thread: Thread, now: datetime | None = None) -> Verdict:
    now = now or datetime.now(timezone.utc)
    tid = thread.thread_id

    # --- Evidence gate. An unverified item is never flagged. -----------------
    if not thread.fetch_complete:
        return Verdict(tid, UNVERIFIED, "full-thread fetch failed")
    if not thread.messages:
        return Verdict(tid, UNVERIFIED, "no messages returned")
    if (thread.search_hint_count is not None
            and len(thread.messages) < thread.search_hint_count):
        return Verdict(
            tid, UNVERIFIED,
            f"fetch returned {len(thread.messages)} of "
            f"{thread.search_hint_count} messages the search reported",
        )

    msgs = thread.sorted_messages()
    latest = msgs[-1]

    # --- Explicit human close signals ---------------------------------------
    if set(latest.reactions) & RESOLVE_REACTIONS:
        return Verdict(tid, RESOLVED, "resolve reaction on latest message")
    if set(thread.parent_reactions) & RESOLVE_REACTIONS:
        return Verdict(tid, RESOLVED, "resolve reaction on thread parent")
    if LABEL_NO_REPLY in latest.labels:
        return Verdict(tid, RESOLVED, "No Reply Needed on latest message")

    wait = int((now - latest.ts).total_seconds())

    # --- Last message is ours -----------------------------------------------
    if latest.sender_is_mrp:
        if is_inbound_forward(latest):
            return Verdict(tid, WAITING, "client request forwarded into support@",
                           wait_seconds=wait, escalated=_escalates(wait, latest))
        if is_outbound_forward(latest):
            return Verdict(tid, HANDED_OFF,
                           f"forwarded to {', '.join(latest.recipients)}")
        return Verdict(tid, RESOLVED, "last message is from MRP")

    # --- Last message is the client's ---------------------------------------
    if is_closing_phrase(latest.body):
        return Verdict(tid, RESOLVED, "closing phrase only")

    sig = signoff_signals(latest.body)
    if sig["has_courtesy"] and not sig["has_question"] and not sig["has_request"]:
        return Verdict(tid, JUDGE, "possible client sign-off - needs a read",
                       wait_seconds=wait, signals=sig)

    return Verdict(tid, WAITING, "client's message is unanswered",
                   wait_seconds=wait, escalated=_escalates(wait, latest), signals=sig)


ESCALATION_KEYWORDS = ("urgent", "blocking", "escalate", "still not resolved")
ESCALATION_TOPICS = ("billing", "payment", "invoice", "cannot log in",
                     "lost access", "data loss")


def _escalates(wait_seconds: int, latest: Message) -> bool:
    if wait_seconds > timedelta(days=3).total_seconds():
        return True
    low = (latest.body or "").lower()
    return any(k in low for k in ESCALATION_KEYWORDS + ESCALATION_TOPICS)


def dedupe(threads: list[Thread]) -> list[Thread]:
    """One thread = one item. Later entries merge into the first."""
    seen: dict[str, Thread] = {}
    for t in threads:
        if t.thread_id not in seen:
            seen[t.thread_id] = t
            continue
        kept = seen[t.thread_id]
        known = {m.id for m in kept.messages}
        kept.messages.extend(m for m in t.messages if m.id not in known)
        kept.fetch_complete = kept.fetch_complete and t.fetch_complete
    return list(seen.values())
