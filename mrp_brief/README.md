# mrp_brief — deterministic pre-processing for the MRP daily brief

## Why this exists

The MRP brief routine was a ~500-line prose rulebook asked to do two very
different jobs at once:

1. **Retrieval** — fetch threads, sort by timestamp, find the last sender,
   dedupe by thread, notice when an API returned a partial result.
2. **Judgement** — is this client actually waiting on us?

Job 1 is deterministic and a model does it unreliably. Every false positive we
traced on 19 Aug 2026 came from job 1, not job 2:

| Incident | Cause |
|---|---|
| Kennon flagged for 2 days | `search_threads` returned 5 of 7 messages; the reply was in the 2 it dropped |
| David + Maria as two items | tracked by message id instead of thread id |
| Budimir flagged for 7 days | outbound forward treated as an unanswered client message |
| "Maria" filed under a support@ sent message | authorship read off the To: header |

No amount of prompt wording fixes a truncated API response. This package moves
job 1 into code and leaves the model only job 2.

## The contract

```
raw MCP dumps ──► normalize ──► report.json ──┬─► waiting     (flag as-is)
                                              ├─► judge       ◄── model reads ONLY these
                                              ├─► resolved    (drop)
                                              ├─► handed_off  (drop, count)
                                              └─► unverified  (warn, never flag)
```

The **unverified** bucket is the important one. When a fetch returns fewer
messages than the search claimed, the item is not flagged — it produces a
warning instead. A brief that occasionally says "couldn't verify 2 threads" is
strictly better than one that confidently pings Lisa about an answered email.

## Usage from the routine

```bash
# 1. the model dumps whatever the MCP tools returned
#    (one JSON file, or many — globs are fine)
python3 -m mrp_brief.normalize raw/*.json --out report.json

# 2. the model reads report.json, and reads ONLY the judge + waiting buckets
```

On the 19 Aug data that is **1 item to judge** instead of ~40 threads to read.

## What code decides vs. what the model decides

Code decides, with no model involved:

- fetch completeness (`UNVERIFIED`)
- ✅ reactions and `No Reply Needed` on the **latest** message
- last message is from MRP → resolved
- forward direction — support@ → teammate is handed off, teammate → support@
  is still waiting
- whole-message closing phrases ("Thanks!")
- thread dedupe, wait time, escalation thresholds

Code does **not** decide sign-offs. When a client's message carries courtesy
but no question and no request — Alex's "here's the new owner's email, thanks!"
— it goes to `JUDGE` and the model reads it. Routing is mechanical; the call is
not.

## Fixtures

`fixtures/cases.json` is the regression suite. Every entry is a real thread
from #mrp-daily-brief with the verdict it should produce.

```bash
python3 -m mrp_brief.test_rules
```

Run this before shipping any change to the routine prompt or to `rules.py`.
It already earned its keep: it caught a precedence bug where a teammate →
support@ forward matched the outbound rule first and was wrongly dropped —
which would have re-opened the exact miss the inbound-forward rule was
written to prevent.

Note that `kennon_truncated_search` and `kennon_complete` share a thread id on
purpose. `dedupe` merges them and the recovered full message list resolves the
thread — a partial fetch plus a complete one should never leave the item
flagged.

## Muting an item

Reply in the thread of any recent brief:

```
!mute <link>              stay quiet until the client writes again
!mute forever <link>      never flag this again
!unmute <link>            undo either
```

Anything after the link is kept as a note. The link can be a Slack permalink,
a Gmail message id, or a thread id — matching is loose on purpose so nobody
has to look up an internal id.

Three properties worth knowing:

- **`!mute` re-arms.** The default scope is `until_new_client_message`, so the
  moment the client writes again the item comes back. A mute is a snooze, not
  a hole to lose mail in. Use `forever` only when the conversation is over.
- **It covers both paths.** The routine finds items two ways — carry-forward
  from the ledger, and a fresh 72h scan. A mute is checked in both, otherwise
  the scan would simply rediscover the thread tomorrow.
- **Only the team can mute.** Directives from non-`@myrealprofit.com` authors
  are ignored, and muted counts still appear in the audit line — silence is
  fine, invisible silence is not.

## Ledger

`ledger.py` replaces re-parsing state out of a Slack thread reply each run.
`ledger.json` is committed, so state survives a bad run, every change is
diffable, and an `UNVERIFIED` item is left exactly as it was rather than being
silently closed.

## Not done yet

`normalize.py` consumes dumps the model produces from MCP tool calls, so the
model still performs the I/O. Giving this package direct Gmail/Slack/Intercom
credentials would close the loop and remove the last non-deterministic step —
that needs OAuth setup and is the natural next step.
